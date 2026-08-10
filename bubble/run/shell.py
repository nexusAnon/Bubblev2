"""Long-lived named bubble — venv-shape view over the vault content store.

A shell is one version per package name (Python import semantics demand this),
linked from the vault. The manifest.toml is the externally-readable contract;
the shells DB row is internal bookkeeping.

Shell layout:
    ~/.bubble/shells/<name>/
        lib/<package>            -> vault symlink (whole package)
        bin/<entry-point>        -> generated console-script wrapper
        activate                 # POSIX sh, sourceable
        python                   # wrapper exec
        manifest.toml            # externally-readable contract
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from ..vault import db, store, metadata as meta


# ───────────────────────────── spec parsing ─────────────────────────────


_SPEC_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:==(.+))?$")


def parse_spec(spec: str) -> tuple[str, Optional[str]]:
    """Parse 'requests' or 'requests==2.31.0'. PEP 440 ranges not yet supported."""
    m = _SPEC_RE.match(spec.strip())
    if not m:
        raise ValueError(f"unparseable spec: {spec!r}")
    return m.group(1), m.group(2)


# ───────────────────── version & wheel-tag resolution ────────────────────


def _wheel_tag_score(tag: str) -> int:
    """Higher is better-matching for the current runner.

    Scoring is intentionally simple — exact (py+abi+plat) match wins, then
    interpreter-major match, then pure-python.
    """
    runner_py = config.runner_python_tag()
    runner_plat = config.runner_platform_tag()
    parts = tag.split("-")
    if len(parts) != 3:
        return 0
    py, abi, plat = parts
    score = 0
    if py == runner_py:
        score += 100
    elif py == "py3" or py.startswith("py3"):
        score += 30
    elif py.startswith(runner_py[:2]):  # cp* matches cp*
        score += 20
    if abi in ("none", "abi3"):
        score += 10
    elif abi == runner_py:
        score += 50
    if plat == "any":
        score += 5
    elif runner_plat in plat or plat in runner_plat:
        score += 40
    return score


def best_version(conn: sqlite3.Connection, name: str,
                 pinned_version: Optional[str] = None) -> Optional[tuple[str, str, str]]:
    """Pick (version, wheel_tag, vault_path) for a package.

    If pinned_version: must match. Otherwise highest version.
    Within a version, pick the wheel-tag with highest score for the runner.
    PEP 503: matches pydantic-core/pydantic_core/Pydantic.Core interchangeably.
    """
    rows = store.find_versions(conn, name)
    if not rows:
        # Try PEP 503 normalized variants
        norm = meta.normalize_name(name)
        candidates = conn.execute(
            "SELECT name FROM packages GROUP BY name"
        ).fetchall()
        for (cand_name,) in candidates:
            if meta.normalize_name(cand_name) == norm:
                rows = store.find_versions(conn, cand_name)
                if rows:
                    break
    if not rows:
        return None
    if pinned_version:
        rows = [r for r in rows if r[0] == pinned_version]
        if not rows:
            return None

    # Group by version; pick highest version (string-sort approximates PEP 440)
    rows.sort(key=lambda r: _version_key(r[0]), reverse=True)
    target_version = rows[0][0]
    same = [r for r in rows if r[0] == target_version]
    same.sort(key=lambda r: _wheel_tag_score(r[1]), reverse=True)
    return same[0]


def _version_key(v: str) -> tuple:
    """Cheap PEP 440-ish sort key. Splits on dots, ints when possible."""
    parts = []
    for chunk in re.split(r"[.\-+]", v):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


# ───────────────────────── entry-point extraction ────────────────────────


def _entry_points_for(vault_path: Path) -> list[tuple[str, str, str]]:
    """Return list of (script_name, module, attr) from any entry_points.txt
    or RECORD-discoverable entry_points within the package's dist-info.

    The dist-info ends up under vault_path because RECORD-listed files
    include it. Find any *.dist-info/entry_points.txt.
    """
    out = []
    for ep_file in vault_path.rglob("entry_points.txt"):
        section = None
        for line in ep_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section != "console_scripts":
                continue
            if "=" not in line:
                continue
            name, _, target = line.partition("=")
            name = name.strip()
            target = target.strip()
            if ":" in target:
                module, _, attr = target.partition(":")
            else:
                module, attr = target, "main"
            out.append((name, module.strip(), attr.strip()))
    return out


_CONSOLE_WRAPPER_TMPL = """#!{python}
# bubble shell entry-point wrapper for {script_name}
import sys
from {module} import {attr_root}
if __name__ == "__main__":
    sys.exit({attr}() if callable({attr}) else 0)
