"""Claim: Phase 2 database schema v4 and binary parsing engines are complete and correct.

Verifies:
  - Database schema initializes schema v4, and includes the new universal tracking tables.
  - Cascading deletes and foreign keys work cleanly on the universal schema tables.
  - ELFParser successfully parses a mock 64-bit ELF header and identifies PT_INTERP segments.
  - MachOParser successfully parses a mock 64-bit Mach-O header and identifies LC_RPATH load commands.
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    # 1. Database Schema v4 Verification
    from bubble.vault import db
    from bubble import config

    # Re-route BUBBLE_HOME dynamically to verify schema creation
    db.init_db()
    conn = db.connect()

    # Confirm version metadata is 4
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    assert row is not None, "schema_meta version not found"
    assert int(row[0]) == 4, f"expected schema version 4, got {row[0]}"

    # Verify new tables exist
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required_tables = {
        "universal_packages", "universal_files", "universal_package_files",
        "universal_dependencies", "universal_binaries"
    }
    missing = required_tables - tables
    assert not missing, f"missing universal tables: {missing}"

    # Test insertion and cascade constraints
    conn.execute(
        "INSERT INTO universal_packages (id, name, version, ecosystem, sha256, vault_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("npm:esbuild:0.18.2:x64", "esbuild", "0.18.2", "npm", "abc123sha", "vault/esbuild")
    )
    conn.execute(
        "INSERT INTO universal_files (sha256, size, mtime_ns, is_executable) "
        "VALUES (?, ?, ?, ?)",
        ("file123sha", 1024, 123456789, 1)
    )
    conn.execute(
        "INSERT INTO universal_package_files (package_id, rel_path, file_sha256) "
        "VALUES (?, ?, ?)",
        ("npm:esbuild:0.18.2:x64", "bin/esbuild", "file123sha")
    )
    conn.execute(
        "INSERT INTO universal_binaries (package_id, binary_name, rel_path) "
        "VALUES (?, ?, ?)",
        ("npm:esbuild:0.18.2:x64", "esbuild", "bin/esbuild")
    )

    # Check that they exist
    assert conn.execute("SELECT COUNT(*) FROM universal_packages").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM universal_binaries").fetchone()[0] == 1

    # Verify cascade on delete
    conn.execute("DELETE FROM universal_packages WHERE id=?", ("npm:esbuild:0.18.2:x64",))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM universal_packages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM universal_package_files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM universal_binaries").fetchone()[0] == 0

    conn.close()
    r.evidence.append("v4 universal SQLite tables verified successfully with cascade deletion")

    # 2. ELF Parser Verification
    from bubble.tools.elf import ELFParser, PT_INTERP, ELFCLASS64, ELFDATA2LSB

    # Construct a mock 64-bit ELF header
    # e_ident: magic (4), class (1), endianness (1), version (1), pad (9) = 16 bytes
    # e_type (2), e_machine (2), e_version (4), e_entry (8), e_phoff (8), e_shoff (8)
    # e_flags (4), e_ehsize (2), e_phentsize (2), e_phnum (2), e_shentsize (2), e_shnum (2), e_shstrndx (2)
    mock_elf = bytearray(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8) # e_ident

    # Appending: e_type=2 (ET_EXEC), e_machine=62 (EM_X86_64), e_version=1
    mock_elf.extend(struct_pack("<HHI", 2, 62, 1))
    # e_entry=0x400000, e_phoff=64 (immediately after header), e_shoff=0
    mock_elf.extend(struct_pack("<QQQ", 0x400000, 64, 0))
    # e_flags=0, e_ehsize=64, e_phentsize=56 (64-bit Program Header size), e_phnum=1
    # e_shentsize=0, e_shnum=0, e_shstrndx=0
    mock_elf.extend(struct_pack("<IHHHHHH", 0, 64, 56, 1, 0, 0, 0))

    # Append Program Header 1: type=3 (PT_INTERP), flags=4 (R), offset=120, vaddr=0, paddr=0, filesz=20, memsz=20, align=1
    mock_elf.extend(struct_pack("<IIQQQQQQ", 3, 4, 120, 0, 0, 20, 20, 1))

    # Pad to interpreter offset
    mock_elf.extend(b"\x00" * (120 - len(mock_elf)))
    mock_elf.extend(b"/lib64/ld-linux.so\x00")

    parser = ELFParser(bytes(mock_elf))
    assert parser.elf_class == ELFCLASS64
    assert parser.get_interpreter() == "/lib64/ld-linux.so"

    # Test in-place patch
    parser.patch_interpreter_inplace("/lib/ld-custom.so")
    assert parser.get_interpreter() == "/lib/ld-custom.so"

    r.evidence.append("ELFParser correctly parsed and patched mock 64-bit ELF PT_INTERP")

    # 3. Mach-O Parser Verification
    from bubble.tools.macho import MachOParser, MH_MAGIC_64, LC_RPATH

    # Construct a mock 64-bit Mach-O header
    # magic (4), cputype (4), cpusubtype (4), filetype (4), ncmds (4), sizeofcmds (4), flags (4), reserved (4) = 32 bytes
    mock_macho = bytearray()
    mock_macho.extend(struct_pack("<I", MH_MAGIC_64)) # magic
    mock_macho.extend(struct_pack("<IIIIIII", 0x01000007, 3, 2, 1, 32, 0x00200000, 0))

    # Append LC_RPATH load command
    # cmd (4), cmdsize (4), path offset (4)
    # RPATH string: "/usr/lib" + padding to 8 bytes alignment
    cmd_data = bytearray()
    cmd_data.extend(struct_pack("<III", LC_RPATH, 24, 12))
    cmd_data.extend(b"/usr/lib\x00\x00\x00\x00")
    mock_macho.extend(cmd_data)

    macho_parser = MachOParser(bytes(mock_macho))
    assert macho_parser.is_64 is True
    assert macho_parser.get_rpaths() == ["/usr/lib"]

    r.evidence.append("MachOParser correctly parsed mock 64-bit Mach-O LC_RPATH load command")
    r.passed = True


def struct_pack(fmt: str, *args) -> bytes:
    return struct.pack(fmt, *args)


import struct


if __name__ == "__main__":
    run_test(
        "Phase 2 universal v4 SQLite tables and native ELF/Mach-O parsers work with exact precision",
        body,
    )
