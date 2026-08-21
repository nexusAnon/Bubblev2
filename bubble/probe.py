"""Probe — bubble interrogates the machine and writes a self-portrait.

Each probe is small, independent, and records its result. The output is
~/.bubble/host.toml — the seed of bubble's awareness of where it's running.

Probes are honest about what they find. A probe that fails to detect
something records the failure; it doesn't pretend the capability isn't
there if it might be. The portrait is written empirically.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
import sysconfig
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import config


# ────────────────────────── individual probes ───────────────────────────


def probe_kernel() -> dict:
    return {
        "system":     platform.system(),
        "release":    platform.release(),
        "machine":    platform.machine(),
        "uname":      " ".join(platform.uname()[:3]),
    }


def probe_libc() -> dict:
    out: dict = {"variant": "unknown"}
    try:
        libc = ctypes.CDLL("libc.so.6")
        # gnu_get_libc_version exists on glibc, not on musl
        try:
            libc.gnu_get_libc_version.restype = ctypes.c_char_p
            out["variant"] = "glibc"
            out["version"] = libc.gnu_get_libc_version().decode()
        except AttributeError:
            # Try musl detection — its dynamic linker reports itself
            ldso = "/lib/ld-musl-" + platform.machine() + ".so.1"
            if os.path.exists(ldso):
                out["variant"] = "musl"
                out["ldso"] = ldso
    except OSError as exc:
        out["error"] = str(exc)
    return out


def probe_python() -> dict:
    return {
        "version":       ".".join(map(str, sys.version_info[:3])),
        "implementation": sys.implementation.name,
        "executable":    sys.executable,
        "shared":        bool(sysconfig.get_config_var("Py_ENABLE_SHARED")),
        "ldlibrary":     sysconfig.get_config_var("LDLIBRARY") or "",
        "libdir":        sysconfig.get_config_var("LIBDIR") or "",
        "stdlib_dir":    sysconfig.get_path("stdlib") or "",
    }


def probe_dlmopen() -> dict:
    """Try to dlmopen libc.so.6 into a fresh link namespace. If the new
    handle has a different lmid than 0, dlmopen-isolation is available."""
    out: dict = {"available": False}
    try:
        libc = ctypes.CDLL("libc.so.6")
        if not hasattr(libc, "dlmopen"):
            out["reason"] = "dlmopen symbol not present"
            return out
        libc.dlmopen.restype = ctypes.c_void_p
        libc.dlmopen.argtypes = [ctypes.c_long, ctypes.c_char_p, ctypes.c_int]
        libc.dlerror.restype = ctypes.c_char_p
        libc.dlinfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        # Probe with a benign target — libc itself
        h = libc.dlmopen(-1, b"libc.so.6", 2)  # LM_ID_NEWLM | RTLD_NOW
        if not h:
            err = libc.dlerror()
            out["reason"] = err.decode() if err else "dlmopen returned NULL"
            return out
        out["available"] = True
        lmid = ctypes.c_long(0)
        libc.dlinfo(ctypes.c_void_p(h), 1, ctypes.byref(lmid))  # RTLD_DI_LMID
        out["new_lmid"] = lmid.value
    except OSError as exc:
        out["reason"] = str(exc)
    return out


def probe_libpython_embeddable() -> dict:
    """Test whether we can dlopen libpython and see Py_Initialize. This is
    the precondition for the dlmopen-libpython substrate."""
    out: dict = {"embeddable": False}
    libdir = sysconfig.get_config_var("LIBDIR")
    libname = sysconfig.get_config_var("LDLIBRARY")
    if not (libdir and libname):
        out["reason"] = "sysconfig has no LIBDIR/LDLIBRARY"
        return out
    candidates = [
        Path(libdir) / libname,
        Path(libdir) / (libname + ".1"),
        Path(libdir) / (libname + ".1.0"),
    ]
    found = next((p for p in candidates if p.exists()), None)
    if not found:
        out["reason"] = f"could not find libpython at any of {candidates}"
        return out
    try:
        h = ctypes.CDLL(str(found), mode=ctypes.RTLD_LOCAL)
        if hasattr(h, "Py_Initialize") and hasattr(h, "PyRun_SimpleString"):
            out["embeddable"] = True
            out["path"] = str(found)
            out["size_bytes"] = found.stat().st_size
        else:
            out["reason"] = "libpython lacks expected embedding symbols"
    except OSError as exc:
        out["reason"] = str(exc)
    return out


def probe_subinterpreters() -> dict:
    """PEP 684 sub-interpreter availability. Doesn't try to load anything
    inside one — just confirms the API is there."""
    try:
        import _interpreters
        return {"available": True, "active": _interpreters.list_all()}
    except ImportError as exc:
        return {"available": False, "reason": str(exc)}


def probe_termux_proot() -> dict:
    """Detect whether we're running inside Termux or under proot — affects
    path shims and which substrate options are real."""
    out = {
        "termux":      "com.termux" in os.environ.get("PREFIX", ""),
        "prefix":      os.environ.get("PREFIX", ""),
        "proot":       bool(os.environ.get("PROOT_TMP_DIR"))
                       or os.path.exists("/proc/self/root/.l2s"),
    }
    return out


def probe_resources() -> dict:
    """Coarse machine resources — useful for substrate-cost decisions."""
    out: dict = {}
    try:
        out["cpu_count"] = os.cpu_count() or 0
    except OSError:
        out["cpu_count"] = 0
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                out["mem_total_mb"] = kb // 1024
                break
    except (OSError, ValueError):
        pass
    return out


def probe_runner() -> dict:
    """The bubble-runner side: which Python tag and platform tag we expect
    to match wheels against."""
    return {
        "python_tag":   config.runner_python_tag(),
        "platform_tag": config.runner_platform_tag(),
        "host":         config.detect_host(),
    }


def probe_universal() -> dict:
    """Interrogate the machine for non-Python package managers and tools
    needed for universal repository absorption and linker rewriting."""
    tools = ["patchelf", "install_name_tool", "npm", "node", "cargo", "go", "make", "gcc", "clang"]
    out = {}
    for tool in tools:
        path = shutil.which(tool)
        out[f"has_{tool}"] = path is not None
        out[f"{tool}_path"] = path or ""
    return out


# ─────────────────────────── orchestration ──────────────────────────────


PROBES: dict[str, Callable[[], dict]] = {
    "kernel":              probe_kernel,
    "libc":                probe_libc,
    "python":              probe_python,
    "termux_proot":        probe_termux_proot,
    "resources":           probe_resources,
    "runner":              probe_runner,
    "dlmopen":             probe_dlmopen,
    "libpython_embeddable": probe_libpython_embeddable,
    "subinterpreters":     probe_subinterpreters,
    "universal":           probe_universal,
}


def run_all() -> dict:
    """Run every probe, collect results into a single dict."""
    out: dict = {
        "probed_at":     datetime.now().isoformat(),
        "bubble_version": __import__("bubble").__version__,
    }
    for name, fn in PROBES.items():
        try:
            out[name] = fn()
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    out["substrates"] = derive_substrates(out)
    return out


def derive_substrates(probe_results: dict) -> list[dict]:
    """Turn raw probe results into a ranked list of substrates available
    on this machine. Each entry: {name, cost_mb, applies_to, status}."""
    subs = []
    # Always available: in-process for pure-Python aliases
    subs.append({
        "name":       "in_process",
        "cost_mb":    0,
        "applies_to": "tier-1 pure-Python aliases",
        "status":     "available",
    })
    # Sub-interpreter substrate
    si = probe_results.get("subinterpreters", {})
    subs.append({
        "name":       "sub_interpreter",
        "cost_mb":    1,
        "applies_to": "tier-2 cooperating extensions (Py_mod_multiple_interpreters)",
        "status":     "available" if si.get("available") else "unavailable",
        "detail":     si.get("reason", ""),
    })
    # dlmopen-isolated libpython
    dlm = probe_results.get("dlmopen", {})
    emb = probe_results.get("libpython_embeddable", {})
    if dlm.get("available") and emb.get("embeddable"):
        subs.append({
            "name":       "dlmopen_isolated",
            "cost_mb":    (emb.get("size_bytes", 0) // 1024 // 1024) or 5,
            "applies_to": "tier-3 native extensions with shared C state",
            "status":     "available (multi-call needs GIL-managed re-entry)",
            "detail":     f"libpython at {emb.get('path')}",
        })
    else:
        subs.append({
            "name":       "dlmopen_isolated",
            "cost_mb":    None,
            "applies_to": "tier-3 native extensions with shared C state",
            "status":     "unavailable",
            "detail":     dlm.get("reason") or emb.get("reason") or "preconditions not met",
        })
    # Subprocess fallback — assume always available if Python is here
    subs.append({
        "name":       "subprocess",
        "cost_mb":    30,
        "applies_to": "anything that resists in-process isolation",
        "status":     "available",
    })
    return subs


# ─────────────────────────── output formatting ──────────────────────────


def to_toml(results: dict) -> str:
    """Cheap TOML emitter for the result dict. We don't have tomllib write
    in stdlib until 3.13, and we want bubble to stay zero-dep, so emit by
    hand. Format is the inverse of bubble.run.shell._read_manifest's reader."""
    lines: list[str] = [
        "# bubble host portrait — written by `bubble probe`",
        f'probed_at     = "{results["probed_at"]}"',
        f'bubble_version = "{results["bubble_version"]}"',
        "",
    ]
    for section in ("kernel", "libc", "python", "termux_proot", "resources",
                    "runner", "dlmopen", "libpython_embeddable", "subinterpreters", "universal"):
        block = results.get(section, {})
        lines.append(f"[{section}]")
        for k, v in block.items():
            lines.append(_toml_kv(k, v))
        lines.append("")
    lines.append("# derived: substrates available for hosting alias namespaces")
    for sub in results.get("substrates", []):
        lines.append(f"[[substrates]]")
        for k, v in sub.items():
            lines.append(_toml_kv(k, v))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_kv(k: str, v: Any) -> str:
    if isinstance(v, bool):
        return f"{k} = {'true' if v else 'false'}"
    if isinstance(v, int):
        return f"{k} = {v}"
    if v is None:
        return f"# {k} = (unknown)"
    if isinstance(v, str):
        return f'{k} = "{_toml_escape(v)}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(x) for x in v)
        return f"{k} = [{items}]"
    return f'{k} = "{_toml_escape(str(v))}"'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, int):  return str(v)
    if isinstance(v, str):  return f'"{_toml_escape(v)}"'
    return f'"{_toml_escape(str(v))}"'


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def write(path: Path, results: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_toml(results))


def host_toml_path() -> Path:
    return config.BUBBLE_HOME / "host.toml"
