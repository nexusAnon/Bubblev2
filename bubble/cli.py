"""bubble CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import config
from . import bridge
from . import term
from . import shims
from .vault import db, store, importer
from .run import shell as shell_mod


# ─────────────────────────── command handlers ────────────────────────────


def cmd_vault_list(args: argparse.Namespace) -> int:
    db.init_db()
    rows = store.list_all()
    if not rows:
        print("vault is empty")
        return 0
    print(f"{'NAME':<28} {'VERSION':<14} {'TAG':<46} {'TYPE':<6} {'SOURCE':<14}")
    print("─" * 110)
    for name, version, tag, native, source, _, _ in rows:
        ptype = "native" if native else "pure"
        print(f"{name:<28} {version:<14} {tag:<46} {ptype:<6} {source or '':<14}")
    print(f"\n{len(rows)} packages")
    return 0


def cmd_vault_import_venv(args: argparse.Namespace) -> int:
    db.init_db()
    site_packages = Path(args.path).resolve()
    if site_packages.is_file():
        print(f"error: {site_packages} is a file; pass a site-packages directory", file=sys.stderr)
        return 1
    # If user passed a venv root, drill in
    if not (site_packages.name == "site-packages" or any(
            site_packages.glob("*.dist-info"))):
        candidates = list(site_packages.glob("lib/python*/site-packages"))
        if candidates:
            site_packages = candidates[0]
            print(f"using {site_packages}")
    print(f"importing {site_packages}")
    skip = set(args.skip or [])
    result = importer.import_site_packages(
        site_packages, hardlink=args.hardlink, overwrite=args.overwrite, skip=skip,
    )
    print(f"\n  imported:       {result['imported']}")
    print(f"  skipped:        {result['skipped']}  {result.get('skipped_names', [])[:8]}")
    print(f"  missing RECORD: {result['missing_record']}")
    print(f"  errors:         {result['errors']}")
    if args.verbose:
        for entry in result["entries"]:
            print(" ", entry)
    return 0 if result["errors"] == 0 else 2


def cmd_vault_audit_fs(args: argparse.Namespace) -> int:
    db.init_db()
    root = Path(args.root).resolve()
    print(f"scanning {root} for venvs...")
    venvs = []
    for marker in root.rglob("pyvenv.cfg"):
        venv = marker.parent
        sps = list(venv.glob("lib/python*/site-packages"))
        if not sps:
            continue
        sp = sps[0]
        try:
            size = sum(f.stat().st_size for f in sp.rglob("*") if f.is_file())
        except OSError:
            size = 0
        venvs.append((venv, sp, size))

    if not venvs:
        print("no venvs found")
        return 0

    pkg_locations: dict[str, list[tuple[str, str, Path]]] = {}
    from .vault.metadata import name_version_from_dist_info, derive_wheel_tag_from_dist_info
    for venv, sp, _size in venvs:
        for di in sp.glob("*.dist-info"):
            nv = name_version_from_dist_info(di)
            if not nv:
                continue
            name, version = nv
            tag, *_ = derive_wheel_tag_from_dist_info(di)
            pkg_locations.setdefault(name.lower(), []).append((version, tag, di))

    # Cross-check what's already in vault
    conn = db.connect()
    in_vault = {(name.lower(), version, tag): vault_path
                for name, version, tag, _, _, _, vault_path in store.list_all(conn)}
    conn.close()

    print(f"\n=== venvs ({len(venvs)}) ===")
    total = 0
    for venv, sp, size in sorted(venvs, key=lambda x: -x[2]):
        total += size
        print(f"  {size/1e6:>8.1f}MB  {venv}")
    print(f"\n  total site-packages: {total/1e6:.1f}MB")

    dup_waste = 0
    print(f"\n=== duplicates ({sum(1 for v in pkg_locations.values() if len(v) > 1)}) ===")
    for name, locs in sorted(pkg_locations.items()):
        if len(locs) <= 1:
            continue
        # Compute waste: total RECORD-listed bytes minus largest single
        sizes = []
        for ver, tag, di in locs:
            try:
                bytes_ = sum((di.parent / Path(row.split(",")[0])).stat().st_size
                             for row in (di / "RECORD").read_text().splitlines()
                             if row and ".." not in row.split(",")[0])
            except (OSError, IndexError):
                bytes_ = 0
            sizes.append((ver, tag, bytes_, di))
        waste = (sum(s[2] for s in sizes) - max(s[2] for s in sizes))
        dup_waste += waste
        print(f"  {name}: {len(locs)} copies, dup_waste={waste/1e6:.1f}MB")
        for ver, tag, b, di in sizes:
            in_v = "✓" if (name, ver, tag) in in_vault else " "
            print(f"      {in_v} {ver:<14} {tag:<32} {b/1e6:>6.1f}MB  {di.parent}")

    print(f"\n  total dup waste: {dup_waste/1e6:.1f}MB")

    safely_removable = 0
    print(f"\n=== already in vault (safely removable from venv) ===")
    for name, locs in sorted(pkg_locations.items()):
        for ver, tag, di in locs:
            if (name, ver, tag) in in_vault:
                safely_removable += 1
                print(f"  {name:<28} {ver:<14} {tag:<32}  {di.parent}")
    print(f"\n  {safely_removable} dist-info entries safely removable\n")
    return 0


def cmd_vault_remove(args: argparse.Namespace) -> int:
    db.init_db()
    ok = store.remove(args.name, args.version, args.tag)
    print("removed" if ok else "not found")
    return 0 if ok else 1


def cmd_shell_create(args: argparse.Namespace) -> int:
    db.init_db()
    if args.from_manifest:
        return _shell_create_from_manifest(args)
    parent = getattr(args, "parent", None)
    sd = shell_mod.create(args.name, args.specs or [], exist_ok=args.exist_ok,
                          parent=parent)
    print(f"created shell '{args.name}' at {sd}")
    if parent:
        print(f"  parent: {parent}")
    if args.specs:
        print(f"  added: {len(args.specs)} package specs")
    return 0


def _shell_create_from_manifest(args: argparse.Namespace) -> int:
    """Read a deployment manifest, fetch any missing pins (when --fetch
    is set), and link the shell. Each pin verifies through C1's
    verify-on-link before becoming an entry."""
    from . import manifest as manifest_mod
    manifest_path = Path(args.from_manifest).resolve()
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    m = manifest_mod.load(manifest_path)

    if not m.packages and not m.aliases:
        print(f"error: manifest is empty (no [packages] or [aliases])",
              file=sys.stderr)
        return 1

    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))
    if autofetch:
        from .vault import fetcher
        for pkg, (version, wheel_tag) in m.packages.items():
            conn = db.connect()
            present = store.has(conn, pkg, version, wheel_tag)
            conn.close()
            if present:
                continue
            print(f"  fetching {pkg}=={version} ({wheel_tag}) ...")
            try:
                fetcher.fetch_into_vault(pkg, pinned_version=version)
            except (RuntimeError, ValueError) as exc:
                print(f"  fetch refused: {exc}", file=sys.stderr)
                return 2

    parent = getattr(args, "parent", None)
    sd = shell_mod.create(args.name, [], exist_ok=args.exist_ok, parent=parent)
    if args.name == m.name or m.name is None:
        pass  # name agrees or unspecified
    elif m.name and m.name != args.name:
        print(f"  note: manifest names shell {m.name!r}; using CLI arg {args.name!r}",
              file=sys.stderr)

    total_summary = {"linked": [], "scripts": [], "missing": [], "conflicts": []}
    for pkg, (version, wheel_tag) in m.packages.items():
        s = shell_mod.add_pinned(args.name, pkg, version, wheel_tag)
        for k in total_summary:
            total_summary[k].extend(s[k])

    # Record aliases (and re-affirm parent if set) into shell metadata.
    # Scope is already kept in sync by add_pinned(); we only need an
    # extra write here for the aliases section.
    if m.aliases or parent:
        conn = db.connect()
        row = conn.execute(
            "SELECT metadata FROM shells WHERE name=?", (args.name,),
        ).fetchone()
        meta_blob = json.loads(row[0]) if row and row[0] else {}
        if m.aliases:
            meta_blob["aliases"] = {
                alias: {
                    "name": pin.name,
                    "version": pin.version,
                    "wheel_tag": pin.wheel_tag,
                    "substrate": pin.substrate,
                } for alias, pin in m.aliases.items()
            }
        if parent:
            meta_blob["parent"] = parent
        conn.execute(
            "UPDATE shells SET metadata=? WHERE name=?",
            (json.dumps(meta_blob), args.name),
        )
        conn.commit()
        conn.close()

    print(f"created shell '{args.name}' at {sd}")
    print(f"  manifest: {manifest_path}")
    print(f"  linked:    {len(total_summary['linked'])}")
    if total_summary["missing"]:
        print(f"  MISSING:   {len(total_summary['missing'])} "
              f"(rerun with --fetch to pull from PyPI, or use "
              f"`bubble vault import-venv` to populate from a trusted venv)")
        for s in total_summary["missing"]:
            print(f"    × {s}")
    if total_summary["conflicts"]:
        print(f"  CONFLICTS: {len(total_summary['conflicts'])}")
        for pkg, existing, requested in total_summary["conflicts"]:
            print(f"    × {pkg}: shell pinned to {existing}, manifest asks {requested}")
    if m.aliases:
        print(f"  aliases:   {len(m.aliases)} recorded "
              f"(substrate routing not yet wired; see `bubble host`)")
    if total_summary["missing"] or total_summary["conflicts"]:
        return 2
    return 0


def cmd_project_ingest(args: argparse.Namespace) -> int:
    """bubble project ingest <dir> --name NAME

    Scan every .py file under <dir>, resolve external imports against the
    vault, and create (or update) a named shell whose scope is pinned to
    exactly those versions.  On subsequent runs the shell's BUBBLE_SHELL
    env var makes VaultFinder enforce the pinned versions for that project.
    """
    from . import project as project_mod
    db.init_db()
    project_dir = Path(args.dir).resolve()
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 1

    parent = getattr(args, "parent", None)
    try:
        summary = project_mod.ingest(
            project_dir,
            args.name,
            fetch=bool(getattr(args, "fetch", False)) or bool(os.environ.get("BUBBLE_AUTOFETCH")),
            overwrite=bool(getattr(args, "overwrite", False)),
            parent=parent,
            verbose=getattr(args, "verbose", False),
        )
    except (FileExistsError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"project shell '{args.name}' ← {project_dir}")
    print(f"  scanned:  {summary['scanned_files']} .py files")

    resolved = summary.get("resolved") or {}
    if resolved:
        print(f"  resolved: {len(resolved)} distributions")

    if summary.get("missing_imports"):
        print(f"  missing from vault: {', '.join(sorted(summary['missing_imports']))}")
        if not getattr(args, "fetch", False):
            print("  (re-run with --fetch to pull from PyPI)")

    for pkg, ver, tag, n in summary.get("linked") or []:
        print(f"    + {pkg:<28} {ver:<14} {tag}")
    if summary.get("scripts"):
        print(f"  scripts: {', '.join(summary['scripts'])}")
    if summary.get("conflicts"):
        for pkg, old, new in summary["conflicts"]:
            print(f"  CONFLICT {pkg}: shell has {old}, scan wants {new}")
        return 3
    if summary.get("scan_errors"):
        for path, msg in summary["scan_errors"]:
            print(f"  parse error: {path}: {msg}", file=sys.stderr)

    if parent:
        print(f"  parent: {parent}  (scope inherited for unlisted packages)")
    print(f"  shell:    {summary['shell_dir']}")
    print(f"  marker:   {project_dir / '.bubble-shell'}")
    return 0 if not summary.get("missing_imports") else 2


def cmd_keep_capture(args: argparse.Namespace) -> int:
    """bubble keep capture <dir> [--name NAME]

    Absorb a directory tree into the vault. The vault holds the live
    tree from now on; the original path becomes a symlink pointing into
    the vault unless --no-symlink-back is passed.
    """
    from . import keep as keep_mod
    config.ensure_dirs()
    source = Path(args.dir).resolve()
    if not source.is_dir():
        print(f"error: not a directory: {source}", file=sys.stderr)
        return 1
    try:
        meta = keep_mod.capture(
            source,
            args.name,
            symlink_back=not args.no_symlink_back,
            overwrite=bool(args.overwrite),
        )
    except (FileExistsError, NotADirectoryError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dest = config.KEEP_DIR / meta["name"]
    print(f"kept '{meta['name']}'  ←  {source}")
    print(f"  vault:    {dest}")
    if meta["symlinks"]:
        for s in meta["symlinks"]:
            arrow = dest if s["to"] == "." else dest / s["to"]
            print(f"  symlink:  {s['from']}  →  {arrow}")
    else:
        print(f"  symlink:  (none — source left in place)")
    return 0


def cmd_keep_list(args: argparse.Namespace) -> int:
    """bubble keep list — inventory of vaulted trees and file-sets."""
    from . import keep as keep_mod
    items = keep_mod.list_keeps()
    if not items:
        print("(no keeps)")
        return 0
    for m in items:
        size = m.get("_size_bytes", 0)
        size_str = f"{size / 1024:.1f} KiB" if size < 1_048_576 \
            else f"{size / 1_048_576:.2f} MB"
        sources = ", ".join(s["from"] for s in m.get("symlinks", []))
        if not sources:
            sources = "(unwired)"
        print(f"  · {m['name']:<24}  {m['kind']:<5}  {size_str:>9}  "
              f"{m['captured_at']}  ←  {sources}")
    return 0


def cmd_keep_show(args: argparse.Namespace) -> int:
    """bubble keep show <name> — print meta for one keep."""
    from . import keep as keep_mod
    try:
        m = keep_mod.show(args.name)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  name:        {m['name']}")
    print(f"  kind:        {m['kind']}")
    print(f"  captured:    {m['captured_at']}")
    print(f"  vault path:  {config.KEEP_DIR / m['name']}")
    size = m.get("_size_bytes", 0)
    print(f"  size:        "
          f"{size / 1024:.1f} KiB" if size < 1_048_576
          else f"  size:        {size / 1_048_576:.2f} MB")
    for s in m.get("symlinks", []):
        print(f"  symlink:     {s['from']}  →  {s['to']}")
    return 0


def cmd_keep_wire(args: argparse.Namespace) -> int:
    """bubble keep wire <name> — recreate source-path symlinks from meta."""
    from . import keep as keep_mod
    try:
        result = keep_mod.wire(args.name)
    except (FileNotFoundError, FileExistsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result["wired"]:
        print(f"'{args.name}': nothing to wire (symlinks already in place)")
        return 0
    for s in result["wired"]:
        print(f"  wired:  {s['from']}  →  {s['to']}")
    return 0


def cmd_keep_unwire(args: argparse.Namespace) -> int:
    """bubble keep unwire <name> — remove source-path symlinks; leave vault."""
    from . import keep as keep_mod
    try:
        result = keep_mod.unwire(args.name)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result["unwired"]:
        print(f"'{args.name}': nothing to unwire")
        return 0
    for s in result["unwired"]:
        print(f"  unwired:  {s}")
    return 0


def cmd_keep_activate(args: argparse.Namespace) -> int:
    """bubble keep activate <name> — symlink bin/ executables into ~/.local/bin/."""
    from . import keep as keep_mod
    try:
        result = keep_mod.activate(args.name)
    except (FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in result["activated"]:
        print(f"  on PATH:  {path}")
    for s in result["skipped"]:
        print(f"  skipped:  {s['path']}  ({s['reason']})")
    if not result["activated"] and not result["skipped"]:
        print(f"'{args.name}': no executables in bin/")
    return 0


def cmd_keep_deactivate(args: argparse.Namespace) -> int:
    """bubble keep deactivate <name> — remove the ~/.local/bin/ symlinks."""
    from . import keep as keep_mod
    try:
        result = keep_mod.deactivate(args.name)
    except (FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result["deactivated"]:
        print(f"'{args.name}': nothing to deactivate")
        return 0
    for path in result["deactivated"]:
        print(f"  removed:  {path}")
    return 0


def cmd_keep_remove(args: argparse.Namespace) -> int:
    """bubble keep remove <name> — unwire, deactivate, delete from vault."""
    from . import keep as keep_mod
    try:
        keep_mod.remove(args.name)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"removed '{args.name}'")
    return 0


def cmd_shell_freeze(args: argparse.Namespace) -> int:
    """bubble shell freeze <name> -o <manifest> — snapshot shell as manifest."""
    from . import project as project_mod
    db.init_db()
    out = Path(args.output).resolve()
    try:
        project_mod.freeze(args.name, out)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  froze '{args.name}' → {out}")
    return 0


def cmd_shell_add(args: argparse.Namespace) -> int:
    db.init_db()
    summary = shell_mod.add(args.name, args.specs)
    for pkg, ver, tag, n in summary["linked"]:
        print(f"  + {pkg:<28} {ver:<14} {tag:<32}  ({n} entries)")
    if summary["scripts"]:
        print(f"  scripts: {', '.join(summary['scripts'])}")
    if summary["missing"]:
        print(f"  MISSING from vault: {', '.join(summary['missing'])}")
        return 2
    if summary["conflicts"]:
        for pkg, existing, new in summary["conflicts"]:
            print(f"  CONFLICT {pkg}: shell has {existing}, requested {new}")
        return 3
    return 0


def cmd_shell_remove(args: argparse.Namespace) -> int:
    db.init_db()
    removed = shell_mod.remove_packages(args.name, args.pkgs)
    print(f"unlinked: {', '.join(removed) if removed else '(nothing)'}")
    return 0


def cmd_shell_list(args: argparse.Namespace) -> int:
    db.init_db()
    shells = shell_mod.list_shells()
    if not shells:
        print("no shells")
        return 0
    print(f"{'NAME':<20} {'PYTHON':<8} {'PKGS':<6} {'SIZE':<10} {'PATH'}")
    print("─" * 90)
    for s in shells:
        print(f"{s['name']:<20} {s['python_tag'] or '':<8} {s['package_count']:<6} "
              f"{s['size_bytes']/1024:>8.1f}KB  {s['path']}")
    return 0


def cmd_shell_delete(args: argparse.Namespace) -> int:
    db.init_db()
    ok = shell_mod.delete(args.name)
    print("deleted" if ok else "not found")
    return 0 if ok else 1


def cmd_shell_exec(args: argparse.Namespace) -> int:
    db.init_db()
    return shell_mod.exec_in(args.name, args.cmd)


def cmd_shell_enter(args: argparse.Namespace) -> int:
    db.init_db()
    return shell_mod.shell_enter(args.name)


def cmd_host(args: argparse.Namespace) -> int:
    """bubble host — show what bubble knows about this machine.

    The consult side of the probe→consult→record loop. Reads ~/.bubble/host.toml
    that the probe wrote, plus any failures recorded by the runtime since.
    """
    from . import host
    portrait = host.load()
    if not portrait:
        print("no host portrait yet — run `bubble probe` to write one")
        return 1

    print(f"  probed_at:      {portrait.get('probed_at', '-')}")
    print(f"  bubble_version: {portrait.get('bubble_version', '-')}")
    if "kernel" in portrait:
        k = portrait["kernel"]
        print(f"  kernel:         {k.get('system','?')} {k.get('release','?')} {k.get('machine','?')}")
    if "libc" in portrait:
        l = portrait["libc"]
        print(f"  libc:           {l.get('variant','?')} {l.get('version','')}")
    print()
    print("  substrates:")
    for s in portrait.get("substrates", []):
        cost = f"~{s['cost_mb']}MB" if s.get("cost_mb") else "n/a"
        print(f"    • {s.get('name',''):<22} cost={cost:<8} {s.get('status','')}")
    failures = portrait.get("failures", [])
    if failures:
        print()
        print(f"  observed failures ({len(failures)}):")
        for f in failures[-10:]:  # last 10
            print(f"    × [{f.get('kind','?')}] {f.get('target','?')}")
            if f.get("detail"):
                print(f"      {f['detail'][:140]}")
    else:
        print()
        print("  no runtime failures recorded yet")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """bubble setup — zero-flag bootstrap.

    Probes the host, scans every site-packages this Python install knows
    about, imports everything into the vault (hardlink by default, falling
    back to copy on cross-fs). Idempotent: re-running after `pip install`
    only adds the new entries.
    """
    from . import probe
    import site as _site
    import sys as _sys

    first_run = not config.VAULT_DB.exists()
    config.ensure_dirs()
    db.init_db()

    if first_run:
        term.out()
        term.out(f"  {term.bold('◉ bubble')}  {term.dim('first run — getting set up')}")
        term.out()

    # 1. Probe — host portrait + substrate menu.
    term.out(f"  {term.dim('◎')} probing host...")
    results = probe.run_all()
    out_path = probe.host_toml_path()
    probe.write(out_path, results)
    py = results["python"]
    k = results["kernel"]
    py_line = f"python {py['version']}  {k['system']} {k['machine']}"
    term.out(f"      {term.dim(py_line)}")
    avail = [s["name"] for s in results["substrates"]
             if s.get("status", "").startswith("available")]
    if avail:
        term.out(f"      {term.dim('substrates: ' + ', '.join(avail))}")

    # 2. Apply shims so first-run HTTPS works on Termux/Alpine/proot.
    shim_rpt = shims.apply()
    if shim_rpt.applied:
        term.out(f"  {term.dim('◎')} ssl certs: {term.dim(str(shim_rpt.cert_file))}")
    elif shim_rpt.gaps and first_run:
        term.out(f"  {term.amber('⚠')} ssl certs not found  "
                 f"{term.dim('(HTTPS may fail; `bubble doctor` for detail)')}")

    # 3. Discover every site-packages on this interpreter's view.
    candidates: set[Path] = set()
    for p in _sys.path:
        if "packages" in p:
            pp = Path(p)
            if pp.is_dir():
                candidates.add(pp.resolve())
    for p in _site.getsitepackages():
        pp = Path(p)
        if pp.is_dir():
            candidates.add(pp.resolve())

    # Don't recurse into our own vault tree.
    bubble_home = Path(config.BUBBLE_HOME).resolve()
    candidates = {p for p in candidates
                  if bubble_home not in p.parents and p != bubble_home}

    if not candidates:
        term.out()
        term.out(f"  {term.dim('no site-packages directories on this interpreter path.')}")
        term.out(f"  {term.green('◉')} vault ready: {term.dim(str(bubble_home))}")
        return 0

    # 4. Import each, hardlinking by default (falls back to copy on EXDEV).
    term.out()
    sp_word = "directory" if len(candidates) == 1 else "directories"
    term.out(f"  {term.dim('◎')} scanning {len(candidates)} site-packages {sp_word}...")
    totals = {"imported": 0, "skipped": 0, "errors": 0, "missing_record": 0}
    for sp in sorted(candidates):
        n_dists = len(list(sp.glob("*.dist-info")))
        term.out(f"      {term.dim(str(sp))}  {term.dim(f'({n_dists} dists)')}")
        r = importer.import_site_packages(sp, hardlink=True, overwrite=False)
        for k_ in totals:
            totals[k_] += r.get(k_, 0)

    # 5. Report what's ready, then a tiny menu.
    term.out()
    term.out(f"  {term.green('◉')} vault ready: {term.dim(str(bubble_home))}")
    term.out(f"      {term.dim('imported now:    ')}{totals['imported']}")
    term.out(f"      {term.dim('already vaulted: ')}{totals['skipped']}")
    if totals["missing_record"]:
        term.out(f"      {term.dim('no RECORD file:  ')}{totals['missing_record']}  "
                 f"{term.dim('(silent skip)')}")
    if totals["errors"]:
        term.out(f"      {term.amber('errors:          ')}{totals['errors']}")

    term.out()
    term.out(f"  {term.cyan('bubble run <script.py>'):<48}  {term.dim('run a script')}")
    term.out(f"  {term.cyan('bubble vault get <pkg>'):<48}  {term.dim('pre-cache a package')}")
    term.out(f"  {term.cyan('bubble status'):<48}  {term.dim('what is in the vault')}")
    term.out(f"  {term.cyan('bubble doctor'):<48}  {term.dim('diagnose environment')}")
    term.out(f"  {term.cyan('bubble preflight <script.py>'):<48}  {term.dim('offline-readiness check')}")
    term.out()
    return 0 if totals["errors"] == 0 else 2


def cmd_probe(args: argparse.Namespace) -> int:
    """bubble probe — interrogate the machine, write host.toml."""
    from . import probe
    config.ensure_dirs()
    results = probe.run_all()
    out_path = probe.host_toml_path()
    probe.write(out_path, results)
    if args.show:
        print(out_path.read_text())
    else:
        # Brief summary
        sub_names = [s["name"] + (f" [{s['status']}]") for s in results["substrates"]]
        print(f"  wrote {out_path}")
        print(f"  kernel:    {results['kernel']['system']} {results['kernel']['release']} {results['kernel']['machine']}")
        print(f"  libc:      {results['libc'].get('variant')} {results['libc'].get('version', '')}")
        print(f"  python:    {results['python']['version']} ({results['python']['executable']})")
        print(f"  shared:    {results['python']['shared']}")
        print(f"  dlmopen:   {results['dlmopen'].get('available')}")
        print(f"  embed:     {results['libpython_embeddable'].get('embeddable')}")
        print(f"  sub-int:   {results['subinterpreters'].get('available')}")
        print(f"  substrates available:")
        for s in results["substrates"]:
            cost = f"~{s['cost_mb']}MB" if s.get("cost_mb") is not None else "n/a"
            print(f"    • {s['name']:<22} cost={cost:<8} {s['status']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """bubble run <target> — dispatch on shape.

    `.py` script  → demand-paged Python execution through the meta-finder.
    path to file  → exec it directly (sh/binary/etc), passing args through.
    bare name     → resolve against vaulted keeps (looks at bin/<name>,
                    then a single executable in bin/, then top-level
                    <name>) and exec.

    Network is opt-in (Python path only): pass --fetch or set
    BUBBLE_AUTOFETCH=1 to allow vault misses to escalate to PyPI.
    """
    raw = args.script
    raw_path = Path(raw)

    # Non-Python dispatch: explicit file path or bare keep name.
    if not raw.endswith(".py"):
        target = None
        if raw_path.exists():
            target = raw_path.resolve()
        else:
            from . import keep as keep_mod
            target = keep_mod.resolve_executable(raw)
        if target is None:
            term.err(f"  {term.red('✗')} cannot resolve '{raw}' as file or keep")
            return 1
        if not os.access(target, os.X_OK):
            term.err(f"  {term.red('✗')} not executable: {target}")
            return 1
        # Hand the process to the target. argv[0] is the target path so
        # the program sees its real name.
        os.execv(str(target), [str(target), *(args.args or [])])
        return 1  # unreachable; execv replaces the process

    # Python path.
    db.init_db()
    script = raw_path.resolve()
    if not script.exists():
        term.err(f"  {term.red('✗')} not found: {script}")
        return 1

    # SSL on Termux/proot/Alpine — keep HTTPS working without further config.
    shims.apply()

    if term.is_tty() and not term.is_quiet():
        term.out()
        term.out(f"  {term.dim('◎')} {term.cyan(script.name)}")

    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))

    # Pre-flight: only on a TTY, only when not skipped, only when not already
    # granting network (autofetch handles missing deps via the meta-finder).
    if (term.is_tty() and not term.is_quiet()
            and not getattr(args, "no_preflight", False)
            and not autofetch):
        from .scanner import py as scanner_py, resolver as resolver_mod
        try:
            iset = scanner_py.scan(script)
        except ValueError:
            iset = None
        if iset is not None:
            plan = resolver_mod.resolve(iset)
            if plan.missing:
                term.out(f"  {term.amber('⚠')} missing: "
                         f"{', '.join(sorted(plan.missing))}")
                if term.prompt_yn("Fetch from PyPI and cache?"):
                    from .vault import fetcher
                    for pkg in sorted(plan.missing):
                        term.out(f"  {term.dim('◎')} fetching {term.amber(pkg)} ...")
                        try:
                            fetcher.fetch_into_vault(pkg)
                        except (RuntimeError, ValueError, FileNotFoundError) as exc:
                            term.out(f"      {term.dim('→')} "
                                     f"could not pre-fetch {pkg}: {term.dim(str(exc))}")
                    autofetch = True  # let the finder fall through for any still-missing transitives

    from .meta_finder import install, _load_scope, _load_aliases
    scope = aliases = None
    if args.scope:
        scope = _load_scope(Path(args.scope))
        aliases = _load_aliases(Path(args.scope))
    finder = install(
        scope=scope or None,
        aliases=aliases or None,
        autofetch=autofetch,
        verbose=args.verbose,
    )

    # Execute the script in-process so the finder traps every import
    # Strip system site-packages so the vault is the only source
    if args.isolate:
        sys.path = [p for p in sys.path
                    if "dist-packages" not in p and "site-packages" not in p]

    sys.argv = [str(script), *(args.args or [])]
    code = compile(script.read_text(), str(script), "exec")
    globs = {"__name__": "__main__", "__file__": str(script)}

    try:
        exec(code, globs)
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 1
    else:
        rc = 0

    if args.lock:
        _write_lockfile(Path(args.lock), finder.hits)
        if args.verbose:
            term.err(f"  wrote lockfile: {args.lock} ({len(finder.hits)} entries)")
    return rc


def cmd_bridge(args: argparse.Namespace) -> int:
    """bubble bridge <script> — orchestrate main + legacy runners."""
    return bridge.run(args)


def _write_lockfile(path: Path, hits: list) -> None:
    """A run's hit log IS the lockfile. (import_name, package, version, wheel_tag) per line."""
    seen = set()
    lines = ["# bubble lockfile — recorded from a real run",
             "# columns: import_name  package  version  wheel_tag", ""]
    for import_name, package, version, tag in hits:
        key = (import_name, package, version, tag)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{import_name}\t{package}\t{version}\t{tag}")
    path.write_text("\n".join(lines) + "\n")


