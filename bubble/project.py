"""Project-level ingestion — scan a directory, create a named shell.

Pipeline
--------
1. Walk <project_dir>/**/*.py via scanner.scan_dir, merging all imports.
2. Resolve the non-stdlib, non-local import set against the vault
   (scanner.resolver.resolve).
3. Optionally fetch still-missing packages from PyPI (--fetch).
4. Create or update a named shell pinned to the resolved closure.
5. Save the shell scope to metadata so BUBBLE_SHELL-aware import
   resolution always enforces the project's exact versions.
6. Write a .bubble-shell marker in the project root so
   shell.discover_shell_for() can find the project automatically.

Spinoff / parent projects
-------------------------
Pass parent=<shell-name> to inherit packages that are not overridden
by this project's own resolution.  On import, the meta-finder walks the
parent chain (shell.load_parent) so a spinoff gets its parent's pinned
version for any package not in its own scope, while remaining free to
pin a different version for packages it does own.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def ingest(
    project_dir: Path,
    shell_name: str,
    *,
    fetch: bool = False,
    overwrite: bool = False,
    parent: Optional[str] = None,
    verbose: bool = False,
) -> dict:
    """Scan *project_dir*, resolve deps, create / update a named shell.

    Returns a summary dict::

        {
          "scanned_files": int,          # .py files successfully scanned
          "scan_errors":  [(path, msg)], # files that could not be parsed
          "resolved":     {dist: Resolved},
          "missing_imports": [str],      # distributions not found in vault
          "linked":    [(pkg, ver, tag, n)],
          "scripts":   [str],
          "missing":   [str],            # from add() — vault misses
          "conflicts": [(pkg, old, new)],
          "shell_dir": Path,
        }
    """
    from .scanner.py import scan_dir
    from .scanner.resolver import resolve, fetch_missing
    from .run import shell as shell_mod
    from .vault import db

    project_dir = Path(project_dir).resolve()
    if not project_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {project_dir}")

    # 1. Scan
    merged, scan_errors = scan_dir(project_dir)

    # Universal Polyglot Ingestion Scanning
    from .scanner.universal import scan_project
    pset = scan_project(project_dir)

    if verbose:
        import sys
        print(f"  scanned {sum(1 for _ in project_dir.rglob('*.py'))} .py files, "
              f"{len(merged.top_level_imports)} external imports", file=sys.stderr)
        if pset.is_polyglot():
            print(f"  detected polyglot assets: c/cpp={len(pset.c_cpp_files)}, "
                  f"rust={len(pset.rust_manifests)}, bash={len(pset.bash_scripts)}, "
                  f"binaries={len(pset.binaries)}", file=sys.stderr)

    # 2. Resolve
    plan = resolve(merged)

    # 3. Optionally fetch missing
    if fetch and plan.missing:
        if verbose:
            import sys
            print(f"  fetching {len(plan.missing)} missing: "
                  f"{', '.join(sorted(plan.missing))}", file=sys.stderr)
        plan = fetch_missing(plan)

    db.init_db()

    # 4. Create or update shell
    sd = shell_mod.create(shell_name, [], exist_ok=overwrite, parent=parent)
    shell_bin = sd / "bin"
    shell_bin.mkdir(parents=True, exist_ok=True)

    # Compile and import C/C++ files
    for c_file in pset.c_cpp_files:
        if c_file.suffix.lower() in (".c", ".cpp", ".cc"):
            from .run.builder import build_c_cpp
            try:
                build_c_cpp(c_file, shell_bin / c_file.stem)
            except Exception as exc:
                scan_errors.append((c_file, f"Polyglot compilation error: {exc}"))

    # Compile and import Rust manifests (Cargo)
    for cargo_toml in pset.rust_manifests:
        from .run.builder import build_rust_cargo
        try:
            build_rust_cargo(cargo_toml, shell_bin)
        except Exception as exc:
            scan_errors.append((cargo_toml, f"Polyglot Cargo build error: {exc}"))

    # Symlink and permission Bash scripts
    for sh_script in pset.bash_scripts:
        dest = shell_bin / sh_script.name
        try:
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            rel_target = os.path.relpath(sh_script, start=dest.parent)
            os.symlink(rel_target, dest)
            # Make sure it's executable
            sh_script.chmod(sh_script.stat().st_mode | 0o111)
        except Exception as exc:
            scan_errors.append((sh_script, f"Bash linking error: {exc}"))

    # Copy/Symlink and Isolate Binary Executables
    for binary in pset.binaries:
        dest = shell_bin / binary.name
        import shutil
        try:
            shutil.copy2(binary, dest)
            dest.chmod(dest.stat().st_mode | 0o111)
            # Link-patch for user-space hermeticity
            from .tools.elf import patch_elf_binary
            from .tools.macho import patch_macho_binary
            try:
                patch_elf_binary(dest, rpath="$ORIGIN/../lib")
            except Exception:
                pass
            try:
                patch_macho_binary(dest, rpath_changes=[("@loader_path/../lib", "@loader_path/../lib")])
            except Exception:
                pass
        except Exception as exc:
            scan_errors.append((binary, f"Binary import error: {exc}"))

    # Build pinned specs from the resolved plan and add them.
    specs = [
        f"{r.distribution}=={r.version}"
        for r in plan.resolved.values()
    ]
    if specs:
        summary = shell_mod.add(shell_name, specs)
    else:
        summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}

    # Scope is synced automatically by add(); nothing extra needed.

    # 5. Write .bubble-shell marker
    marker = project_dir / ".bubble-shell"
    if not marker.exists() or overwrite:
        marker.write_text(f"# bubble project shell\n{shell_name}\n")

    summary["scanned_files"] = sum(1 for _ in project_dir.rglob("*.py"))
    summary["scan_errors"] = [(str(p), msg) for p, msg in scan_errors]
    summary["resolved"] = plan.resolved
    summary["missing_imports"] = plan.missing
    summary["shell_dir"] = sd

    return summary


def freeze(shell_name: str, output: Path) -> None:
    """Write the current shell state as a deployment manifest.

    This is the inverse of `bubble shell create --from`: given a
    live shell, emit a manifest that can reproduce it elsewhere.
    Aliases stored in metadata are included.
    """
    from . import manifest as manifest_mod
    from .run import shell as shell_mod
    from .vault import db

    db.init_db()
    sd = shell_mod.shell_dir(shell_name)
    m = manifest_mod.from_shell(sd)

    # Re-attach aliases from metadata if present.
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (shell_name,),
        ).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        import json
        blob = json.loads(row[0])
        for alias, info in blob.get("aliases", {}).items():
            m.aliases[alias] = manifest_mod.AliasPin(
                name=info["name"],
                version=info["version"],
                wheel_tag=info["wheel_tag"],
                substrate=info.get("substrate"),
            )

    manifest_mod.dump(m, output)
