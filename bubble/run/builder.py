"""Sovereign compilation and polyglot builder engine.

Handles building and isolated patching of C, C++, and Rust components into the vault.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..tools.elf import patch_elf_binary
from ..tools.macho import patch_macho_binary


class BuildError(RuntimeError):
    pass


def build_c_cpp(file_path: Path, output_path: Path) -> Path:
    """Compile a single C/C++ source file into an isolated binary executable."""
    file_path = Path(file_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine compiler
    compiler = shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        raise BuildError("No C/C++ compiler (gcc or clang) found on this host. Run `bubble probe`.")

    cmd = [compiler, str(file_path), "-o", str(output_path)]
    # Link with common math / standard libraries if needed
    if file_path.suffix.lower() == ".cpp":
        cmd.append("-lstdc++")

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"C/C++ compilation failed: {exc.stderr.decode('utf-8', errors='replace')}")

    # Patch the compiled binary RPATH/Interpreter to maintain user-space hermeticity
    try:
        patch_elf_binary(output_path, rpath="$ORIGIN/../lib")
    except Exception:
        pass
    try:
        patch_macho_binary(output_path, rpath_changes=[("@loader_path/../lib", "@loader_path/../lib")])
    except Exception:
        pass

    return output_path


def build_rust_cargo(manifest_path: Path, output_dir: Path) -> list[Path]:
    """Run cargo build --release on a Cargo manifest, copy and link-patch outputs."""
    manifest_path = Path(manifest_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    cargo_bin = shutil.which("cargo")
    if not cargo_bin:
        raise BuildError("Rust Cargo toolchain ('cargo') not found on this host. Run `bubble probe`.")

    cmd = [cargo_bin, "build", "--release", "--manifest-path", str(manifest_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, cwd=str(manifest_path.parent))
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"Cargo compilation failed: {exc.stderr.decode('utf-8', errors='replace')}")

    # Find the target binaries
    target_dir = manifest_path.parent / "target" / "release"
    built_binaries = []
    if target_dir.is_dir():
        for item in target_dir.iterdir():
            if item.is_file() and not item.name.startswith(".") and os.access(item, os.X_OK):
                # Copy to output_dir
                dest = output_dir / item.name
                shutil.copy2(item, dest)
                # Patch for hermeticity
                try:
                    patch_elf_binary(dest, rpath="$ORIGIN/../lib")
                except Exception:
                    pass
                try:
                    patch_macho_binary(dest, rpath_changes=[("@loader_path/../lib", "@loader_path/../lib")])
                except Exception:
                    pass
                built_binaries.append(dest)

    return built_binaries
