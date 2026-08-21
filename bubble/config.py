"""Paths, environment, and host detection."""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


BUBBLE_HOME = Path(os.environ.get("BUBBLE_HOME", os.path.expanduser("~/.bubble")))

VAULT_DIR = BUBBLE_HOME / "vault"
VAULT_DB = BUBBLE_HOME / "vault.db"
BUBBLES_DIR = BUBBLE_HOME / "bubbles"
SHELLS_DIR = BUBBLE_HOME / "shells"
LOGS_DIR = BUBBLE_HOME / "logs"
WHEELS_DIR = BUBBLE_HOME / "wheels"
KEEP_DIR = BUBBLE_HOME / "keep"
STAGING_DIR = VAULT_DIR / ".staging"


def ensure_dirs() -> None:
    """Create vault directories. The whole tree is 0o700 by default — wheel
    payloads regularly contain dist-info tokens, partial credentials baked
    into examples, and other things the user did not consent to making
    world-readable. If a dir already exists at a wider mode (user's choice),
    we leave it alone.
    """
    for d in (BUBBLE_HOME, VAULT_DIR, BUBBLES_DIR, SHELLS_DIR,
              LOGS_DIR, WHEELS_DIR, KEEP_DIR, STAGING_DIR):
        existed = d.exists()
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not existed:
            try:
                d.chmod(0o700)
            except OSError:
                pass


def runner_python_tag() -> str:
    """The wheel-tag fragment for the current interpreter, e.g. 'cp313'."""
    impl = sys.implementation.name
    impl_short = {"cpython": "cp", "pypy": "pp"}.get(impl, impl[:2])
    return f"{impl_short}{sys.version_info.major}{sys.version_info.minor}"


def runner_platform_tag() -> str:
    """Platform tag fragment, e.g. 'manylinux2014_aarch64' or 'linux_aarch64'.
    Best-effort; sysconfig is authoritative when available."""
    plat = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return plat


def detect_host() -> str:
    """Coarse host classification for shim-profile selection."""
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return "termux"
    try:
        is_debian = Path("/etc/debian_version").exists()
    except OSError:
        is_debian = False
    try:
        proc_root_exists = Path("/proc/1/root").exists()
    except OSError:
        proc_root_exists = False

    if is_debian and proc_root_exists:
        # proot-distro distros tend to mount root weirdly; cheap heuristic
        if "proot" in os.environ.get("PROOT_TMP_DIR", "") or os.path.exists("/proc/self/root/.l2s"):
            return "debian-proot"
        return "debian"
    return "linux"


def set_home(home: Path | str) -> None:
    """Rebind every BUBBLE_HOME-derived path to a new root.

    The module-level path constants are read at import time, but other
    modules consult them via attribute access (`config.VAULT_DB`) rather
    than capturing values at their own import. So rebinding the
    attributes here propagates live across the package.

    The intended consumer is `bubble.AgentVault`: an embedding agent
    framework wants a vault separate from the user's `~/.bubble`, and
    setting `BUBBLE_HOME` in the environment doesn't help once bubble
    is already imported. This function is the supported path for
    re-rooting the package after import.
    """
    global BUBBLE_HOME, VAULT_DIR, VAULT_DB, BUBBLES_DIR
    global SHELLS_DIR, LOGS_DIR, WHEELS_DIR, KEEP_DIR, STAGING_DIR
    new_home = Path(home).expanduser().resolve()
    BUBBLE_HOME = new_home
    VAULT_DIR = new_home / "vault"
    VAULT_DB = new_home / "vault.db"
    BUBBLES_DIR = new_home / "bubbles"
    SHELLS_DIR = new_home / "shells"
    LOGS_DIR = new_home / "logs"
    WHEELS_DIR = new_home / "wheels"
    KEEP_DIR = new_home / "keep"
    STAGING_DIR = VAULT_DIR / ".staging"
    os.environ["BUBBLE_HOME"] = str(new_home)