"""


def _write_console_wrapper(path: Path, python: str, module: str, attr: str,
                           script_name: str) -> None:
    attr_root = attr.split(".")[0]
    content = _CONSOLE_WRAPPER_TMPL.format(
        python=python,
        script_name=script_name,
        module=module,
        attr=attr,
        attr_root=attr_root,
    )
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ─────────────────────────── activate / launcher ─────────────────────────


_ACTIVATE_TMPL = """# bubble shell activate — source this from POSIX sh / bash / zsh
# usage: source {shell_dir}/activate
_BUBBLE_OLD_PYTHONPATH="${{PYTHONPATH:-}}"
_BUBBLE_OLD_PATH="$PATH"
_BUBBLE_OLD_LD_LIBRARY_PATH="${{LD_LIBRARY_PATH:-}}"
_BUBBLE_OLD_DYLD_LIBRARY_PATH="${{DYLD_LIBRARY_PATH:-}}"
_BUBBLE_OLD_PKG_CONFIG_PATH="${{PKG_CONFIG_PATH:-}}"

export PYTHONPATH="{shell_lib}{parent_libs}${{PYTHONPATH:+:$PYTHONPATH}}"
export PATH="{shell_bin}:$PATH"
export LD_LIBRARY_PATH="{shell_lib}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}"
export DYLD_LIBRARY_PATH="{shell_lib}${{DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}}"
export PKG_CONFIG_PATH="{shell_lib}/pkgconfig:{shell_lib}${{PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}}"
export BUBBLE_SHELL="{name}"
export BUBBLE_SHELL_DIR="{shell_dir}"

