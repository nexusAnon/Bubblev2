# BUBBLE UNIVERSAL

## Point-and-Absorb Atomic Component Architecture & Universal Shell Operator

This document specifies the architectural blueprint to expand Bubble from a standard-library-only Python demand-pager into a **universal repository-level component absorber, atomic cache, and environment maintainer**.

By leveraging Bubble’s core primitives—content-addressed vaulting, cryptographically bound integrity edges, and host-failure-informed routing—Bubble Universal acts as an offline-first, zero-overhead, hermetic installer and operator for multi-language, multi-runtime codebases.

---

## 1. Core Vision & Philosophy

A modern project is rarely pure Python. It is a polyglot weave: a React frontend, a Rust or Go compiler toolchain, a PostgreSQL database server, C-shared libraries (`.so` / `.dylib`), static templates, and shell orchestration scripts.

Nix solves this through an elegant, declarative, pure functional language, but requires root/privileged namespaces, is notoriously hard to learn, and forces a total rewrite of existing build patterns. Docker solves this by packing an entire operating system kernel layer, introducing massive disk overhead, loss of native execution speeds, and complex host-to-container boundary crossings.

**Bubble Universal** offers a third path: **demand-paged, in-process, user-space hermeticity**.
You point Bubble at an arbitrary repository; it scans the manifests (`package.json`, `Cargo.toml`, `go.mod`, `Makefile`, etc.), absorbs all artifacts and system dependencies at an atomic, content-addressed level, rewrites their linker hooks to point only into the vault, and dynamically maintains a shadow shell environment.

### The Five Pillars of Bubble Universal:
1. **Manifest Agnosticism (The Universal Ingestion Scanner):** Scans build manifests and maps the project's dependency graph across boundaries (e.g. Python calling a Node tool calling a Rust compiled binary).
2. **Content-Addressed Storage (CAS) for All Components:** Every binary executable, shared library, header file, and config template is decomposed into an atomic hash-addressed file system.
3. **Linker Hermeticity (User-Space Isolation):** Native binaries (`ELF` on Linux, `Mach-O` on macOS) are dynamically patched on ingestion (using `patchelf` or `install_name_tool`) to rewrite their `RPATH` / `RUNPATH` and dynamic loader interpreters to point strictly into the vault. No global state, no container boundary.
4. **Universal Shell Operator (Live Environment Maintainer):** Intercepts shell execution, maintaining active state reconciliation over `PATH`, `LD_LIBRARY_PATH`, and template configuration files.
5. **The Feedback Loop of Sovereignty:** Extends `host.toml` (`bubble host`) to track non-Python tools, compilers, and architectures, enabling the agent and the human to execute polyglot code safely.

---

## 2. Ingestion Pipeline & Abstract Component Tree

When pointed at a repository (`bubble project ingest <dir>`), Bubble parses the structure into an **Abstract Component Tree (ACT)**.

```
[Repository Root]
  ├── package.json     ──> (npm Scanner)   ──> [Node Components]   ──┐
  ├── Cargo.toml       ──> (Cargo Scanner) ──> [Rust Components]   ──┼─> [ACT] ──> [Universal Vault]
  ├── main.py          ──> (AST Scanner)   ──> [Python Components] ──┤
  └── libsqlite3.so    ──> (ELF Scanner)   ──> [Binary Components] ──┘
```

### The Ingestion Verification & Capture Loop:
1. **Scan Phase:** Recurse the repository directory. Discover language manifests and binary dependencies.
2. **Resolve Phase:** Map identified components to the SQLite vault database.
3. **Fetch Phase:** For missing elements, fetch compiled pre-built binaries or lockfile closures.
4. **Isolation/Patching Phase:** Ensure all compiled assets are fully hermetic.
5. **Keep Capture & Wire:** Convert the local repository assets into a Bubble `keep` so they are backed up in the vault, symlinking back to the active workspace.

---

## 3. Database Schema Extensions (v4)

