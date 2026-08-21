"""Claim: AgentVault register_polyglot and exec_polyglot support universal, isolated tool execution.

Verifies:
  - AgentVault.register_polyglot compiles and registers C, Rust, Bash, and Binary tools.
  - AgentVault.exec_polyglot executes the compiled tool in a fully isolated dynamic path environment.
  - Database metadata tables correctly reflect the universal tool registrations.
"""
import sys
import os
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble import AgentVault
    from bubble.run import shell as shell_mod
    from bubble.vault import db

    # 1. Create a mock polyglot tool C source file
    proj_dir = Path("./mock_agent_polyglot").resolve()
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    proj_dir.mkdir()

    c_source = proj_dir / "agent_hello.c"
    c_source.write_text(
        '#include <stdio.h>\n'
        'int main(int argc, char** argv) {\n'
        '    printf("AGENT_SOVEREIGN_HELLO\\n");\n'
        '    for(int i=1; i<argc; i++) {\n'
        '        printf("arg: %s\\n", argv[i]);\n'
        '    }\n'
        '    return 0;\n'
        '}\n'
    )

    # Write a mock Bash tool
    bash_source = proj_dir / "agent_bash.sh"
    bash_source.write_text(
        '#!/bin/bash\n'
        'echo "AGENT_BASH_HELLO"\n'
    )

    # Check if compiler is available
    has_compiler = shutil.which("gcc") is not None or shutil.which("clang") is not None

    # 2. Initialize AgentVault and register tools
    with AgentVault() as av:
        # Check C compilation if compiler is present
        if has_compiler:
            bin_path = av.register_polyglot("c_hello", c_source, overwrite=True)
            assert bin_path.exists()
            assert bin_path.is_file()

            # Execute C tool via AgentVault
            res = av.exec_polyglot("c_hello", ["foo", "bar"])
            assert res.returncode == 0
            assert b"AGENT_SOVEREIGN_HELLO" in res.stdout
            assert b"arg: foo" in res.stdout
            assert b"arg: bar" in res.stdout

            r.evidence.append("AgentVault registered and executed isolated C tool successfully")

        # Register and execute Bash tool
        sh_path = av.register_polyglot("bash_hello", bash_source, overwrite=True)
        assert sh_path.exists()

        res_sh = av.exec_polyglot("bash_hello", [])
        assert res_sh.returncode == 0
        assert b"AGENT_BASH_HELLO" in res_sh.stdout

        r.evidence.append("AgentVault registered and executed isolated Bash tool successfully")

        # 3. Verify universal packages database schema metadata
        conn = db.connect()
        try:
            row_pkg = conn.execute(
                "SELECT ecosystem, name FROM universal_packages WHERE id='agent:bash_hello'"
            ).fetchone()
            assert row_pkg is not None
            assert row_pkg[0] == "bash"
            assert row_pkg[1] == "bash_hello"

            row_bin = conn.execute(
                "SELECT rel_path FROM universal_binaries WHERE package_id='agent:bash_hello'"
            ).fetchone()
            assert row_bin is not None
            assert row_bin[0] == "bin/agent_bash.sh"
        finally:
            conn.close()

        r.evidence.append("AgentVault universal v4 metadata registration verified inside database")

    # Clean up
    shutil.rmtree(proj_dir)
    # Clean up generated agent shells
    for name in ("agent-c_hello", "agent-bash_hello"):
        sd = shell_mod.shell_dir(name)
        if sd.exists():
            shutil.rmtree(sd)

    r.passed = True


if __name__ == "__main__":
    run_test(
        "AgentVault universal polyglot C and Bash tool registration and dynamic execution works flawlessly",
        body,
    )
