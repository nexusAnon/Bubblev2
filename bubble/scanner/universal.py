"""Universal project scanner (C, C++, Rust, Bash, and Binary executables).

Stage 1 (universal) of the polyglot pipeline: scan_project(project_dir) -> PolyglotSet
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class PolyglotSet:
    project_dir: Path
    python_files: list[Path] = field(default_factory=list)
    c_cpp_files: list[Path] = field(default_factory=list)
    rust_manifests: list[Path] = field(default_factory=list)
    bash_scripts: list[Path] = field(default_factory=list)
    binaries: list[Path] = field(default_factory=list)

    def is_polyglot(self) -> bool:
        return bool(self.c_cpp_files or self.rust_manifests or self.bash_scripts or self.binaries)


def scan_project(project_dir: Path) -> PolyglotSet:
    project_dir = Path(project_dir).resolve()
    pset = PolyglotSet(project_dir=project_dir)

    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue

        # Ignore common build/cache directories
        if any(part in p.parts for part in (".git", ".bubble", "__pycache__", "target", "node_modules", "build")):
            continue

        suffix = p.suffix.lower()
        if suffix == ".py":
            pset.python_files.append(p)
        elif suffix in (".c", ".cpp", ".cc", ".h", ".hpp") or p.name in ("Makefile", "CMakeLists.txt"):
            pset.c_cpp_files.append(p)
        elif p.name == "Cargo.toml":
            pset.rust_manifests.append(p)
        elif suffix == ".sh":
            pset.bash_scripts.append(p)
        else:
            # Check if it is a compiled binary executable (ELF / Mach-O magic numbers)
            try:
                with open(p, "rb") as f:
                    magic = f.read(4)
                if magic in (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xae\xdf\xe0"):
                    pset.binaries.append(p)
                # Check shebang for shell scripts
                elif magic.startswith(b"#!"):
                    with open(p, "r", errors="ignore") as f:
                        first_line = f.readline()
                    if "bash" in first_line or "sh" in first_line:
                        pset.bash_scripts.append(p)
            except OSError:
                pass

    return pset