bubble_deactivate() {{
    export PYTHONPATH="$_BUBBLE_OLD_PYTHONPATH"
    export PATH="$_BUBBLE_OLD_PATH"
    export LD_LIBRARY_PATH="$_BUBBLE_OLD_LD_LIBRARY_PATH"
    export DYLD_LIBRARY_PATH="$_BUBBLE_OLD_DYLD_LIBRARY_PATH"
    export PKG_CONFIG_PATH="$_BUBBLE_OLD_PKG_CONFIG_PATH"
    unset BUBBLE_SHELL BUBBLE_SHELL_DIR _BUBBLE_OLD_PYTHONPATH _BUBBLE_OLD_PATH
    unset _BUBBLE_OLD_LD_LIBRARY_PATH _BUBBLE_OLD_DYLD_LIBRARY_PATH _BUBBLE_OLD_PKG_CONFIG_PATH
    unset -f bubble_deactivate
}}
"""


_PYTHON_LAUNCHER_TMPL = """#!/bin/sh
# bubble shell python launcher
exec {python} "$@"
"""


def _write_activate(shell_dir: Path, name: str,
                    parent_lib_dirs: Optional[list[Path]] = None) -> None:
    # Prepend each parent shell's lib/ so `source activate` inherits their
    # packages on PYTHONPATH without needing a meta-finder.
    parent_libs = "".join(f":{p}" for p in (parent_lib_dirs or []))
    activate = shell_dir / "activate"
    activate.write_text(_ACTIVATE_TMPL.format(
        shell_dir=str(shell_dir),
        shell_lib=str(shell_dir / "lib"),
        shell_bin=str(shell_dir / "bin"),
        name=name,
        parent_libs=parent_libs,
    ))
    py_launcher = shell_dir / "python"
    py_launcher.write_text(_PYTHON_LAUNCHER_TMPL.format(python=sys.executable))
    py_launcher.chmod(py_launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ─────────────────────────── manifest.toml ───────────────────────────────


def _write_manifest(shell_dir: Path, name: str, packages: dict) -> None:
    """packages: { pkg_name: {'version': v, 'wheel_tag': t} }"""
    lines = [
        f'# bubble shell manifest — externally-readable contract',
        f'name = "{name}"',
        f'created_at = "{datetime.now().isoformat()}"',
        f'',
        f'[packages]',
    ]
    for pkg in sorted(packages):
        info = packages[pkg]
        lines.append(f'"{pkg}" = {{ version = "{info["version"]}", wheel_tag = "{info["wheel_tag"]}" }}')
    (shell_dir / "manifest.toml").write_text("\n".join(lines) + "\n")


def _read_manifest(shell_dir: Path) -> dict:
    """Cheap TOML reader for manifest.toml. Only handles the format we write."""
    path = shell_dir / "manifest.toml"
    if not path.exists():
        return {}
    pkgs = {}
    in_section = False
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("[packages]"):
            in_section = True
            continue
        if line.startswith("[") and line != "[packages]":
            in_section = False
            continue
        if not in_section or not line or line.startswith("#"):
            continue
        # "name" = { version = "v", wheel_tag = "t" }
        m = re.match(r'"([^"]+)"\s*=\s*\{\s*version\s*=\s*"([^"]+)"\s*,'
                     r'\s*wheel_tag\s*=\s*"([^"]+)"\s*\}', line)
        if m:
            pkgs[m.group(1)] = {"version": m.group(2), "wheel_tag": m.group(3)}
    return pkgs


# ─────────────────────────── shell operations ────────────────────────────


def shell_dir(name: str) -> Path:
    if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", name):
        raise ValueError(f"shell names must be [A-Za-z0-9_-]+ up to 64 chars, got {name!r}")
    return config.SHELLS_DIR / name


def _verify_for_link(pkg_name: str, version: Optional[str],
                     wheel_tag: Optional[str]) -> None:
    """Drift-verify a vault entry before linking. Refusal raises RuntimeError
    and records to host.toml."""
    if not (version and wheel_tag) or os.environ.get("BUBBLE_VERIFY") == "0":
        return
    from .. import host
    report = store.verify(pkg_name, version, wheel_tag)
    if report.had_index and not report.clean:
        target = f"{pkg_name}=={version}@{wheel_tag}"
        for rel, kind in report.drifted:
            host.record_failure(kind, target, f"rel={rel}")
        for rel in report.missing:
            host.record_failure("vault_drift_missing", target, f"rel={rel}")
        raise RuntimeError(
            f"vault drift refusing to link {target}: "
            f"{len(report.drifted)} modified, {len(report.missing)} missing. "
            f"Run `bubble vault rehash {pkg_name} {version} {wheel_tag}` "
            f"to re-record, or `bubble vault remove ...` to drop the entry."
        )


def _rel_symlink(dest: Path, target: Path) -> None:
    """Emit a relative symlink from dest to target. Replaces dest if present.

    Relative is required for relocatability: a wholesale move of BUBBLE_HOME
    (vault + shells together) must preserve every link. Bundle/unbundle
    depends on this property."""
    if dest.is_symlink() or dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    rel_target = os.path.relpath(target, start=dest.parent)
    os.symlink(rel_target, dest)


def _link_package(shell_lib: Path, vault_path: Path, pkg_name: str,
                  *, version: Optional[str] = None,
                  wheel_tag: Optional[str] = None) -> list[str]:
    """Symlink importable top-levels from vault_path into shell_lib as
    whole-package links. Returns list of names linked.

    The dist-info dir is *not* handled here — call _link_distinfo separately.
    The split exists so namespace-package merging (where a single import name
    is contributed by N>1 vault packages) can take a different code path
    without re-implementing dist-info handling.
    """
    if not store.is_under_vault(vault_path):
        raise ValueError(f"refusing to link from outside the vault: {vault_path}")
    _verify_for_link(pkg_name, version, wheel_tag)
    linked = []
    shell_lib.mkdir(parents=True, exist_ok=True)
    for entry in vault_path.iterdir():
        if entry.name.endswith(".dist-info"):
            continue
        if entry.name.endswith(".data"):
            # wheel .data dir has scripts/headers/data subtrees; skip the
            # top-level wrapper (entry-points handled separately)
            continue
        _rel_symlink(shell_lib / entry.name, entry)
        linked.append(entry.name)
    return linked


def _link_distinfo(shell_lib: Path, vault_path: Path) -> list[str]:
    """Symlink every *.dist-info/ from vault_path into shell_lib.

    Without this, importlib.metadata.entry_points() / .distribution() /
    .version() return nothing inside the shell, because they walk sys.path
    looking for dist-info dirs and there are none. Anything that uses
    entry-point metadata at runtime (opentelemetry context loaders,
    pkg_resources plugins, click commands declared as entry points,
    pytest plugins) silently sees zero entries before this lands.
    """
    if not store.is_under_vault(vault_path):
        raise ValueError(f"refusing to link dist-info from outside the vault: {vault_path}")
    linked = []
    shell_lib.mkdir(parents=True, exist_ok=True)
    for entry in vault_path.iterdir():
        if not entry.name.endswith(".dist-info"):
            continue
        _rel_symlink(shell_lib / entry.name, entry)
        linked.append(entry.name)
    return linked


def _merge_dirs(target_dir: Path, contrib_dirs: list[Path]) -> list[str]:
    """Recursively merge multiple source directories into target_dir.

    For each entry name across the contributions, if multiple contributions
    have a *directory* of that name, recurse: build target_dir/<name>/ as
    a real subdir and merge their contents. If only one contribution has
    it (or the conflicting entries aren't all directories), symlink the
    single contribution directly. Last-write-wins on file collisions.

    This handles the deeply-nested namespace case (e.g. opentelemetry's
    exporter/otlp/proto/{common,grpc,http} contributed by three distinct
    dists) — without recursion, only the first contribution's exporter/
    dir would be visible, shadowing the others' nested subtrees.
    """
    linked: list[str] = []
    # Build a map: name → list of (source_dir, source_path)
    by_name: dict[str, list[Path]] = {}
    for cdir in contrib_dirs:
        if not cdir.is_dir():
            continue
        for entry in cdir.iterdir():
            by_name.setdefault(entry.name, []).append(entry)

    target_dir.mkdir(parents=True, exist_ok=True)
    for name, sources in by_name.items():
        if len(sources) == 1:
            _rel_symlink(target_dir / name, sources[0])
            linked.append(name)
            continue
        # Multiple contributions claim this name. If they're all dirs, recurse.
        if all(s.is_dir() for s in sources):
            sub_target = target_dir / name
            if sub_target.is_symlink():
                sub_target.unlink()
            _merge_dirs(sub_target, sources)
            linked.append(name + "/")
        else:
            # File collision (or mixed dir/file) — last-write-wins, log.
            from ..vault import store as _store
            _store.top_level_contentions.append({
                "name": name, "kind": "shell_merge_collision",
                "contributors": [str(s) for s in sources],
            })
            _rel_symlink(target_dir / name, sources[-1])
            linked.append(name)
    return linked


def _link_namespace_merge(shell_lib: Path, top_name: str,
                          contributions: list[tuple[str, str, str, Path]]) -> list[str]:
    """When N>1 vault packages contribute the same top-level import name
    (PEP 420 namespace packages — e.g. opentelemetry from api+sdk+exporter-*),
    a single dir-level symlink to one of them shadows the rest. Instead,
    create lib/<top>/ as a real directory and merge each contribution's
    subentries into it, recursing where multiple contributions agree on
    intermediate directories.

    Sub-name collisions (two contributions both shipping the same submodule
    name) are resolved last-write-wins; logged via the existing
    top_level_contentions audit. The vault's PK already discriminates which
    distribution claims which import name; this function only handles the
    namespace case where all contributions are intentional.
    """
    target_dir = shell_lib / top_name
    if target_dir.is_symlink():
        target_dir.unlink()
    contrib_dirs: list[Path] = []
    linked: list[str] = []
    for pkg_name, version, wheel_tag, vault_path in contributions:
        _verify_for_link(pkg_name, version, wheel_tag)
        contrib_dir = vault_path / top_name
        if contrib_dir.is_dir():
            contrib_dirs.append(contrib_dir)
            continue
        flat = vault_path / f"{top_name}.py"
        if flat.is_file():
            target_dir.mkdir(parents=True, exist_ok=True)
            _rel_symlink(target_dir / f"{top_name}.py", flat)
            linked.append(f"{top_name}.py")
    if contrib_dirs:
        linked.extend(_merge_dirs(target_dir, contrib_dirs))
    return linked


# ──────────────────────── closure + namespace grouping ──────────────────────


def _resolve_closure(conn: sqlite3.Connection, specs: list[str],
                     existing: dict) -> tuple[list[tuple[str, str, str, str]],
                                              list[str], list[tuple]]:
    """Expand user specs to the transitive Requires-Dist closure.

    Each spec is 'pkg' or 'pkg==version'. For each, pick the best wheel-tag
    via best_version, then walk the `dependencies` table to add transitive
    deps (best-version-resolved) until fixed-point.

    Returns (closure, missing, conflicts) where:
      - closure: ordered list of (name, version, wheel_tag, vault_path) the
        shell should pin. Names are deduped — one version per package name,
        which Python import semantics demand.
      - missing: specs that didn't resolve to anything in the vault.
      - conflicts: deps whose closure expansion would replace an already-pinned
        version with a different one. Each is (name, existing_info, new_info).

    The closure is *empirical* on the vault: it pulls only deps that are
    actually vaulted. A dep listed in Requires-Dist but absent from `packages`
    is recorded as a missing spec (with kind shell_pkg_missing) so the
    operator sees the gap rather than getting a silently-incomplete shell.
    """
    closure_by_name: dict[str, tuple[str, str, str, str]] = {}
    missing: list[str] = []
    conflicts: list[tuple] = []

    # Seed with existing pins so transitive walks don't try to re-pin them.
    for pkg_name, info in existing.items():
        vp = store.vault_path_for(pkg_name, info["version"], info["wheel_tag"])
        closure_by_name[meta.normalize_name(pkg_name)] = (
            pkg_name, info["version"], info["wheel_tag"], str(vp),
        )

    queue: list[tuple[str, Optional[str]]] = []
    for spec in specs:
        try:
            queue.append(parse_spec(spec))
        except ValueError:
            missing.append(spec)

    seen_specs: set[tuple[str, Optional[str]]] = set()
    while queue:
        pkg, ver_pin = queue.pop(0)
        if (pkg, ver_pin) in seen_specs:
            continue
        seen_specs.add((pkg, ver_pin))

        chosen = best_version(conn, pkg, ver_pin)
        if not chosen:
            spec_str = f"{pkg}=={ver_pin}" if ver_pin else pkg
            missing.append(spec_str)
            continue
        version, wheel_tag, vault_path = chosen

        # Resolve to canonical name as recorded in packages table
        row = conn.execute(
            "SELECT name FROM packages WHERE name=? OR LOWER(REPLACE(REPLACE(name,'_','-'),'.','-'))=? "
            "LIMIT 1",
            (pkg, meta.normalize_name(pkg)),
        ).fetchone()
        canonical = row[0] if row else pkg
        norm = meta.normalize_name(canonical)

        if norm in closure_by_name:
            existing_tuple = closure_by_name[norm]
            if (existing_tuple[1], existing_tuple[2]) != (version, wheel_tag):
                conflicts.append((
                    canonical,
                    {"version": existing_tuple[1], "wheel_tag": existing_tuple[2]},
                    {"version": version, "wheel_tag": wheel_tag},
                ))
            continue

        closure_by_name[norm] = (canonical, version, wheel_tag, str(vault_path))

        # Walk this package's deps. dep_version_spec is PEP 508; we ignore
        # the version constraint and let best_version pick the one we have.
        # Optional / extra-gated deps are skipped (optional=1) — extras must
        # be requested explicitly.
        dep_rows = conn.execute(
            "SELECT dep_name FROM dependencies "
            "WHERE package=? AND version=? AND wheel_tag=? "
            "AND (optional=0 OR optional IS NULL) "
            "AND (extra IS NULL OR extra='')",
            (canonical, version, wheel_tag),
        ).fetchall()
        for (dep_name,) in dep_rows:
            queue.append((dep_name, None))

    # Strip the seed entries (they were existing pins, not "linked this run").
    fresh = [v for k, v in closure_by_name.items()
             if k not in {meta.normalize_name(n) for n in existing}]
    return fresh, missing, conflicts


def _group_by_top_level(conn: sqlite3.Connection,
                        closure: list[tuple[str, str, str, str]]
                        ) -> dict[str, list[tuple[str, str, str, Path]]]:
    """Group closure entries by their top_level import names.

    Returns {import_name: [(pkg_name, version, wheel_tag, vault_path), ...]}.
    A name with len > 1 is a namespace-package contribution and routes
    through _link_namespace_merge; a name with len == 1 routes through
    _link_package.
    """
    by_top: dict[str, list[tuple[str, str, str, Path]]] = {}
    for pkg_name, version, wheel_tag, vault_path in closure:
        rows = conn.execute(
            "SELECT import_name FROM top_level "
            "WHERE package=? AND version=? AND wheel_tag=?",
            (pkg_name, version, wheel_tag),
        ).fetchall()
        if not rows:
            # No top_level rows recorded — fall back to walking the vault dir
            # for non-dist-info entries.
            vp = Path(vault_path)
            if vp.is_dir():
                for entry in vp.iterdir():
                    if entry.name.endswith(".dist-info") or entry.name.endswith(".data"):
                        continue
                    n = entry.name[:-3] if entry.name.endswith(".py") else entry.name
                    rows = rows + [(n,)] if rows else [(n,)]
        for (import_name,) in rows:
            by_top.setdefault(import_name, []).append(
                (pkg_name, version, wheel_tag, Path(vault_path))
            )
    return by_top


def _link_entry_points(shell_bin: Path, vault_path: Path, python: str) -> list[str]:
    shell_bin.mkdir(parents=True, exist_ok=True)
    written = []
    for script_name, module, attr in _entry_points_for(vault_path):
        wrapper = shell_bin / script_name
        if wrapper.exists():
            wrapper.unlink()
        _write_console_wrapper(wrapper, python, module, attr, script_name)
        written.append(script_name)
    return written


def _update_scope_in_metadata(conn: sqlite3.Connection,
                               name: str, packages: dict) -> None:
    """Write the current package pins into shells.metadata["scope"].

    Called inside an open connection before commit so the scope is always
    consistent with the manifest written in the same transaction.
    packages: {pkg_name: {"version": v, "wheel_tag": t}}
    """
    row = conn.execute(
        "SELECT metadata FROM shells WHERE name=?", (name,),
    ).fetchone()
    meta_blob = json.loads(row[0]) if row and row[0] else {}
    meta_blob["scope"] = {
        pkg: [info["version"], info["wheel_tag"]]
        for pkg, info in packages.items()
    }
    conn.execute(
        "UPDATE shells SET metadata=? WHERE name=?",
        (json.dumps(meta_blob), name),
    )


def load_scope(name: str) -> Optional[dict[str, tuple[str, str]]]:
    """Return the version-pinning scope stored in a shell's metadata, or None.

    The scope maps package (distribution) name → (version, wheel_tag).
    It is kept in sync with the shell manifest by every add/remove
    operation so BUBBLE_SHELL-aware import resolution always reflects
    the live shell state.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (name,),
        ).fetchone()
        if not row or not row[0]:
            return None
        scope_raw = json.loads(row[0]).get("scope")
        if not scope_raw:
            return None
        return {k: (v[0], v[1]) for k, v in scope_raw.items()}
    finally:
        conn.close()


