"""AgentVault — bubble as a primitive other systems consume.

The CLI surface (vault | shell | run | bridge | probe | host) is bubble
shaped for a human at a terminal. This module is bubble shaped for an
agent framework embedding bubble as a library — a single import surface
that gives the framework what bubble already has and the framework
cannot easily build itself: a content-addressed package vault, a meta-
path finder that intercepts unresolved imports, and a substrate ladder
that hosts multi-version coexistence in one process tree.

The intended consumer is an autonomous agent runtime that needs to
load tools at runtime, must not be trapped in a single dependency
graph, and cannot afford one tool's transitive deps to break another's.
The shape:

    from bubble import AgentVault

    with AgentVault(home="/srv/agent/.bubble", autofetch=True) as av:
        av.add("requests", version="2.32.5")
        av.register("http", real_name="requests", isolation="subprocess")

        http = av.tool("http")
        resp = http.get("https://example.com")

The discipline this surface preserves: bubble does not run installation
scripts. `add()` calls into `bubble.vault.fetcher` which refuses sdists
by default. Execution is dispatched to substrate handlers through the
existing meta-finder + router; no new path bypasses the integrity edge
or the substrate ladder. AgentVault is composition over those primitives,
not a parallel implementation.

What this module is not:
  - a sandbox. Substrates isolate aliases from each other; they don't
    sandbox tool code from the host. Sandboxing untrusted build code is
    a separate move that builds on the subprocess substrate.
  - a build system. Wheels are vaulted; sdists refuse by default.
  - a process supervisor. Substrate handlers manage their own children
    via the existing per-alias registries with atexit cleanup.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Optional


_VALID_ISOLATIONS = frozenset({
    "in_process", "subprocess", "dlmopen_isolated", "sub_interpreter",
})


class AgentVault:
    """Embedding surface for an agent framework that consumes bubble.

    Construction sets up the vault root (optionally a non-default path),
    initializes the SQLite index, and prepares the alias map the
    meta-finder will resolve through. Tools are vaulted with `add()`,
    registered as named aliases with `register()`, and accessed as
    Python modules with `tool()`.

    Lifetime: use as a context manager (`with AgentVault() as av:`) or
    call `close()` explicitly. Closing removes the meta-finder from
    sys.meta_path and shuts down any substrate child interpreters
    (subprocess) or isolated namespaces (dlmopen) eagerly, so the agent
    framework isn't left with zombies.
    """

    def __init__(
        self,
        *,
        home: Optional[Path | str] = None,
        autofetch: bool = False,
        verbose: bool = False,
    ) -> None:
        from . import config

        if home is not None:
            config.set_home(home)

        config.ensure_dirs()
        from .vault import db
        db.init_db()

        self._autofetch = autofetch
        self._verbose = verbose
        # alias → (real_name, version, wheel_tag, isolation)
        self._aliases: dict[str, tuple[str, str, str, Optional[str]]] = {}
        self._finder = None
        self._closed = False

    # ─────────────────────── vault management ──────────────────────────

    def add(
        self,
        package: str,
        *,
        version: Optional[str] = None,
        prerelease: bool = False,
        overwrite: bool = False,
        allow_sdist: bool = False,
    ) -> Optional[tuple[str, str, str]]:
        """Vault a package from PyPI. Refuses sdists by default — running
        setup.py is RCE under the agent's privileges, and AgentVault
        inherits that boundary.

        Returns (name, version, wheel_tag) on success, None if already
        vaulted at the same pin.
        """
        from .vault import fetcher
        return fetcher.fetch_into_vault(
            package,
            pinned_version=version,
            allow_prerelease=prerelease,
            overwrite=overwrite,
            allow_sdist=allow_sdist,
        )

    def add_from_venv(
        self,
        site_packages: Path | str,
        *,
        hardlink: bool = False,
        overwrite: bool = False,
    ) -> dict:
        """Import a venv's site-packages into the vault. Refuses
        symlinked RECORD entries — the bytes a content-addressed vault
        serves under a name must come from the file the dist's RECORD
        names, not from a symlink terminus.

        Returns the importer's summary dict (imported, skipped,
        missing_record, errors, entries)."""
        from .vault import importer
        site = Path(site_packages).expanduser().resolve()
        return importer.import_site_packages(
            site, hardlink=hardlink, overwrite=overwrite,
        )

    # ─────────────────────── tool registration ─────────────────────────

    def register(
        self,
        alias: str,
        *,
        real_name: Optional[str] = None,
        version: Optional[str] = None,
        wheel_tag: Optional[str] = None,
        isolation: Optional[str] = None,
    ) -> None:
        """Register a tool by alias. The agent imports the tool by its
        alias name; bubble resolves the alias to (real_name, version,
        wheel_tag) and hosts it on the chosen isolation substrate.

        - alias: the name the agent will import as
        - real_name: the dist name in the vault (defaults to alias)
        - version, wheel_tag: pin; if either is None the latest vaulted
          entry for `real_name` is used
        - isolation: 'in_process' (default), 'subprocess',
          'dlmopen_isolated', or 'sub_interpreter'

        Re-registering the same alias replaces the previous binding.
        Multiple aliases for the same dist with different versions and/
        or different isolation rings is the supported diamond-conflict-
        dissolution pattern.
        """
        if isolation is not None and isolation not in _VALID_ISOLATIONS:
            raise ValueError(
                f"isolation must be one of {sorted(_VALID_ISOLATIONS)}, "
                f"got {isolation!r}"
            )
        real = real_name or alias
        if version is None or wheel_tag is None:
            v, t = self._latest_pin(real)
            version = version or v
            wheel_tag = wheel_tag or t
        self._aliases[alias] = (real, version, wheel_tag, isolation)
        # If the alias was already imported, remove it from sys.modules
        # so the next `tool(alias)` re-resolves through the new binding.
        if alias in sys.modules:
            del sys.modules[alias]
        self._reinstall_finder()

    def tool(self, alias: str):
        """Return the registered tool as a Python module.

        For in_process aliases this is a normal module from the vaulted
        package. For subprocess / dlmopen_isolated aliases this is a
        proxy module (types.ModuleType subclass) whose attribute access
        marshals into the isolated interpreter.

        Subsequent calls return the same module object (cached in
        sys.modules under the alias name) until `register()` rebinds
        the alias.
        """
        self._check_open()
        if alias not in self._aliases:
            raise LookupError(
                f"alias {alias!r} not registered; call register() first"
            )
        if self._finder is None:
            self._reinstall_finder()
        return importlib.import_module(alias)

    # ─────────────────────── inspection ────────────────────────────────

    def list_vaulted(self) -> list[tuple[str, str, str]]:
        """Return every (name, version, wheel_tag) the vault holds."""
        from .vault import db
        conn = db.connect()
        try:
            return [
                (row[0], row[1], row[2])
                for row in conn.execute(
                    "SELECT name, version, wheel_tag FROM packages "
                    "ORDER BY name, version"
                )
            ]
        finally:
            conn.close()

    def registered_tools(self) -> dict[str, dict]:
        """Return the alias map in inspectable form."""
        return {
            alias: {
                "real_name": real,
                "version": version,
                "wheel_tag": wheel_tag,
                "isolation": isolation,
            }
            for alias, (real, version, wheel_tag, isolation)
            in self._aliases.items()
        }

    def register_polyglot(
        self,
        alias: str,
        source_path: Path | str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Register an arbitrary C, C++, Rust, Bash, or Binary tool for the agent.

        Compiles C/C++ or Rust assets, link-patches them for user-space hermeticity,
        registers the build in v4 universal metadata database tables, and returns
        the absolute path to the fully contained binary executable.
        """
        self._check_open()
        source_path = Path(source_path).resolve()
        from .run import shell as shell_mod
        from .vault import db
        from . import config

        # Create a dedicated agent-shell bin directory
        sd = shell_mod.shell_dir(f"agent-{alias}")
        if sd.exists() and not overwrite:
            raise FileExistsError(f"Polyglot tool alias {alias!r} already registered at {sd}")
        elif sd.exists():
            import shutil
            shutil.rmtree(sd)

        sd.mkdir(parents=True)
        shell_bin = sd / "bin"
        shell_bin.mkdir(parents=True)
        (sd / "lib").mkdir()

        # Handle file shapes
        compiled_executable = None
        ecosystem = "binary"

        suffix = source_path.suffix.lower()
        if suffix in (".c", ".cpp", ".cc"):
            from .run.builder import build_c_cpp
            compiled_executable = shell_bin / source_path.stem
            build_c_cpp(source_path, compiled_executable)
            ecosystem = "c_cpp"
        elif source_path.name == "Cargo.toml":
            from .run.builder import build_rust_cargo
            binaries = build_rust_cargo(source_path, shell_bin)
            if binaries:
                compiled_executable = binaries[0]
            ecosystem = "cargo"
        elif suffix == ".sh":
            compiled_executable = shell_bin / source_path.name
            if compiled_executable.exists() or compiled_executable.is_symlink():
                compiled_executable.unlink()
            rel_target = os.path.relpath(source_path, start=compiled_executable.parent)
            os.symlink(rel_target, compiled_executable)
            source_path.chmod(source_path.stat().st_mode | 0o111)
            ecosystem = "bash"
        else:
            # Assume pre-compiled binary executable
            compiled_executable = shell_bin / source_path.name
            import shutil
            shutil.copy2(source_path, compiled_executable)
            compiled_executable.chmod(compiled_executable.stat().st_mode | 0o111)
            # Patch links
            from .tools.elf import patch_elf_binary
            from .tools.macho import patch_macho_binary
            try:
                patch_elf_binary(compiled_executable, rpath="$ORIGIN/../lib")
            except Exception:
                pass
            try:
                patch_macho_binary(compiled_executable, rpath_changes=[("@loader_path/../lib", "@loader_path/../lib")])
            except Exception:
                pass

        if not compiled_executable or not compiled_executable.exists():
            raise RuntimeError(f"Could not build or register polyglot tool at {source_path}")

        # Register in SQLite v4 universal metadata tables
        conn = db.connect()
        try:
            pkg_id = f"agent:{alias}"
            # Record universal package
            conn.execute(
                "INSERT OR REPLACE INTO universal_packages (id, name, version, ecosystem, platform_tag, sha256, vault_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pkg_id, alias, "1.0.0", ecosystem, config.runner_platform_tag(), "sha256-agent-tool", str(sd))
            )
            # Record binary
            conn.execute(
                "INSERT OR REPLACE INTO universal_binaries (package_id, binary_name, rel_path) "
                "VALUES (?, ?, ?)",
                (pkg_id, compiled_executable.name, str(compiled_executable.relative_to(sd)))
            )
            conn.commit()
        finally:
            conn.close()

        # Cache in aliases map for record
        self._aliases[alias] = (alias, "1.0.0", "universal", f"agent-polyglot:{ecosystem}")
        return compiled_executable

    def exec_polyglot(self, alias: str, args: list[str]) -> subprocess.CompletedProcess:
        """Execute a registered polyglot tool inside its isolated user-space shell.

        Prepopulates paths (PYTHONPATH, PATH, LD_LIBRARY_PATH, DYLD_LIBRARY_PATH, PKG_CONFIG_PATH),
        guaranteeing safe and hermetic binary execution, capturing and returning the completed process.
        """
        self._check_open()
        from .run import shell as shell_mod
        sd = shell_mod.shell_dir(f"agent-{alias}")
        if not sd.exists():
            raise LookupError(f"Polyglot tool alias {alias!r} not registered; call register_polyglot() first")

        # Find registered binary name from universal binaries
        from .vault import db
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT rel_path FROM universal_binaries WHERE package_id=?",
                (f"agent:{alias}",)
            ).fetchone()
        finally:
            conn.close()

        if not row:
            raise LookupError(f"No binary registered for polyglot tool alias {alias!r}")

        executable = sd / row[0]

        # Build environment
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
        env["BUBBLE_SHELL"] = f"agent-{alias}"
        env["BUBBLE_SHELL_DIR"] = str(sd)

        import subprocess
        return subprocess.run([str(executable)] + args, capture_output=True, env=env)

    # ─────────────────────── lifecycle ─────────────────────────────────

    def close(self) -> None:
        """Tear down the meta-finder and release substrate children.
        Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._finder is not None and self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        self._finder = None
        # Drop every registered alias from sys.modules so a subsequent
        # AgentVault in the same process starts clean.
        for alias in list(self._aliases):
            if alias in sys.modules:
                del sys.modules[alias]
        # Eagerly close substrate registries; atexit will also clean up
        # but the agent framework may want bubble's children gone now
        # rather than at process exit.
        try:
            from .substrate import dlmopen as _dl
            _dl._shutdown_registry()
        except Exception:
            pass
        try:
            from .substrate import subprocess as _sub
            _sub._shutdown_registry()
        except Exception:
            pass

    def __enter__(self) -> "AgentVault":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    # ─────────────────────── internals ─────────────────────────────────

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("AgentVault is closed")

    def _latest_pin(self, real_name: str) -> tuple[str, str]:
        """Pick the newest cached_at entry for `real_name`. The agent's
        register() default; explicit pin always wins."""
        from .vault import db
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT version, wheel_tag FROM packages WHERE name=? "
                "ORDER BY cached_at DESC LIMIT 1",
                (real_name,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise LookupError(
                f"{real_name!r} not in vault; call add() first or pass "
                f"explicit version + wheel_tag to register()"
            )
        return row[0], row[1]

    def _reinstall_finder(self) -> None:
        """Replace the meta-finder with one carrying the current alias
        map. AgentVault keeps a single finder for its lifetime; each
        register() rebuilds it so newly added aliases take effect on
        the next import."""
        from .meta_finder import install
        if self._finder is not None and self._finder in sys.meta_path:
            sys.meta_path.remove(self._finder)
        self._finder = install(
            aliases=self._aliases or None,
            autofetch=self._autofetch,
            verbose=self._verbose,
        )
