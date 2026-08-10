# Bubble — test gallery

Each entry below is an architectural claim that was tested by running real code against the actual `bubble` package on this machine. The tests are also exhibits — read them to learn what the system does.

_Run: 2026-08-10T03:47:13 — 44 passed, 0 failed, 0 skipped._

---

## ✓ a fresh BUBBLE_HOME yields a usable vault DB on schema v2

`00_sanity/test_vault_initializes.py` — 45 ms

```
vault_db: /tmp/bubble-test-yojosmn4/vault.db
tables: 14 (bubbles, dependencies, module_imports, modules, packages, schema_meta, shells, top_level, universal_binaries, universal_dependencies, universal_files, universal_package_files, universal_packages, vault_files)
packages PK: ['name', 'version', 'wheel_tag']
```

## ✓ bubble.AgentVault is a consumption-shape embedding API: agent runtimes can vault, register tools by alias with declarable isolation, and import them as modules — diamond-conflict dissolution surfaced as one library instead of as a CLI

`10_breakers/test_agent_vault_embedding.py` — 142 ms

```
AgentVault() constructs into BUBBLE_HOME, vault empty
staged 2 versions of agentdemo: [('agentdemo', '1.0.0', 'py3-none-any'), ('agentdemo', '2.0.0', 'py3-none-any')]
register('greeter', version='1.0.0') + tool('greeter') returns a usable in_process module
second alias 'greeter_new' bound to v2.0.0; both surfaces live concurrently from one AgentVault
v1 alias unchanged after v2 alias registered — diamond conflict dissolved through the embedding API
registered_tools(): ['greeter', 'greeter_new']
close() removes registered aliases from sys.modules and drops the meta-finder
AgentVault(home=av-second-home-a3ivr6po) created separate vault root with its own SQLite index
```

## ✓ bridge orchestrates main + legacy runtimes while preserving strict defaults and hardening

`10_breakers/test_bridge_routes_and_hardens.py` — 11 ms

```
.py routes to main bubble run with isolation by default
legacy route is fail-closed unless --allow-legacy-network is explicit
bridge runs with reduced, hardened environment
```

## ✓ bundle → unbundle is the deployment surface: source manifest in, tar.gz out, target vault.db rebuilt from source's recorded facts, alias substrate field preserved, integrity edge survives the wire (post-extract tampering caught by verify against source's sha256)

`10_breakers/test_bundle_round_trip.py` — 204 ms

```
bundled: 2 packages, 15 files, 2155 bytes
tar layout: manifest + vault subtree + shell tree (22 entries)
unbundled into fresh home: 2 packages, integrity clean
target db: packages=2, vault_files=8, shells=1
alias substrate field survived bundle → unbundle
target shell symlinks resolve to extracted vault
target verify(alpha): 4 matched, clean
post-extract tampering caught by target verify (integrity edge survives transport)
```

## ✓ the canonical name returned by the index is cross-validated against the requested name (PEP 503 normalized) — a swap refuses before download, so the vault never holds bytes under a name the operator didn't request

`10_breakers/test_canonical_name_validated.py` — 614 ms

```
name swap refused before any download
  message: index returned name 'substituted' for request 'original'; refusing to vault under a name we didn't ask for
```

## ✓ bubble.tools.diff exposes compare / fuzz / bisect as the differential-evaluation verb over AgentVault — the substrate's multi-version coexistence becomes an answer to 'what changed?' without any consumer needing to rebuild the alias machinery

`10_breakers/test_diff_primitive.py` — 123 ms

```
compare(v0, v2): v0=('ok', 'small') v2=('ok', 'compact') identical=False
compare(v0, v1): identical=True (both report 'small')
fuzz(20 inputs): 20 divergences; boundaries={('widget_v1', 'widget_v2'): 20}
bisect: boundary=('widget_v1', 'widget_v2') (evaluations=3, endpoints=('ok', ('v', 'small')) → ('ok', ('v', 'compact')))
bisect(agreeing endpoints): boundary=None (no change to localize)
alias-name redaction: identical AttributeError across 3 aliases fingerprints as one (no spurious divergence)
→ compare / fuzz / bisect compose over AgentVault: differential evaluation is a verb the substrate exposes, not something each consumer rebuilds
```