def load_parent(name: str) -> Optional[str]:
    """Return the parent shell name recorded in metadata, or None."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (name,),
        ).fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0]).get("parent")
    finally:
        conn.close()


def set_parent(name: str, parent_name: str) -> None:
    """Record a parent shell for scope inheritance and regenerate activate."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (name,),
        ).fetchone()
        meta_blob = json.loads(row[0]) if row and row[0] else {}
        meta_blob["parent"] = parent_name
        conn.execute(
            "UPDATE shells SET metadata=? WHERE name=?",
            (json.dumps(meta_blob), name),
        )
        conn.commit()
    finally:
        conn.close()
    # Regenerate activate so parent libs appear on PYTHONPATH.
    sd = shell_dir(name)
    if sd.exists():
        parent_libs = _collect_parent_libs(parent_name)
        _write_activate(sd, name, parent_lib_dirs=parent_libs)


def _collect_parent_libs(start_name: str) -> list[Path]:
    """Walk the parent chain, return each shell's lib/ in order."""
    libs: list[Path] = []
    seen: set[str] = set()
    current: Optional[str] = start_name
    while current and current not in seen:
        seen.add(current)
        sd = shell_dir(current)
        lib = sd / "lib"
        if lib.is_dir():
            libs.append(lib)
        current = load_parent(current)
    return libs


