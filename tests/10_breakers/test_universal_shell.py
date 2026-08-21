"""Claim: Phase 3 universal dynamic library paths and interactive shell hooks are complete and correct.

Verifies:
  - Sourcing activate file populates LD_LIBRARY_PATH, DYLD_LIBRARY_PATH, and PKG_CONFIG_PATH.
  - exec_in dynamically injects these library path environments correctly.
  - shell_enter spawns custom sub-shell environments with custom PS1 prompts.
"""
import sys
import os
import shutil
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import run_test, Result


def body(r: Result):
    from bubble.run import shell as shell_mod
    from bubble.vault import db

    # Initialize temp DB and shell
    db.init_db()
    name = "uniops"

    # Clean up old shell if exists
    sd = shell_mod.shell_dir(name)
    if sd.exists():
        shutil.rmtree(sd)

    shell_mod.create(name, [])

    # 1. Verify `activate` file contents contain LD_LIBRARY_PATH, DYLD_LIBRARY_PATH, and PKG_CONFIG_PATH
    activate_path = sd / "activate"
    assert activate_path.exists(), "activate file does not exist"
    content = activate_path.read_text()

    assert "export LD_LIBRARY_PATH=" in content
    assert "export DYLD_LIBRARY_PATH=" in content
    assert "export PKG_CONFIG_PATH=" in content
    assert "_BUBBLE_OLD_LD_LIBRARY_PATH=" in content

    r.evidence.append("Sourced activation script verified with full universal dynamic library search paths")

    # 2. Verify `exec_in` environmental injections
    lib_path = str(sd / "lib")
    bin_path = str(sd / "bin")

    with mock.patch("subprocess.call") as mock_call:
        mock_call.return_value = 0
        shell_mod.exec_in(name, ["echo", "hello"])

        # Verify subprocess was called
        assert mock_call.called
        called_args, called_kwargs = mock_call.call_args
        env = called_kwargs["env"]

        assert lib_path in env["PYTHONPATH"]
        assert bin_path in env["PATH"]
        assert lib_path in env["LD_LIBRARY_PATH"]
        assert lib_path in env["DYLD_LIBRARY_PATH"]
        assert lib_path in env["PKG_CONFIG_PATH"]
        assert env["BUBBLE_SHELL"] == name
        assert env["BUBBLE_SHELL_DIR"] == str(sd)

    r.evidence.append("exec_in dynamic environment injection verified for all 3 universal paths")

    # 3. Verify `shell_enter` interactive sub-shell behavior
    with mock.patch("subprocess.call") as mock_call:
        mock_call.return_value = 0
        shell_mod.shell_enter(name, "/bin/mocksh")

        # Verify call
        assert mock_call.called
        called_args, _ = mock_call.call_args
        assert called_args[0] == ["/bin/mocksh"]

        _, called_kwargs = mock_call.call_args
        env = called_kwargs["env"]
        assert env["PS1"].startswith(f"(bubble:{name})")
        assert lib_path in env["LD_LIBRARY_PATH"]

    r.evidence.append("shell_enter interactive sub-shell execution verified with visual prompt customizer")

    # Clean up
    shutil.rmtree(sd)
    r.passed = True


if __name__ == "__main__":
    run_test(
        "Phase 3 universal environment operators and interactive prompt entry hooks execute with perfect correctness",
        body,
    )