## ✓ vault drift refuses the lookup at the meta-finder AND surfaces a [[failures]] entry of kind vault_drift_modified in host.toml — the first place the closed loop is load-bearing rather than decorative; cached per-process so repeat lookups don't double-record

`10_breakers/test_drift_refuses_and_records.py` — 82 ms

```
clean verify: 4 matched, 0 drifted, 0 missing
drift verify: drifted=[('canary/__init__.py', 'vault_drift_modified')]
refused lookup; recorded 1 drift entries
  most recent target: canary==1.0.0@py3-none-any
per-process cache prevents re-recording on repeat lookup
```

## ✓ a stdlib-only run with autofetch on leaves the vault empty

`10_breakers/test_empty_script_fetches_nothing.py` — 58 ms

```
stdlib imports: 10
vault.packages rows: 0
wheels/ entries:    0
→ demand paging: zero touched, zero paid
```

## ✓ fetcher refuses non-allowlisted download URLs (off-host, http, file://) before any network work — a poisoned simple-API response can't redirect us

`10_breakers/test_fetcher_refuses_off_host_url.py` — 54 ms

```
http://files.pythonhosted.org/...           → rejected
https://evil.example.com/wheel.whl          → rejected
file:///etc/passwd                          → rejected
https://files.pythonhosted.org/...          → admitted
→ poisoned-mirror redirects fail before any bytes are fetched
```

## ✓ aliases resolve flat single-file modules (e.g. six.py), not only package-directory layouts — two versions of a flat dist coexist as distinct module objects in one process

`10_breakers/test_flat_module_alias.py` — 82 ms

```
flatmod_old.__file__: /tmp/bubble-test-jye0cxya/vault/flatmod/1.0.0/py3-none-any/flatmod.py
flatmod_new.__file__: /tmp/bubble-test-jye0cxya/vault/flatmod/2.0.0/py3-none-any/flatmod.py
flatmod_old.where(): 'v1'
flatmod_new.where(): 'v2'
→ flat single-file dists alias as cleanly as packages
```

## ✓ BUBBLE_PYPI_INDEX must be https; http / file / ftp / etc. are refused at fetch time — per-file sha256 only authenticates a channel we already trust, and TLS is the only thing making the index responses themselves trustworthy

`10_breakers/test_https_index_required.py` — 57 ms

```
http   refused: refusing non-https index URL: 'http://pypi.org/simple' (BUBBLE_PYPI_INDEX must u
ftp    refused: refusing non-https index URL: 'ftp://pypi.org/simple' (BUBBLE_PYPI_INDEX must us
file   refused: refusing non-https index URL: 'file:///tmp/index' (BUBBLE_PYPI_INDEX must use ht
```

## ✓ vault import-venv refuses symlinked RECORD entries: the bytes a content-addressed vault serves under a name must come from the file the dist's RECORD names, not from wherever a symlink chain happens to terminate

`10_breakers/test_importer_refuses_symlinks.py` — 71 ms

```
vault contents under evil/: ['__init__.py']
symlink RECORD entry refused; non-symlinked modules pass through
symlink target's bytes never reached the vault
```

## ✓ a late-arriving alias does not retroactively corrupt earlier imports — Bubble's isolation is temporal, not just spatial

`10_breakers/test_late_alias_does_not_corrupt_earlier.py` — 83 ms

```
t0: widget_old.VERSION=1.0.0, hello='v1 says hi', calls=1
t1: widget_new arrives. VERSION=2.0.0, hello='v2 says hi'
t2: re-using widget_old. VERSION=1.0.0, hello='v1 says hi', calls=2
module id stable:  0x7fe2e65c2430 → 0x7fe2e65c2430
class id stable:   0x5573a4245b60 → 0x5573a4245b60
STATE dicts distinct: old@0x7fe2e65ca600 vs new@0x7fe2e65caf00
→ time axis: a late alias did not contaminate earlier state
```

## ✓ importlib.metadata queries from inside an alias resolve against that alias's vault dist-info — not the host's, not a sibling alias's; the diamond-conflict story holds for metadata-driven packages, not just hardcoded-__version__ ones

`10_breakers/test_metadata_per_alias.py` — 115 ms

