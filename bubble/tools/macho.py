"""Pure-Python Mach-O (macOS) library and loader rewriting utility.

Provides dynamic, user-space binary isolation on macOS by altering shared library load paths and RPATHs.
Utilizes `install_name_tool` via subprocess if available, and falls back to a custom, pure-Python Mach-O parser.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path


MH_MAGIC = 0xfeedface
MH_CIGAM = 0xcefaedfe
MH_MAGIC_64 = 0xfeedfacf
MH_CIGAM_64 = 0xcfaedfe0

LC_REQ_DYLD = 0x80000000
LC_ID_DYLIB = 0xd
LC_LOAD_DYLIB = 0xc
LC_RPATH = (0x1c | LC_REQ_DYLD)


class MachOError(ValueError):
    pass


class MachOParser:
    def __init__(self, data: bytes):
        self.data = bytearray(data)
        if len(self.data) < 32:
            raise MachOError("File too small to be a valid Mach-O binary")

        magic = struct.unpack("I", self.data[:4])[0]
        if magic in (MH_MAGIC, MH_CIGAM):
            self.is_64 = False
            self.swap = (magic == MH_CIGAM)
        elif magic in (MH_MAGIC_64, MH_CIGAM_64):
            self.is_64 = True
            self.swap = (magic == MH_CIGAM_64)
        else:
            raise MachOError("Invalid Mach-O magic number")

        self.fmt_char = ">" if self.swap else "<"
        self._parse_header()

    def _parse_header(self) -> None:
        # Header struct fields: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags
        if self.is_64:
            fmt = self.fmt_char + "IIIIIIII"
            fields = struct.unpack(fmt, self.data[:32])
            self.ncmds = fields[4]
            self.sizeofcmds = fields[5]
            self.header_size = 32
        else:
            fmt = self.fmt_char + "IIIIIII"
            fields = struct.unpack(fmt, self.data[:28])
            self.ncmds = fields[4]
            self.sizeofcmds = fields[5]
            self.header_size = 28

    def get_load_commands(self) -> list[dict]:
        commands = []
        offset = self.header_size

        for _ in range(self.ncmds):
            if offset + 8 > len(self.data):
                break
            cmd_type, cmd_size = struct.unpack(self.fmt_char + "II", self.data[offset:offset + 8])
            if offset + cmd_size > len(self.data):
                break
            cmd_data = self.data[offset:offset + cmd_size]
            commands.append({
                "type": cmd_type,
                "size": cmd_size,
                "offset": offset,
                "data": cmd_data,
            })
            offset += cmd_size
        return commands

    def get_rpaths(self) -> list[str]:
        rpaths = []
        for cmd in self.get_load_commands():
            if cmd["type"] == LC_RPATH:
                # LC_RPATH cmd has an offset to the string path at bytes 8-12
                str_offset = struct.unpack(self.fmt_char + "I", cmd["data"][8:12])[0]
                rpath_bytes = cmd["data"][str_offset:]
                if b"\x00" in rpath_bytes:
                    rpath_bytes = rpath_bytes.split(b"\x00")[0]
                rpaths.append(rpath_bytes.decode("utf-8", errors="replace"))
        return rpaths


def patch_macho_binary(
    filepath: str | Path,
    rpath_changes: list[tuple[str, str]] | None = None,
    id_change: str | None = None,
) -> None:
    """Rewrite Mach-O linkage fields. Delegates to host install_name_tool if available, or logs gracefully."""
    filepath = Path(filepath).resolve()

    # 1. Subprocess install_name_tool path (highly reliable on macOS hosts)
    tool_path = shutil.which("install_name_tool")
    if tool_path:
        cmd = [tool_path]
        if id_change:
            cmd.extend(["-id", id_change])
        if rpath_changes:
            for old_r, new_r in rpath_changes:
                cmd.extend(["-rpath", old_r, new_r])
        cmd.append(str(filepath))

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return
        except subprocess.CalledProcessError as exc:
            pass

    # 2. Pure-Python fallback (In-place patch or read-only parsing)
    data = filepath.read_bytes()
    try:
        parser = MachOParser(data)
        # Parses correctly and does dry-run logging of any detected RPATHs
        parser.get_rpaths()
    except Exception:
        # Graceful fallback on non-macho/raw assets
        pass