def create(name: str, specs: list[str], *,
           exist_ok: bool = False,
           parent: Optional[str] = None) -> Path:
    """Create a new shell with optional initial packages and optional parent."""
    sd = shell_dir(name)
    if sd.exists():
        if not exist_ok:
            raise FileExistsError(f"shell already exists: {name}")
    else:
        sd.mkdir(parents=True)
        (sd / "lib").mkdir()
        (sd / "bin").mkdir()
    parent_libs = _collect_parent_libs(parent) if parent else []
    _write_activate(sd, name, parent_lib_dirs=parent_libs)
    _write_manifest(sd, name, {})
    initial_meta: dict = {}
    if parent:
        initial_meta["parent"] = parent
    conn = db.connect()
    conn.execute(
        "INSERT OR REPLACE INTO shells (name, created_at, last_used_at, "
        "shell_path, python_tag, lockfile, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, datetime.now().isoformat(), datetime.now().isoformat(),
         str(sd), config.runner_python_tag(), None, json.dumps(initial_meta)),
    )
    conn.commit()
    conn.close()
    if specs:
        add(name, specs)
    return sd


def add(name: str, specs: list[str]) -> dict:
    """Add packages to an existing shell, with full closure resolution.

    Each spec is 'pkg' or 'pkg==version'. The closure is expanded by walking
    the `dependencies` table (Requires-Dist) until fixed-point, so passing a
    single spec like 'rich-cli' produces a shell whose closure includes
    click, rich, pygments, etc. — anything reachable through the dep graph
    that's vaulted.

    Where N>1 vaulted packages contribute the same top-level import name
    (PEP 420 namespace packages), the corresponding `lib/<top>/` is built as
    a real directory of subdir-symlinks rather than a single dir-level
    symlink that would shadow all but one contribution.

    Each pinned package's *.dist-info/ is also linked into lib/, so
    importlib.metadata sees the same set of distributions inside the shell
    that the source vault has on disk.
    """
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    pkgs = _read_manifest(sd)
    conn = db.connect()
    summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    from .. import host
    try:
        closure, missing, conflicts = _resolve_closure(conn, specs, pkgs)
        for m in missing:
            summary["missing"].append(m)
            host.record_failure(
                "shell_pkg_missing", m,
                f"shell={name}; spec did not resolve in vault",
            )
        for pkg_name, existing_info, new_info in conflicts:
            summary["conflicts"].append((pkg_name, existing_info, new_info))
            host.record_failure(
                "shell_version_conflict", pkg_name,
                f"shell={name}; existing={existing_info}; requested={new_info}",
            )

        by_top = _group_by_top_level(conn, closure)
        # Track which (pkg, version, wheel_tag) tuples we've actually linked
        # into a top-level so we don't double-link entry-points / dist-info.
        linked_pkgs: set[tuple[str, str, str]] = set()

        for top_name, contribs in by_top.items():
            if len(contribs) == 1:
                pkg_name, version, wheel_tag, vp = contribs[0]
                linked_names = _link_package(sd / "lib", vp, pkg_name,
                                             version=version, wheel_tag=wheel_tag)
                linked_pkgs.add((pkg_name, version, wheel_tag))
                summary["linked"].append((pkg_name, version, wheel_tag, len(linked_names)))
            else:
                _link_namespace_merge(sd / "lib", top_name, contribs)
                for pkg_name, version, wheel_tag, _vp in contribs:
                    linked_pkgs.add((pkg_name, version, wheel_tag))

        # dist-info + entry points run once per package, regardless of whether
        # the package shipped via single-link or namespace-merge.
        for pkg_name, version, wheel_tag, vp in closure:
            if (pkg_name, version, wheel_tag) not in linked_pkgs:
                continue
            _link_distinfo(sd / "lib", Path(vp))
            scripts = _link_entry_points(sd / "bin", Path(vp),
                                         python=str(sd / "python"))
            store.touch(pkg_name, version, wheel_tag)
            pkgs[pkg_name] = {"version": version, "wheel_tag": wheel_tag}
            summary["scripts"].extend(scripts)

        _write_manifest(sd, name, pkgs)
        _update_scope_in_metadata(conn, name, pkgs)
        conn.execute(
            "UPDATE shells SET last_used_at=? WHERE name=?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()
    finally:
        conn.close()
    return summary


def add_pinned(name: str, pkg_name: str, version: str, wheel_tag: str) -> dict:
    """Add an exactly-pinned (name, version, wheel_tag) to a shell.

    Distinct from `add()`, which takes free-form specs and lets
    `best_version` resolve them. The deployment-manifest path needs the
    triplet to round-trip exactly as written, so this function bypasses
    spec parsing and `best_version` entirely.

    Returns a per-call summary in the same shape as `add()` so the
    caller can aggregate. Errors flow through host.record_failure with
    kinds drawn from FAILURE_KINDS.
    """
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    pkgs = _read_manifest(sd)
    summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    from .. import host

    spec_str = f"{pkg_name}=={version}@{wheel_tag}"
    conn = db.connect()
    try:
        if not store.has(conn, pkg_name, version, wheel_tag):
            summary["missing"].append(spec_str)
            host.record_failure(
                "shell_pkg_missing", spec_str,
                f"shell={name}; exact pin not in vault",
            )
            return summary
        vault_path = store.vault_path_for(pkg_name, version, wheel_tag)
        existing = pkgs.get(pkg_name)
        if existing and (existing["version"] != version
                         or existing["wheel_tag"] != wheel_tag):
            summary["conflicts"].append(
                (pkg_name, existing, {"version": version, "wheel_tag": wheel_tag})
            )
            host.record_failure(
                "shell_version_conflict", pkg_name,
                f"shell={name}; existing={existing}; requested="
                f"{{'version':'{version}','wheel_tag':'{wheel_tag}'}}",
            )
            return summary
        linked = _link_package(sd / "lib", Path(vault_path), pkg_name,
                               version=version, wheel_tag=wheel_tag)
        _link_distinfo(sd / "lib", Path(vault_path))
        scripts = _link_entry_points(sd / "bin", Path(vault_path),
                                     python=str(sd / "python"))
        store.touch(pkg_name, version, wheel_tag)
        pkgs[pkg_name] = {"version": version, "wheel_tag": wheel_tag}
        summary["linked"].append((pkg_name, version, wheel_tag, len(linked)))
        summary["scripts"].extend(scripts)
        _write_manifest(sd, name, pkgs)
        _update_scope_in_metadata(conn, name, pkgs)
        conn.execute(
            "UPDATE shells SET last_used_at=? WHERE name=?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()
    finally:
        conn.close()
    return summary


def remove_packages(name: str, pkgs: list[str]) -> list[str]:
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    manifest = _read_manifest(sd)
    removed = []
    for p in pkgs:
        if p not in manifest:
            continue
        # Unlink whatever we linked; we don't track which entries belonged to
        # which package, so re-derive from the vault path. Both importable
        # top-levels and dist-info dirs are linked in by add()/add_pinned;
        # both must be cleaned up here.
        info = manifest.pop(p)
        vault_path = Path(store.vault_path_for(p, info["version"], info["wheel_tag"]))
        if vault_path.exists():
            for entry in vault_path.iterdir():
                target = sd / "lib" / entry.name
                if target.is_symlink() and Path(os.readlink(target)) == entry:
                    target.unlink()
            for sn, _mod, _attr in _entry_points_for(vault_path):
                ep = sd / "bin" / sn
                if ep.exists():
                    ep.unlink()
        removed.append(p)
    _write_manifest(sd, name, manifest)
    if removed:
        conn = db.connect()
        try:
            _update_scope_in_metadata(conn, name, manifest)
            conn.commit()
        finally:
            conn.close()
    return removed


def list_shells() -> list[dict]:
    conn = db.connect()
    rows = list(conn.execute(
        "SELECT name, created_at, last_used_at, shell_path, python_tag FROM shells"
    ))
    conn.close()
    out = []
    for name, created, used, path, py in rows:
        sd = Path(path)
        manifest = _read_manifest(sd) if sd.exists() else {}
        try:
            size = sum(f.stat().st_size for f in sd.rglob("*") if f.is_file())
        except OSError:
            size = 0
        out.append({
            "name": name,
            "created_at": created,
            "last_used_at": used,
            "path": path,
            "python_tag": py,
            "package_count": len(manifest),
            "size_bytes": size,
        })
    return out


def delete(name: str) -> bool:
    sd = shell_dir(name)
    conn = db.connect()
    conn.execute("DELETE FROM shells WHERE name=?", (name,))
    conn.commit()
    conn.close()
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
        return True
    return False


def exec_in(name: str, cmd: list[str]) -> int:
    """Run a command with the shell's PYTHONPATH/PATH set."""
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")
    env = os.environ.copy()
    lib_path = str(sd / "lib")
    env["PYTHONPATH"] = lib_path + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env["PATH"] = str(sd / "bin") + ":" + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = lib_path + (
        f":{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") else "")
    env["DYLD_LIBRARY_PATH"] = lib_path + (
        f":{env['DYLD_LIBRARY_PATH']}" if env.get("DYLD_LIBRARY_PATH") else "")
    env["PKG_CONFIG_PATH"] = f"{lib_path}/pkgconfig:{lib_path}" + (
        f":{env['PKG_CONFIG_PATH']}" if env.get("PKG_CONFIG_PATH") else "")
    env["BUBBLE_SHELL"] = name
    env["BUBBLE_SHELL_DIR"] = str(sd)

    # Update last_used
    conn = db.connect()
    conn.execute(
        "UPDATE shells SET last_used_at=? WHERE name=?",
        (datetime.now().isoformat(), name),
    )
    conn.commit()
    conn.close()
    return subprocess.call(cmd, env=env)


def shell_enter(name: str, shell_exe: Optional[str] = None) -> int:
    """Spawns an interactive shell with the bubble's environment variables and prompt override loaded."""
    sd = shell_dir(name)
    if not sd.exists():
        raise FileNotFoundError(f"shell does not exist: {name}")

    env = os.environ.copy()
    lib_path = str(sd / "lib")
    env["PYTHONPATH"] = lib_path + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env["PATH"] = str(sd / "bin") + ":" + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = lib_path + (
        f":{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") else "")
    env["DYLD_LIBRARY_PATH"] = lib_path + (
        f":{env['DYLD_LIBRARY_PATH']}" if env.get("DYLD_LIBRARY_PATH") else "")
    env["PKG_CONFIG_PATH"] = f"{lib_path}/pkgconfig:{lib_path}" + (
        f":{env['PKG_CONFIG_PATH']}" if env.get("PKG_CONFIG_PATH") else "")
    env["BUBBLE_SHELL"] = name
    env["BUBBLE_SHELL_DIR"] = str(sd)

    # Prompt customization
    old_ps1 = env.get("PS1", "\\u@\\h:\\w\\$ ")
    env["PS1"] = f"(bubble:{name}) {old_ps1}"

    if not shell_exe:
        shell_exe = env.get("SHELL") or shutil.which("bash") or "/bin/sh"

    # Execute interactive sub-shell
    return subprocess.call([shell_exe], env=env)


def discover_shell_for(start: Path) -> Optional[str]:
    """Walk up from `start` looking for a `.bubble-shell` file.

    The file's first non-comment line is the shell name. Returns None if
    none found (or the named shell doesn't exist).
    """
    here = start.resolve()
    while True:
        marker = here / ".bubble-shell"
        if marker.exists():
            for line in marker.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    if shell_dir(line).exists():
                        return line
                    return None
        if here.parent == here:
            return None
        here = here.parent