```
widget_old via importlib.metadata: version='1.0.0', name='widget'
widget_new via importlib.metadata: version='2.0.0', name='widget'
→ each alias resolves importlib.metadata against its own vault dist-info; the host's view does not leak across the alias boundary
```

## ✓ vault-add populates modules, module_imports (split into stdlib-and-own-pkg-filtered externals), and dependencies (Requires-Dist parsed) — the three tables that schema v2 declared but never wrote

`10_breakers/test_modules_and_deps_indexed.py` — 68 ms

```
modules: ['gizmo', 'gizmo.helpers']
gizmo            external imports: ['requests']
gizmo.helpers    external imports: ['urllib3']
dependencies parsed: name + spec + optional + extra
  ('pytest', '', 1, 'test')
  ('requests', '<3,>=2.0', 0, None)
  ('urllib3', '>=1.21.1', 0, None)
re-stage overwrites: no duplicate rows under same key
```

## ✓ bubble can build its own deployment artifact via bubble run

`10_breakers/test_recursive_self_host.py` — 851 ms

```
build script: tools/build_pyz.py
bubble run tools/build_pyz.py: rc=0
produced artifact: 131565 bytes
sidecar sha256 matches bytes: 771d99d35d733c8b…
produced pyz --help responds and lists bubble subcommands
produced pyz `vault list` returned: 'vault is empty'
deterministic: two builds same source → identical sha256 771d99d35d733c8b…
```

## ✓ vault-only is the default for bubble's runtime — every fetch is an explicit authorization (--fetch CLI flag or BUBBLE_AUTOFETCH=1). A bare `bubble run` cannot reach PyPI, no matter what the script tries to import

`10_breakers/test_run_default_no_network.py` — 11 ms

```
default mode: autofetch=False, vault-miss → None spec
opt-in via BUBBLE_AUTOFETCH=1: autofetch=True at install
```

## ✓ sdist-only releases are refused by default — running setup.py is RCE under the user's privileges, a sovereignty break the vault exists to prevent. --allow-sdist / BUBBLE_ALLOW_SDIST=1 toggles the gate explicitly.

`10_breakers/test_sdist_refused_by_default.py` — 620 ms

```
default refuse: sdist blocked before any download
  message: fakepkg==1.0.0 is only available as an sdist; vaulting an sdist runs its setup.py / build backend, which the vault refus
opt-in via BUBBLE_ALLOW_SDIST=1 changes the failure shape
  downstream failure: HTTPError
```

## ✓ bubble shell create follows the Requires-Dist closure: pinning a single package pulls its transitive deps into the shell, because the vault's `dependencies` table already knows the graph

`10_breakers/test_shell_create_follows_requires_dist.py` — 126 ms

```
shell at /tmp/bubble-test-o3bdjhcv/shells/closuretest
lib contents: ['alpha', 'alpha-1.0.0.dist-info', 'beta', 'beta-1.0.0.dist-info', 'gamma', 'gamma-1.0.0.dist-info']
alpha + beta + gamma all linked from closure
```

## ✓ deployment manifest round-trips through shell.add_pinned: exact (name, version, wheel_tag) triplets become shell-state entries; alias substrate fields are preserved for C5; drift in any pin refuses the link via the C1∩C4 join

`10_breakers/test_shell_create_from_manifest.py` — 157 ms

```
manifest: 2 packages, 1 aliases
shell-state matches deployment-manifest [packages] exactly
alias substrate field preserved through to shell row metadata
drifted pin refused at link time with named target
host.toml gained 1 failure entries; 1 of kind vault_drift_modified
```

## ✓ shell creation links *.dist-info dirs into lib/, so importlib.metadata.distribution() / .entry_points() inside the shell sees the same distributions the source vault has on disk — without this, entry-point-driven runtimes silently see nothing

`10_breakers/test_shell_create_links_dist_info.py` — 214 ms

```
shell at /tmp/bubble-test-0wiggiw_/shells/distinfotest
dist-info symlinked: distinfopkg-3.1.4.dist-info
METADATA reachable: Name: distinfopkg
importlib.metadata.distribution: distinfopkg 3.1.4
```

## ✓ shell creation merges namespace-package contributions: when N>1 vault packages claim the same top-level import name, lib/<top>/ becomes a real directory of subdir-symlinks rather than a single dir-symlink that shadows all but one. Closes the diamond-conflict story for namespace-distributed packages like opentelemetry