def cmd_up(args: argparse.Namespace) -> int:
    """bubble up <script.py> — scan, resolve, fetch, assemble, run."""
    from .scanner import py as scanner_py, resolver as resolver_mod
    from .run import assemble as assemble_mod, runner
    import hashlib
    import uuid
    from datetime import datetime
    db.init_db()
    script = Path(args.script).resolve()
    if not script.exists():
        print(f"error: script not found: {script}", file=sys.stderr)
        return 1

    # Stage 1: scan
    iset = scanner_py.scan(script)
    if args.verbose:
        print(f"scan: {len(iset.top_level_imports)} top-level imports "
              f"({len(iset.stdlib_imports)} stdlib excluded)")

    # Stage 2: resolve against vault
    plan = resolver_mod.resolve(iset)
    if args.verbose:
        print(f"resolve: {len(plan.resolved)} matched in vault, "
              f"{len(plan.missing)} missing")

    # Stage 3: fetch missing — opt-in only. Strict default keeps `bubble up`
    # offline unless the user (or env) explicitly authorizes network.
    autofetch = bool(args.fetch) or bool(os.environ.get("BUBBLE_AUTOFETCH"))
    if plan.missing and autofetch:
        plan = resolver_mod.fetch_missing(plan, allow_prerelease=args.prerelease)
        if args.verbose:
            print(f"fetch: {len(plan.resolved)} now resolved, "
                  f"{len(plan.missing)} still missing")

    # Stage 4: assemble bubble. uuid4 in the digest avoids collisions between
    # concurrent runs of the same script (timestamp resolution is not enough).
    bubble_id = hashlib.sha256(
        f"{script}{datetime.now().isoformat()}{uuid.uuid4()}".encode()
    ).hexdigest()[:12]
    bubble_dir = config.BUBBLES_DIR / bubble_id
    env = assemble_mod.assemble(plan, bubble_dir)
    if args.verbose:
        print(f"bubble {bubble_id} at {bubble_dir}")

    # Stage 5: run
    cmd = [sys.executable, str(script), *(args.args or [])]
    rc = runner.run(env, cmd, verbose=args.verbose)
    if not args.keep:
        import shutil
        shutil.rmtree(bubble_dir, ignore_errors=True)
    return rc


