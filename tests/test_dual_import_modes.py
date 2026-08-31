"""Modules with CLIs must work both as package members and as scripts.

Three modules ship `__main__` blocks and documented command-line usage, e.g.
`python general_invoice_agent.py --base-dir path/to/invoices`. Centralizing path
constants behind relative imports (`from ...paths import ...`) silently broke
all three, because a relative import has no parent package when the file is run
directly -- the import system raises "attempted relative import with no known
parent package" before argparse ever runs.

The package test suite could not catch this: it imports modules through the
package, which is the path that still worked. Only executing them as scripts
exposes it.
"""

import subprocess
import sys

import pytest

from invoice_processing import paths

# Modules that must remain runnable as standalone scripts.
RUNNABLE_MODULES = (
    "shared_libraries/acting/general_invoice_agent.py",
    "shared_libraries/alf_engine.py",
    "shared_libraries/investigation/investigate_agent_reconst.py",
)


@pytest.mark.parametrize("relative_path", RUNNABLE_MODULES)
def test_module_runs_as_a_script(relative_path):
    """`python <module>.py --help` must not fail on imports."""
    script = paths.AGENT_PKG_DIR / relative_path
    assert script.is_file(), f"{script} not found"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=paths.AGENT_ROOT,
    )

    assert "attempted relative import" not in result.stderr, (
        f"{relative_path} cannot run as a script -- a relative import needs a "
        f"parent package. Use the try/except fallback pattern.\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"{relative_path} failed to run as a script:\n{result.stderr}"
    )


@pytest.mark.parametrize("relative_path", RUNNABLE_MODULES)
def test_module_imports_as_package_member(relative_path):
    """The same module must also import normally through the package."""
    module = "invoice_processing." + relative_path.removesuffix(".py").replace(
        "/", "."
    )
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=paths.AGENT_ROOT,
    )
    assert result.returncode == 0, (
        f"{module} failed to import as a package member:\n{result.stderr}"
    )


def test_both_modes_resolve_the_same_paths():
    """Script mode and package mode must agree, or outputs land in two places."""
    script_mode = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'invoice_processing/shared_libraries');"
            " import alf_engine; print(alf_engine.ALF_OUT_DIR)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=paths.AGENT_ROOT,
    )
    assert script_mode.returncode == 0, script_mode.stderr
    # Module import prints a master-data banner first; the path is the last line.
    resolved = script_mode.stdout.strip().splitlines()[-1].strip()
    assert resolved == str(paths.ALF_OUT_DIR), (
        f"bare-name import resolved ALF_OUT_DIR to {resolved!r}, "
        f"package import resolves it to {paths.ALF_OUT_DIR!r}"
    )