`10_breakers/test_shell_create_merges_namespace_packages.py` — 190 ms

```
shell at /tmp/bubble-test-c0s7hjfk/shells/nstest
lib/ns/ contents: ['__init__.py', 'alpha.py', 'beta.py']
both contributions importable: alpha beta
4-way merge stable: lib/ns/ = ['__init__.py', 'alpha.py', 'beta.py', 'shared.py']
```

## ✓ shell create / add_pinned refuses pins that aren't in the vault, with shell_pkg_missing recorded to host.toml — observable at create time, not deferred to first invocation

`10_breakers/test_shell_create_refuses_orphan_targets.py` — 92 ms

```
manifest after create: ['real-pkg']
phantom-pkg recorded as shell_pkg_missing (1 entries)
add_pinned refused phantom-pkg==9.9.9: ['phantom-pkg==9.9.9@py3-none-any']
```

## ✓ every top_level row carries a content sha256 over its subtree, populated at vault-add — the import-name → bytes edge is cryptographic

`10_breakers/test_top_level_carries_content_hash.py` — 83 ms

```
alpha import_sha256: 8a94beb727299d2f180df11e817b2858ca6f7478a1011c0a7ec543fa9368a4e1
beta  import_sha256: 7eef7974f5e08293d88af8537fb8f37b11d081b3c7cb35a0b1ccc817c882edcd
→ each top_level row binds the import name to its bytes
```

## ✓ import-name collisions across distributions emit a structured contention log entry — silent accident becomes observable event

`10_breakers/test_top_level_contention_logged.py` — 79 ms

```
first claimant:  opencv-python (no log)
second claimant: opencv-python-headless
contention recorded: import_name=cv2
existing: [('opencv-python', '4.10.0', 'py3-none-any')]
incoming sha256: 66e79245b348a9ba…
→ collisions are observable, not silent
```

## ✓ import name resolves to a different distribution name via the SQLite top_level index, with no hardcoded table

`10_breakers/test_top_level_index_bridges_import_to_dist.py` — 82 ms

```
distribution name: Carbohydrate-9000
top-level import:  sugar
top_level row:     ('Carbohydrate-9000', '3.0.0', 'sugar')
resolved module:   /tmp/bubble-test-igq31y3k/vault/Carbohydrate-9000/3.0.0/py3-none-any/sugar/__init__.py
→ no hardcoded mapping needed; the dist-info IS the mapping
```

## ✓ top_level.txt is verified against the staged tree — asserted-but-absent names are dropped, so no row claims bytes that don't exist

`10_breakers/test_top_level_verify_mode.py` — 60 ms

```
top_level.txt asserted: ['real', 'ghost']
verified subpaths exist for: ['real']
recorded in top_level: ['real']
→ wheel self-attestation is verified, not trusted
```

## ✓ two versions of the same package coexist in one process via aliases, with distinct classes and asymmetric isinstance

`10_breakers/test_two_versions_one_process.py` — 88 ms

```
widget_old.Widget: id=0x55dbc0c232b0
widget_new.Widget: id=0x55dbc0c25100
widget_old hello:  'I am widget v1'
widget_new hello:  'I am widget v2'
isinstance asymmetric: v1∈v2=False, v2∈v1=False
→ two versions, one process, distinct classes
```

## ✓ Phase 3 universal environment operators and interactive prompt entry hooks execute with perfect correctness

`10_breakers/test_universal_shell.py` — 56 ms

```
Sourced activation script verified with full universal dynamic library search paths
exec_in dynamic environment injection verified for all 3 universal paths
shell_enter interactive sub-shell execution verified with visual prompt customizer
```

## ✓ Phase 2 universal v4 SQLite tables and native ELF/Mach-O parsers work with exact precision

`10_breakers/test_universal_v4.py` — 53 ms

```
v4 universal SQLite tables verified successfully with cascade deletion
ELFParser correctly parsed and patched mock 64-bit ELF PT_INTERP
MachOParser correctly parsed mock 64-bit Mach-O LC_RPATH load command
```

## ✓ ensure_dirs creates BUBBLE_HOME, vault, staging, shells, wheels, logs at 0o700 — wheel payloads are not in general world-readable, and the vault should match