def cmd_shell_activate(args: argparse.Namespace) -> int:
    db.init_db()
    sd = shell_mod.shell_dir(args.name)
    if not sd.exists():
        print(f"shell does not exist: {args.name}", file=sys.stderr)
        return 1
    print(sd / "activate")
    return 0


def cmd_shell_bundle(args: argparse.Namespace) -> int:
    """bubble shell bundle <name> -o <path> — produce a portable artifact.

    The bundle holds the shell + its vault closure + the integrity facts
    (vault_files rows) the source machine recorded. Untar on a target
    machine with `bubble shell unbundle` and the trust chain travels.
    """
    from . import bundle as bundle_mod
    db.init_db()
    out = Path(args.output).resolve()
    try:
        summary = bundle_mod.bundle(args.name, out)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  bundled {args.name} → {out}")
    print(f"    packages: {summary['packages']}")
    print(f"    files:    {summary['files']}")
    print(f"    size:     {summary['bytes']/1024:.1f}KB")
    return 0


def cmd_shell_unbundle(args: argparse.Namespace) -> int:
    """bubble shell unbundle <tar> — extract a bundle into BUBBLE_HOME.

    The target's vault.db is rebuilt from the bundle's recorded rows;
    the target probes itself to write a fresh host.toml; every
    extracted pin is verified against the source's sha256 facts.
    """
    from . import bundle as bundle_mod
    try:
        result = bundle_mod.unbundle(
            Path(args.tar),
            allow_python_mismatch=args.allow_python_mismatch,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  unbundled {result['shell']} → {result['into_home']}")
    print(f"    packages: {result['packages']}")
    print(f"    source python_tag: {result['source_python']}")
    print(f"    target python_tag: {result['target_python']}")
    if result["drift"]:
        print(f"    DRIFT in transit ({len(result['drift'])} packages):")
        for d in result["drift"]:
            print(f"      × {d}")
        print(f"    Recorded as `vault_drift_*` in host.toml. The shell "
              f"is on disk but its drifted entries will refuse to link "
              f"on use. Investigate before activating.")
        return 2
    print(f"    integrity: clean (every extracted file matches source sha256)")
    return 0


# ─────────────────────────── human-surface commands ─────────────────────


def cmd_doctor(args: argparse.Namespace) -> int:
    """bubble doctor — one-screen environment health.

    Adapted from legacy. Reads vault stats, host portrait, shim state,
    and tool availability into a single ASCII tree.
    """
    from . import doctor as doctor_mod
    return doctor_mod.run()


def cmd_preflight(args: argparse.Namespace) -> int:
    """bubble preflight <script.py> — offline-readiness shopping list."""
    from . import preflight as preflight_mod
    return preflight_mod.run(Path(args.script))


def cmd_status(args: argparse.Namespace) -> int:
    """bubble status — glance-able vault overview."""
    if not config.VAULT_DB.exists():
        term.out()
        term.out(f"  {term.bold('◉ bubble')}  {term.dim('vault is empty')}")
        term.out()
        term.out(f"  {term.cyan('bubble setup'):<48}  {term.dim('import existing site-packages')}")
        term.out(f"  {term.cyan('bubble vault get <pkg>'):<48}  {term.dim('pre-cache a package')}")
        term.out()
        return 0
    db.init_db()
    conn = db.connect()
    try:
        n_pkgs = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
        n_native = conn.execute(
            "SELECT COUNT(*) FROM packages WHERE has_native=1"
        ).fetchone()[0]
        n_mods = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
        n_tl = conn.execute("SELECT COUNT(*) FROM top_level").fetchone()[0]
        n_shells = conn.execute("SELECT COUNT(*) FROM shells").fetchone()[0]
    finally:
        conn.close()
    # Dedup by (st_dev, st_ino) so hardlinked blobs count once — otherwise
    # status overstates disk usage by ~3-5x in a hardlinked vault.
    size_bytes = 0
    if config.VAULT_DIR.exists():
        seen: set[tuple[int, int]] = set()
        for f in config.VAULT_DIR.rglob("*"):
            try:
                if not f.is_file() or f.is_symlink():
                    continue
                st = f.stat()
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                size_bytes += st.st_size
            except OSError:
                continue
    pure = n_pkgs - n_native
    term.out()
    term.out(f"  {term.bold('◉ bubble')}  {term.dim('vault')}")
    term.out(f"      {n_pkgs} packages "
             f"{term.dim(f'({pure} pure, {n_native} native)')}, "
             f"{size_bytes / (1024 * 1024):.1f} MB")
    term.out(f"      {n_mods:,} modules, {n_tl:,} import names indexed")
    term.out(f"      {n_shells} shell{'s' if n_shells != 1 else ''}")

    from . import keep as keep_mod
    keeps = keep_mod.list_keeps()
    if keeps:
        total_keep_bytes = sum(k.get("_size_bytes", 0) for k in keeps)
        term.out()
        term.out(f"  {term.bold('◉ keep')}  {term.dim('projects & config in the vault')}")
        term.out(f"      {len(keeps)} keep{'s' if len(keeps) != 1 else ''}, "
                 f"{total_keep_bytes / (1024 * 1024):.2f} MB")
        for k in keeps:
            sz = k.get("_size_bytes", 0)
            sz_str = f"{sz / 1024:.1f} KiB" if sz < 1_048_576 \
                else f"{sz / 1_048_576:.2f} MB"
            term.out(f"      · {k['name']:<20}  {term.dim(k['kind']):<14}  "
                     f"{term.dim(sz_str)}")
    term.out()
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """bubble clean — dissolve ephemeral bubbles + clear vault staging.

    Two cleanup targets:
      - $BUBBLE_HOME/bubbles/* — leftover trees from `bubble up --keep`.
      - $BUBBLE_HOME/vault/.staging/* — abandoned staging dirs from
        interrupted vault adds.
    Shells are NOT touched here — they are user-named and persistent.
    """
    import shutil
    n_bubbles = 0
    n_staging = 0
    bubbles_dir = config.BUBBLES_DIR
    staging_dir = config.STAGING_DIR

    if bubbles_dir.exists():
        for d in list(bubbles_dir.iterdir()):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n_bubbles += 1
    if staging_dir.exists():
        for d in list(staging_dir.iterdir()):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n_staging += 1

    term.out()
    if n_bubbles == 0 and n_staging == 0:
        term.out(f"  {term.green('◉')} {term.dim('nothing to clean')}")
    else:
        term.out(f"  {term.green('◉')} cleaned: "
                 f"{n_bubbles} ephemeral bubble{'s' if n_bubbles != 1 else ''}, "
                 f"{n_staging} staging dir{'s' if n_staging != 1 else ''}")
    term.out()
    return 0


def cmd_default(args: argparse.Namespace) -> int:
    """No-args greeting. Show a tiny menu of where to start."""
    from . import __version__
    from . import keep as keep_mod
    term.out()
    term.out(f"  {term.bold('◉ bubble')}  {term.dim('v' + __version__)}")
    if config.VAULT_DB.exists():
        try:
            conn = db.connect()
            n_pkgs = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            conn.close()
            n_keeps = len(keep_mod.list_keeps())
            line = f"{n_pkgs} packages vaulted"
            if n_keeps:
                line += f", {n_keeps} keep{'s' if n_keeps != 1 else ''}"
            term.out(f"      {term.dim(line)}")
        except Exception:
            pass
    else:
        term.out(f"      {term.dim('vault not yet initialized — run `bubble setup`')}")
    term.out()
    term.out(f"  {term.cyan('bubble run <script.py>'):<48}  {term.dim('run a script')}")
    term.out(f"  {term.cyan('bubble vault get <pkg>'):<48}  {term.dim('pre-cache a package')}")
    term.out(f"  {term.cyan('bubble project ingest <dir>'):<48}  {term.dim('ingest a project tree')}")
    term.out(f"  {term.cyan('bubble keep capture <dir>'):<48}  {term.dim('archive a directory in the vault')}")
    term.out(f"  {term.cyan('bubble status'):<48}  {term.dim('what is in the vault')}")
    term.out(f"  {term.cyan('bubble doctor'):<48}  {term.dim('diagnose environment')}")
    term.out(f"  {term.cyan('bubble preflight <script.py>'):<48}  {term.dim('offline-readiness check')}")
    term.out(f"  {term.cyan('bubble shell create <name>'):<48}  {term.dim('long-lived bubble')}")
    term.out(f"  {term.cyan('bubble shell enter <name>'):<48}  {term.dim('spawn isolated interactive shell')}")
    term.out(f"  {term.cyan('bubble probe / host'):<48}  {term.dim('machine self-portrait')}")
    term.out()
    term.out(f"  {term.dim('flags:')}  "
             f"{term.dim('--quiet')}    "
             f"{term.dim('--yes')}")
    term.out()
    return 0


# ───────────────────────────── argument tree ─────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bubble",
                                description="content-addressed package vault + thin runtime views")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="suppress UI output (agent / pipe mode); errors still go to stderr")
    p.add_argument("--yes", "-y", action="store_true",
                   help="auto-confirm interactive prompts")
    p.set_defaults(func=cmd_default)
    sub = p.add_subparsers(dest="command", required=False)

    # vault
    vault = sub.add_parser("vault", help="manage the package vault")
    vsub = vault.add_subparsers(dest="vault_cmd", required=True)

    vsub.add_parser("list", help="list vaulted packages").set_defaults(func=cmd_vault_list)

    iv = vsub.add_parser("import-venv", help="import packages from a venv site-packages")
    iv.add_argument("path", help="path to a venv root or its site-packages dir")
    iv.add_argument("--copy", dest="hardlink", action="store_false",
                    help="copy files into the vault instead of hardlinking "
                         "(default is hardlink, with automatic fallback to "
                         "copy on cross-filesystem)")
    iv.add_argument("--hardlink", dest="hardlink", action="store_true",
                    help=argparse.SUPPRESS)  # kept for compatibility; default
    iv.set_defaults(hardlink=True)
    iv.add_argument("--overwrite", action="store_true",
                    help="re-import even if already vaulted at same (name,version,tag)")
    iv.add_argument("--skip", nargs="*", help="package names to skip")
    iv.add_argument("--verbose", "-v", action="store_true")
    iv.set_defaults(func=cmd_vault_import_venv)

    af = vsub.add_parser("audit-fs", help="scan filesystem for venvs and report duplicates")
    af.add_argument("--root", default="/", help="root to scan (default /)")
    af.set_defaults(func=cmd_vault_audit_fs)

    vr = vsub.add_parser("remove", help="remove a specific (name,version,tag) from vault")
    vr.add_argument("name")
    vr.add_argument("version")
    vr.add_argument("tag")
    vr.set_defaults(func=cmd_vault_remove)

    vg = vsub.add_parser("get", help="download a package from PyPI into the vault")
    vg.add_argument("package")
    vg.add_argument("--version", "-V", help="pin to exact version")
    vg.add_argument("--prerelease", action="store_true",
                    help="allow alpha/beta/rc/dev versions")
    vg.add_argument("--overwrite", action="store_true",
                    help="re-fetch even if already vaulted")
    vg.add_argument("--allow-sdist", action="store_true",
                    help="permit sdist builds (runs setup.py / build "
                         "backend; trust boundary crossed; default off)")
    vg.set_defaults(func=cmd_vault_get)

    # shell
    sh = sub.add_parser("shell", help="manage long-lived bubbles")
    ssub = sh.add_subparsers(dest="shell_cmd", required=True)

    sc = ssub.add_parser("create", help="create a new shell")
    sc.add_argument("name")
    sc.add_argument("specs", nargs="*", help="package specs (pkg or pkg==version)")
    sc.add_argument("--exist-ok", action="store_true")
    sc.add_argument("--from", dest="from_manifest", metavar="MANIFEST",
                    help="create from a deployment manifest "
                         "(see bubble.manifest format)")
    sc.add_argument("--fetch", action="store_true",
                    help="when --from is set, pull any missing pin from PyPI; "
                         "default is vault-only (fail closed on missing pins)")
    sc.add_argument("--parent", metavar="SHELL",
                    help="name of a parent shell; imports not pinned by this "
                         "shell resolve against the parent's scope (spinoff "
                         "/ project inheritance)")
    sc.set_defaults(func=cmd_shell_create)

    sa = ssub.add_parser("add", help="add packages to a shell")
    sa.add_argument("name")
    sa.add_argument("specs", nargs="+")
    sa.set_defaults(func=cmd_shell_add)

    sr = ssub.add_parser("remove", help="unlink packages from a shell")
    sr.add_argument("name")
    sr.add_argument("pkgs", nargs="+")
    sr.set_defaults(func=cmd_shell_remove)

    ssub.add_parser("list", help="list shells").set_defaults(func=cmd_shell_list)

    sd = ssub.add_parser("delete", help="delete a shell")
    sd.add_argument("name")
    sd.set_defaults(func=cmd_shell_delete)

    se = ssub.add_parser("exec", help="exec a command with shell PYTHONPATH/PATH set")
    se.add_argument("name")
    se.add_argument("cmd", nargs=argparse.REMAINDER)
    se.set_defaults(func=cmd_shell_exec)

    sent = ssub.add_parser("enter", help="spawn an interactive shell with bubble environment loaded")
    sent.add_argument("name")
    sent.set_defaults(func=cmd_shell_enter)

    sact = ssub.add_parser("activate", help="print path to activate script")
    sact.add_argument("name")
    sact.set_defaults(func=cmd_shell_activate)

    sb = ssub.add_parser("bundle", help="bundle a shell + its vault closure into a tar.gz")
    sb.add_argument("name")
    sb.add_argument("-o", "--output", required=True,
                    help="output path for the tar.gz bundle")
    sb.set_defaults(func=cmd_shell_bundle)

    sf = ssub.add_parser("freeze",
                         help="snapshot a shell's state as a deployment manifest")
    sf.add_argument("name")
    sf.add_argument("-o", "--output", required=True,
                    help="output path for the manifest (.toml)")
    sf.set_defaults(func=cmd_shell_freeze)

    sub_un = ssub.add_parser("unbundle",
                             help="extract a bundle into BUBBLE_HOME, "
                                  "rebuild vault.db, probe, verify")
    sub_un.add_argument("tar", help="path to a bubble bundle (.tar.gz)")
    sub_un.add_argument("--allow-python-mismatch", action="store_true",
                        help="extract even when source/target python_tag differ "
                             "(verify will still refuse drifted pins)")
    sub_un.set_defaults(func=cmd_shell_unbundle)

    # project — ingest a whole project directory into a named shell
    proj = sub.add_parser("project",
                          help="project-level operations (ingest a directory, freeze)")
    psub = proj.add_subparsers(dest="project_cmd", required=True)

    pi = psub.add_parser("ingest",
                         help="scan all .py files in a directory and create a "
                              "named shell pinned to their resolved dependencies")
    pi.add_argument("dir", help="project directory to scan")
    pi.add_argument("--name", "-n", required=True,
                    help="shell name to create / update")
    pi.add_argument("--parent", metavar="SHELL",
                    help="name of a parent shell for scope inheritance "
                         "(spinoff projects)")
    pi.add_argument("--fetch", action="store_true",
                    help="pull missing packages from PyPI; default vault-only")
    pi.add_argument("--overwrite", action="store_true",
                    help="recreate the shell if it already exists")
    pi.add_argument("--verbose", "-v", action="store_true")
    pi.set_defaults(func=cmd_project_ingest)

    # keep — vault as canonical home for projects, repos, and shell config
    keep_p = sub.add_parser("keep",
                            help="absorb directories and files into the vault "
                                 "as the canonical home")
    ksub = keep_p.add_subparsers(dest="keep_cmd", required=True)

    kc = ksub.add_parser("capture",
                         help="absorb a directory into the vault and symlink the "
                              "original path to it")
    kc.add_argument("dir", help="directory to absorb")
    kc.add_argument("--name", default=None,
                    help="name to file this keep under (default: basename of dir)")
    kc.add_argument("--overwrite", action="store_true",
                    help="replace an existing keep with this name")
    kc.add_argument("--no-symlink-back", action="store_true",
                    help="leave the source path alone; do not replace it with a "
                         "symlink (the vault still receives a copy)")
    kc.set_defaults(func=cmd_keep_capture)

    kl = ksub.add_parser("list", help="list keeps in the vault")
    kl.set_defaults(func=cmd_keep_list)

    ks = ksub.add_parser("show", help="show meta for one keep")
    ks.add_argument("name")
    ks.set_defaults(func=cmd_keep_show)

    kw = ksub.add_parser("wire",
                         help="recreate the source-path symlinks recorded in meta "
                              "(use after a filesystem nuke or unwire)")
    kw.add_argument("name")
    kw.set_defaults(func=cmd_keep_wire)

    kuw = ksub.add_parser("unwire",
                          help="remove the source-path symlinks; the vault tree stays")
    kuw.add_argument("name")
    kuw.set_defaults(func=cmd_keep_unwire)

    ka = ksub.add_parser("activate",
                         help="symlink the keep's bin/ executables into ~/.local/bin/")
    ka.add_argument("name")
    ka.set_defaults(func=cmd_keep_activate)

    kda = ksub.add_parser("deactivate",
                          help="remove the ~/.local/bin/ symlinks for this keep")
    kda.add_argument("name")
    kda.set_defaults(func=cmd_keep_deactivate)

    krm = ksub.add_parser("remove",
                          help="unwire, deactivate, and delete from the vault")
    krm.add_argument("name")
    krm.set_defaults(func=cmd_keep_remove)

    # up — ephemeral per-script bubble (the original entry point)
    up = sub.add_parser("up", help="scan a script, assemble an ephemeral bubble, run")
    up.add_argument("script")
    up.add_argument("args", nargs="*")
    up.add_argument("--keep", action="store_true", help="don't dissolve bubble after run")
    up.add_argument("--fetch", action="store_true",
                    help="opt in to PyPI fetches for missing packages "
                         "(default: vault-only, fail closed)")
    up.add_argument("--prerelease", action="store_true")
    up.add_argument("--verbose", "-v", action="store_true")
    up.set_defaults(func=cmd_up)

    # setup — zero-flag bootstrap (probe + scan all site-packages)
    st = sub.add_parser("setup",
        help="bootstrap: probe the host and import every site-packages into the vault")
    st.set_defaults(func=cmd_setup)

    # probe — write host.toml self-portrait
    pr = sub.add_parser("probe",
        help="interrogate the machine; write ~/.bubble/host.toml")
    pr.add_argument("--show", action="store_true",
                    help="print the full toml after writing")
    pr.set_defaults(func=cmd_probe)

    # host — show what bubble currently knows (consult side of the loop)
    h = sub.add_parser("host",
        help="show what bubble knows about this machine + recorded failures")
    h.set_defaults(func=cmd_host)

    # run — demand-paged execution, no materialized bubble
    run = sub.add_parser("run",
        help="run a script with imports faulted in from the vault on demand")
    run.add_argument("script")
    run.add_argument("args", nargs="*")
    run.add_argument("--fetch", action="store_true",
                     help="opt in to PyPI fetches on vault miss "
                          "(default: vault-only, fail closed)")
    run.add_argument("--scope", help="path to a scope manifest (TOML)")
    run.add_argument("--lock", help="record actually-loaded closure to this lockfile")
    run.add_argument("--isolate", action="store_true",
                     help="strip system site-packages from sys.path; vault is sole source")
    run.add_argument("--no-preflight", action="store_true",
                     help="skip the pre-run scan + interactive offer (TTY only behavior)")
    run.add_argument("--verbose", "-v", action="store_true")
    run.set_defaults(func=cmd_run)

    # doctor — one-screen environment health
    dr = sub.add_parser("doctor", help="one-screen environment health check")
    dr.set_defaults(func=cmd_doctor)

    # preflight — offline-readiness shopping list for a script
    pf = sub.add_parser("preflight",
        help="given a script, list what's needed to run it offline")
    pf.add_argument("script")
    pf.set_defaults(func=cmd_preflight)

    # status — glance-able vault overview
    sts = sub.add_parser("status", help="glance-able vault overview")
    sts.set_defaults(func=cmd_status)

    # clean — dissolve ephemeral bubbles + stale staging
    cl = sub.add_parser("clean",
        help="dissolve ephemeral bubbles and clear vault staging dirs")
    cl.set_defaults(func=cmd_clean)

    # bridge — route .py to main bubble and .js/.ts to legacy with guardrails
    br = sub.add_parser("bridge",
        help="route .py to main bubble and .js/.ts to legacy bubble with guardrails")
    br.add_argument("script")
    br.add_argument("args", nargs="*")
    br.add_argument("--fetch", action="store_true",
                    help="for .py routes only: authorize PyPI fetch on vault miss")
    br.add_argument("--no-isolate", action="store_true",
                    help="for .py routes only: keep system site-packages on sys.path")
    br.add_argument("--allow-legacy-network", action="store_true",
                    help="for .js/.ts routes only: authorize legacy network fetch")
    br.add_argument("--keep", action="store_true",
                    help="for legacy routes only: keep ephemeral bubble after run")
    br.add_argument("--dry-run", action="store_true",
                    help="print selected command and exit")
    br.set_defaults(func=cmd_bridge)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Top-level flags propagate via env so deep call paths (term, prompt_yn,
    # subprocess invocations) all see them without threading.
    if getattr(args, "quiet", False):
        os.environ["BUBBLE_QUIET"] = "1"
    if getattr(args, "yes", False):
        os.environ["BUBBLE_AUTOYES"] = "1"

    try:
        return args.func(args)
    except KeyboardInterrupt:
        term.show_cursor()
        term.out()
        term.out(f"  {term.amber('⚠')} interrupted")
        return 130
    except BrokenPipeError:
        # Silent — happens when piping to head/less and the consumer closes.
        return 0


if __name__ == "__main__":
    sys.exit(main())


# ─────────────────────────── PyPI fetch ──────────────────────────────────


def cmd_vault_get(args: argparse.Namespace) -> int:
    """bubble vault get <package> [--version V] — download from PyPI into vault."""
    from .vault import fetcher
    db.init_db()
    shims.apply()  # SSL is needed for the fetch on Termux/proot
    pin = args.version
    pkg_label = args.package + (f"=={pin}" if pin else "")

    err_text: str | None = None
    err_rc = 0
    result = None
    with term.Progress(["fetching"], title=pkg_label) as p:
        try:
            result = fetcher.fetch_into_vault(
                args.package,
                pinned_version=pin,
                allow_prerelease=args.prerelease,
                overwrite=args.overwrite,
                allow_sdist=args.allow_sdist,
            )
        except FileNotFoundError as e:
            p.fail("not found")
            err_text, err_rc = str(e), 1
        except (RuntimeError, ValueError) as e:
            # Sovereignty refusals (sdist, off-host index, name mismatch).
            p.fail(str(e)[:60])
            err_text, err_rc = str(e), 2

    # Stderr writes happen AFTER the bar finalizes — otherwise the live
    # render thread overwrites them on the next tick.
    if err_text:
        term.err(f"  {term.red('✗')} {err_text}")
        return err_rc

    if not result:
        term.out(f"  {term.dim('—')} already vaulted or no compatible release for {pkg_label}")
        return 0
    name, version, tag = result
    term.out(f"  {term.green('✓')} vaulted "
             f"{term.cyan(f'{name}=={version}')}  {term.dim(f'({tag})')}")
    return 0