To support multi-language, atomic files, and shell configurations, the schema extends from SQLite v3 to v4.

```sql
-- Represents an atomic package / module / compiler toolchain across any ecosystem
CREATE TABLE universal_packages (
    id TEXT PRIMARY KEY,               -- e.g. "npm:esbuild:0.18.2:x64-linux" or "cargo:ripgrep:13.0.0"
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    ecosystem TEXT NOT NULL,           -- 'python', 'npm', 'cargo', 'binary', 'system'
    platform_tag TEXT,                 -- e.g. "manylinux2014_x86_64"
    sha256 TEXT NOT NULL,              -- Cryptographic hash over the entire package folder
    vault_path TEXT NOT NULL           -- RelPath within ~/.bubble/vault/
);

-- Content-Addressed Storage (CAS) for every single file in the vault
CREATE TABLE universal_files (
    sha256 TEXT PRIMARY KEY,           -- Hash of file content bytes
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    is_executable INTEGER DEFAULT 0
);

-- Maps packages to their composite files
CREATE TABLE universal_package_files (
    package_id TEXT,
    rel_path TEXT NOT NULL,
    file_sha256 TEXT,
    FOREIGN KEY (package_id) REFERENCES universal_packages(id),
    FOREIGN KEY (file_sha256) REFERENCES universal_files(sha256),
    PRIMARY KEY (package_id, rel_path)
);

-- Generalizes the dependencies table beyond Python Requires-Dist
CREATE TABLE universal_dependencies (
    parent_id TEXT,
    dependency_id TEXT,
    is_optional INTEGER DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES universal_packages(id),
    FOREIGN KEY (dependency_id) REFERENCES universal_packages(id)
);

-- Maps binaries and commands provided by universal packages
CREATE TABLE universal_binaries (
    package_id TEXT,
    binary_name TEXT NOT NULL,         -- e.g., "rg" or "node"
    rel_path TEXT NOT NULL,            -- path inside the package folder
    FOREIGN KEY (package_id) REFERENCES universal_packages(id),
    PRIMARY KEY (package_id, binary_name)
);
```

---

## 4. Linker Hermeticity & RPATH Rewriting

The biggest challenge with universal binary caching is **dependency hell for compiled assets**—a binary expecting `libcrypto.so.1.1` in `/usr/lib` fails if the host only has `/usr/lib/libcrypto.so.3`.

Bubble Universal solves this by performing dynamic **RPATH/RUNPATH and Dynamic Interpreter Rewriting** on binary absorption.

```
[Host Binary] ──────────> Linked against /lib64/ld-linux-x86-64.so.2 & host libs
       │
  (Absorption & Rewriting)
       ▼
[Vaulted Binary] ───────> Interpreter: ~/.bubble/vault/glibc/2.35/lib/ld-linux-x86-64.so.2
                         RPATH:       ~/.bubble/vault/openssl/1.1/lib/:$ORIGIN/../lib
```

### The Rewriting Mechanics (Python-Native implementation):
* **On Linux (ELF):**
  If `patchelf` is available (as recorded in `host.toml`), Bubble uses it. Otherwise, Bubble utilizes a pure-Python minimal ELF parser/writer implemented in `bubble/tools/elf.py` to rewrite the `.interp` section and append the target vault paths to the `DT_RUNPATH` in the `.dynamic` section.
* **On macOS (Mach-O):**
  Bubble utilizes `install_name_tool` or a native Mach-O parser to rewrite load commands (`LC_LOAD_DYLIB`), substituting absolute host paths with `@rpath/` or relative `@loader_path/` directives pointing straight back into the cached library entries.

This ensures that any binary compiled for a specific architecture runs identically in user-space, regardless of the host's actual glibc/linker setup.

---

## 5. Universal Shell Operator & Environment Maintainer

Once a repository's components are absorbed and link-patched, Bubble operates as an **Environment Maintainer**. It does this by materializing the shell on demand and reconciling state dynamically.