`10_breakers/test_vault_dir_perms.py` — 2 ms

```
  bubble-test-u1e8clyr mode=0o700
  vault              mode=0o700
  .staging           mode=0o700
  shells             mode=0o700
  wheels             mode=0o700
  logs               mode=0o700
```

## ✓ AgentVault.register(isolation='subprocess') drives the subprocess substrate from the embedding API: an agent declares the isolation ring per tool, the substrate ladder dispatches accordingly, and two versions of one dist coexist as differently-shaped tools — the consumption surface for diamond-conflict dissolution

`30_loop/test_agent_vault_isolation.py` — 180 ms

```
staged avi 1.0.0 + 2.0.0 in vault
two distinct module objects from registered tools
in_process tool: VERSION='1.0.0', label()='v1-inproc', square(7)=49
subprocess tool: VERSION='2.0.0', label()='v2-subproc', cube(4)=64
isolated tool is IsolatedModule from bubble.substrate.subprocess
v1 local tool correctly missing v2-only attr 'cube'
→ AgentVault.register(isolation='subprocess') routes through the subprocess substrate handler; the agent framework calls tool(alias) and gets a callable proxy. The substrate ladder is reachable as a declarative property of registration.
close() drained the subprocess interp registry
```

## ✓ alias declaring substrate=dlmopen_isolated routes through the substrate handler and yields a callable proxy module: module-level constants reachable, functions invokable with primitive args, two versions of the same package serving distinct surfaces in one process — the diamond conflict dissolved at the link-namespace level

`30_loop/test_dlmopen_routing_through_proxy.py` — 153 ms

```
two distinct module objects from one alias dict
VERSION crosses both substrates: dv1='1.0.0', dv2='2.0.0'
shape() returns its version's value: dv1.shape()='rectangle', dv2.shape()='ellipse'
area(4, 5) — primitive args cross both substrates: dv1=20, dv2=15.7080
v1-only proxy correctly missing v2-only attr 'perimeter'
v2-only function callable: dv2.perimeter(4,5) = 18
→ alias declared substrate=dlmopen_isolated routes through the substrate handler; resulting proxy module is callable from the calling interpreter; v1 and v2 surfaces coexist in one process with distinct semantics. The diamond conflict is dissolved.
```

## ✓ dlmopen-isolated substrate is a verified capability on supporting hosts: a fresh libpython initializes in its own link namespace, a vaulted package loads inside it, and a value from the package crosses the boundary back to the caller — single-call demonstration the README named as reachable

`30_loop/test_dlmopen_substrate_handler.py` — 109 ms

```
dlmopen_isolated available on this host
  status: namespace + interpreter init verified; proxy module bridge online (picklable attrs + primitive calls); object-identity-across-calls not yet plumbed
staged islet==3.1.4 at /tmp/bubble-test-dueyzek1/vault/islet/3.1.4/py3-none-any
isolated interp ran a smoke instruction
VERSION crossed the boundary: '3.1.4'
ANSWER crossed the boundary: '42'
double(21) crossed the boundary: '42'
→ a vaulted package loaded inside an isolated libpython, called, and returned a value across the namespace boundary — single-call dlmopen substrate is real, not sketched
```

## ✓ runtime failures round-trip through host.toml: write via record_failure, read via known_failures, find via is_known_failure

`30_loop/test_failure_recording_round_trip.py` — 27 ms

```
recorded 3 failures via host.record_failure
distinct kinds: ['dlmopen_unavailable', 'pypi_fetch_failed', 'wheel_load_segfault']
round-tripped detail: 'received SIGSEGV during dlopen'
→ channel is plumbed; record→consult half of the loop works
(open: the *next-run-alters-strategy* half is not yet load-bearing)
```

## ✓ bubble probe writes host.toml; the host module reads it back; the substrate menu reflects machine capability

`30_loop/test_probe_writes_host_toml.py` — 26 ms

```
probed_at: 2026-08-10T03:47:12.068520
kernel:    Linux 6.8.0 x86_64
python:    3.12.13 (cpython)
substrates this machine reports it can host:
  - in_process         available                                        cost=0MB
  - sub_interpreter    unavailable                                      cost=1MB
  - dlmopen_isolated   available (multi-call needs GIL-managed re-entry) cost=29MB
  - subprocess         available                                        cost=30MB
→ probe writes, host reads, the portrait is real
```

