"""Every module must resolve filesystem paths through invoice_processing.paths.

Six modules used to compute these constants independently. Four derived the repo
root as ``AGENT_PKG_DIR.parent.parent.parent`` -- correct only while the agent
sat three directories deep inside the adk-samples repo. After the agent moved,
that expression pointed above the repo, so `.env` was never found and every
dotenv call silently did nothing. The same defect had already been fixed once in
eval.py; it survived in five other places because each kept its own copy.

These tests fail if a module reintroduces its own root.
"""

import importlib

import pytest

from invoice_processing import paths

# Modules that expose path constants and must agree with paths.py.
PATH_AWARE_MODULES = (
    "invoice_processing.core.config",
    "invoice_processing.agent",
    "invoice_processing.tools.tools",
    "invoice_processing.shared_libraries.alf_engine",
    "invoice_processing.shared_libraries.acting.general_invoice_agent",
    "invoice_processing.shared_libraries.investigation.investigate_agent_reconst",
)


@pytest.fixture(scope="module")
def loaded_modules() -> dict:
    return {name: importlib.import_module(name) for name in PATH_AWARE_MODULES}


def test_every_module_imports(loaded_modules):
    """Guards the investigation module in particular -- it has no other tests."""
    assert len(loaded_modules) == len(PATH_AWARE_MODULES)


@pytest.mark.parametrize("constant", ["AGENT_PKG_DIR", "PROJECT_ROOT"])
def test_modules_agree_on_roots(loaded_modules, constant):
    """One root, not six."""
    values = {
        name: getattr(module, constant)
        for name, module in loaded_modules.items()
        if hasattr(module, constant)
    }
    assert values, f"no module exposes {constant}"

    distinct = {str(v) for v in values.values()}
    assert len(distinct) == 1, (
        f"{constant} diverged across modules: {values}. "
        f"Import it from invoice_processing.paths instead of recomputing it."
    )


def test_project_root_is_the_repo_root():
    """The regression: the root must contain the repo, not sit above it."""
    assert paths.AGENT_ROOT == paths.AGENT_PKG_DIR.parent
    assert (paths.AGENT_ROOT / "pyproject.toml").is_file(), (
        f"{paths.AGENT_ROOT} is not the repo root"
    )
    assert (paths.AGENT_ROOT / "invoice_processing").is_dir()


def test_env_path_points_at_the_repo_root():
    """`.env` lives beside pyproject.toml, not three levels up."""
    assert paths.ENV_PATH == paths.AGENT_ROOT / ".env"
    assert paths.ENV_PATH.parent == paths.AGENT_ROOT


def test_data_paths_live_inside_the_package():
    """All runtime output stays self-contained under the package."""
    for path in (
        paths.DATA_DIR,
        paths.AGENTIC_FLOW_OUT,
        paths.ALF_OUT_DIR,
        paths.SESSIONS_DIR,
        paths.RULE_BASE_PATH,
        paths.RULES_BOOK_PATH,
    ):
        assert paths.AGENT_PKG_DIR in path.parents, (
            f"{path} escapes the package directory"
        )


def test_configured_files_exist():
    """Catch a path that resolves cleanly but points at nothing."""
    assert paths.RULE_BASE_PATH.is_file()
    assert paths.RULES_BOOK_PATH.is_file()
    assert paths.MASTER_DATA_PATH.is_file()
    assert paths.EXEMPLARY_DIR.is_dir()