```bash
# Point bubble at a repository and spawn a managed hermetic shell
bubble shell enter --repo /path/to/my-repo
```

### Mechanics of the Shell Operator:
1. **Shadow Path Materialization:**
   Bubble creates an ephemeral or persistent named shell inside `~/.bubble/shells/`. It populates `lib/` and `bin/` directories using symlinks pointing into the content-addressed store.
2. **Environment Variable Injection:**
   On entering the shell, Bubble overrides and manages environment variables:
   * `PATH`: Prepends `~/.bubble/shells/<repo-shell>/bin` and any specific toolchain `bin` directories (e.g. node, cargo, python).
   * `LD_LIBRARY_PATH` / `DYLD_LIBRARY_PATH`: Prepends the local shell `lib/` directory so unpatched or fallback binaries find the correct isolated shared libraries.
   * `PKG_CONFIG_PATH`: Ensures compiler tools find the correct vaulted `.pc` pkgconfig metadata.
3. **Dynamic State Reconciliation Loop:**
   While the shell is active, a background thread or a shell prompt pre-exec hook (`zsh_directory_changed` / `PROMPT_COMMAND`) watches the repository's configuration.
   * If a user modifies a template, Bubble dynamically regenerates config values.
   * If `package.json` changes, Bubble prompts or auto-pages the delta packages into the vault and updates the active symlink layout without requiring a manual rebuild.

---

## 6. Implementation Phasing Plan

To execute this feature cleanly without introducing regressions, we suggest a three-phase approach:

### Phase 1: Keep & Toolchain Extension (Incremental)
* Extend the `keep` module to support metadata tagging for language runtimes (detecting Cargo, Node, and Python layouts).
* Update `bubble probe` to detect native system compilers (`gcc`, `clang`, `patchelf`, `install_name_tool`, `cargo`, `node`, `npm`).
* Write basic `keep` activations for non-Python directories.

### Phase 2: Binary Link-Rewriting Engine (The Isolation Core)
* Implement `bubble.tools.elf` and `bubble.tools.macho` to parse and safely alter native binary linker sections inside the vault's staging directory.
* Extend `vault.db` to v4 schema, adding the tracking tables for files, binaries, and cross-ecosystem package ids.
* Test with compiling simple native projects inside a Bubble sandbox.

### Phase 3: Universal Shell Hook & Live Maintainer (The Human Interface)
* Implement `bubble shell enter` with interactive shell hooks for `bash`, `zsh`, and `fish`.
* Add the directory-watching pre-prompt hook to detect local manifest changes and synchronize the active symlinks.
* Wire the universal orchestrator into the `AgentVault` API, giving autonomous agents instant access to fully-contained, self-building workspaces.

---

## 7. Comparative Landscape

| Feature | Docker | Nix | Bubble Universal |
| :--- | :--- | :--- | :--- |
| **Virtualization Overhead** | Heavy (Hypervisor/Container Namespace) | None (User/Root Namespaces) | **None (Pure User-Space)** |
| **Performance** | Degradation on macOS/Windows | Native | **Native** |
| **Disk Footprint** | Gigabytes (Layer Duplication) | Hundreds of MBs | **Minimal (Content-Addressed & Hardlinked)** |
| **Setup Complexity** | High (Dockerfile, Port mapping, Volumes) | High (Nix expression language) | **Zero (Point-and-Absorb, automatic discovery)** |
| **Portability** | Requires Docker Daemon / Linux Kernel | Requires Nix installation + Root | **Single `bubble.pyz` binary (No dependencies)** |
| **Isolation Level** | Hard OS / Network Isolation | Strict build-time sandboxing | **Temporal & Spatial Module Link Isolation** |

---

*This blueprint establishes a sovereign, self-contained path toward polyglot dependency isolation—affirming that software execution should be governed by active, observed, and verified cryptographic realities rather than global machine state.*