## ✓ alias declaring substrate=subprocess routes through the substrate handler and yields a callable proxy module: module-level constants reachable, functions invokable with primitive args, two versions of the same package serving distinct surfaces in one caller-process tree — diamond conflict dissolved at the OS-process level, portable everywhere Python runs

`30_loop/test_subprocess_routing_through_proxy.py` — 202 ms

```
two distinct module objects from one alias dict
VERSION crosses both substrates: dv1='1.0.0', dv2='2.0.0'
shape() returns its version's value: dv1.shape()='rectangle', dv2.shape()='ellipse'
area(4, 5) — primitive args cross both substrates: dv1=20, dv2=15.7080
v1 proxy correctly missing v2-only attr 'perimeter'
v2-only function callable: dv2.perimeter(4,5) = 18
→ alias declared substrate=subprocess routes through the substrate handler; resulting proxy module is callable from the calling interpreter; v1 and v2 surfaces coexist in one caller-process via two child processes. Diamond conflict dissolved at the OS-process level.
```

## ✓ subprocess-isolated substrate is a verified capability: a child python spawns, a vaulted package loads inside it, attribute access and primitive function calls cross the OS-process boundary via length-prefixed pickle frames — the structural hole dlmopen's portability constraints left open is closed

`30_loop/test_subprocess_substrate_handler.py` — 144 ms

```
subprocess substrate available on this host
  status: subprocess substrate ready: child python spawnable, pickle channel + proxy module bridge online (picklable attrs + primitive calls); object-identity-across-calls not yet plumbed
staged islet_sub==3.1.4 at /tmp/bubble-test-f8516725/vault/islet_sub/3.1.4/py3-none-any
install_module: islet_sub imported in child
VERSION crossed the boundary: '3.1.4'
ANSWER crossed the boundary: 42
double(21) crossed the boundary: 42
concat(a,b=) crossed: 'left|right'
→ a vaulted package loaded inside a child Python, called, and returned a value across the OS-process boundary via pickle — single-call subprocess substrate is real, not sketched
```

## ✓ substrate routing closes the load-bearing loop: a first-run downgrade records to host.toml, a second-run resolution learns from history without re-probing, and no redundant entries accumulate — every run starts smarter than the last

`30_loop/test_substrate_routing_learns.py` — 83 ms

```
first run: alias resolved, bytes loaded via downgrade
first run: recorded substrate_downgraded (dlmopen_isolated → in_process)
second run: decision learned_from_history=True; actual=in_process without re-probing
second run did not double-record — load-bearing memory, not noise
→ probe → consult → record → consult: the four-step loop is weight-bearing on substrate routing
```

## ✓ shell freeze produces a round-trippable deployment manifest

`70_project/test_project_freeze.py` — 98 ms

```
manifest written: True
packages: ['foxtrot']
foxtrot==5.0.0 @py3-none-any
```

## ✓ project ingest creates shell and .bubble-shell marker

`70_project/test_project_ingest_basic.py` — 105 ms

```
scanned_files: 2
resolved: ['alpha']
linked: [('alpha', '1.0.0', 'py3-none-any', 1)]
```

## ✓ spinoff shell inherits parent scope for packages it does not pin

`70_project/test_project_parent_chain.py` — 130 ms

```
spinoff delta: ('delta', '3.0.0', 'py3-none-any', '/tmp/bubble-test-82lv10jp/vault/delta/3.0.0/py3-none-any')
spinoff echo_pkg (inherited): ('echo-pkg', '4.0.0', 'py3-none-any', '/tmp/bubble-test-82lv10jp/vault/echo-pkg/4.0.0/py3-none-any')
```

## ✓ ingest persists package scope to shell metadata

`70_project/test_project_scope_persisted.py` — 97 ms

```
scope: {'bravo': ('2.0.0', 'py3-none-any')}
```

## ✓ BUBBLE_SHELL pins project-specific version (soft vault isolation)

`70_project/test_project_soft_isolation.py` — 118 ms

```
proj-a charlie: 1.0.0
proj-b charlie: 2.0.0
```
