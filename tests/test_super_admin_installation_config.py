"""Super Admin onboarding: the three installer values, and the legacy config left alone.

The scheduled Collector's collector_config.json is written on the library's
machine by install.ps1 from -CustomerId / -BranchId / -InstallationId. Super
Admin's job is to create the collector_installations row FIRST, keep the
returned id, and show the three values (Operational Customer ID, Operational
Branch ID, Installation ID) plus a paste-ready command fragment.

build_collector_agent_config generates the LEGACY agent_config.json, which the
scheduled Collector does not consume; it is deliberately unchanged and is not
presented as the Collector's config.

The pages are Streamlit scripts (they call st.* and require_super_admin at
import time), so they are not imported. The functions that build a config are
extracted from the page source with `ast` and run for real; ordering and the
operator-facing labels are asserted against the source.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import collector
from src.services import tenant_service

ROOT = Path(__file__).resolve().parent.parent
PROVISION = ROOT / "super_admin" / "pages" / "Provision_Library.py"
MANAGE = ROOT / "super_admin" / "pages" / "Manage_Libraries.py"

_LEGACY_CONFIG_KEYS = {
    "database_url", "customer_id", "branch_id", "raw_checkins_file", "raw_rejects_file",
    "processed_checkins_file", "processed_rejects_file", "checkins_history_file",
    "rejects_history_file", "status_file", "api_url", "raw_acs_file",
    "processed_acs_file", "acs_history_file",
}


_CALL_CLOSE = chr(10) + " " * 8 + ")"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_function(path: Path, name: str, namespace: dict):
    tree = ast.parse(_source(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)  # nosec B102 - test-only, repo source
    return namespace[name]


# --- the legacy agent-config generator is untouched -----------------------------

def test_build_collector_agent_config_keeps_its_prior_signature():
    parameters = list(inspect.signature(tenant_service.build_collector_agent_config).parameters)

    assert parameters == [
        "operational_customer_id", "operational_branch_id", "api_url",
        "raw_checkins_file", "raw_rejects_file", "raw_acs_file",
    ]


def test_build_collector_agent_config_output_has_no_installation_id():
    config = tenant_service.build_collector_agent_config(
        operational_customer_id=50, operational_branch_id=2, api_url="https://x.test/",
    )

    assert set(config) == _LEGACY_CONFIG_KEYS
    assert "installation_id" not in config


def test_neither_page_feeds_an_installation_id_into_the_legacy_config():
    for path in (PROVISION, MANAGE):
        source = _source(path)
        call = source[source.index("build_collector_agent_config(\n"):]
        call = call[: call.index(_CALL_CLOSE)]  # the call's own closing line
        assert "api_url" in call, path.name  # sanity: we sliced the whole call
        assert "installation" not in call, path.name


# --- Provision Library ----------------------------------------------------------

_CONFIG_INPUTS = {
    "api_url": "https://api.example.test",
    "raw_checkins_file": r"C:\TLCFinalDlls\Checkins.txt",
    "raw_rejects_file": r"C:\TLCFinalDlls\Rejects.txt",
    "raw_acs_file": r"C:\TLCFinalDlls\ACS Log.txt",
}


def test_operational_stage_still_takes_no_installation_and_returns_the_legacy_config():
    identity = {"operational_customer_id": 50, "operational_branch_id": 2}
    namespace = {
        "Any": object,
        "assign_operational_identity": lambda organization_id, branch_id: identity,
        "build_collector_agent_config": tenant_service.build_collector_agent_config,
        "__builtins__": __builtins__,
    }
    run_operational_stage = _load_function(PROVISION, "run_operational_stage", namespace)
    assert list(inspect.signature(run_operational_stage).parameters) == [
        "organization_id", "branch_id", "config_inputs",
    ]

    returned_identity, config, error = run_operational_stage(1, 2, _CONFIG_INPUTS)

    assert error is None and returned_identity == identity
    assert set(config) == _LEGACY_CONFIG_KEYS


def test_the_installation_is_created_first_and_its_returned_record_is_retained():
    source = _source(PROVISION)
    submit = source[source.index("if submitted:"):]

    assert submit.index("create_collector_installation(") < submit.index("run_operational_stage(")
    # The returned record (with its id) is what is kept and later displayed.
    assert '"collector_installation": installation,' in submit
    assert 'result_installation["id"]' in source


def test_provisioning_shows_the_three_installer_values_separately_and_the_fragment():
    source = _source(PROVISION)

    for label in ("Operational Customer ID", "Operational Branch ID", "Installation ID"):
        assert f'.metric("{label}"' in source, label
    assert 'result_identity["operational_customer_id"]' in source
    assert 'result_identity["operational_branch_id"]' in source
    assert "format_collector_install_parameters(" in source
    assert 'language="powershell"' in source


def test_provisioning_says_the_legacy_config_is_not_the_collectors_config():
    source = _source(PROVISION)

    assert "Legacy agent_config.json" in source
    assert "NOT the scheduled" in source and "collector_config.json" in source


def _collector_version_field_defaults(path: Path) -> list[ast.expr]:
    """The `value=` expression of every st.text_input("Collector version", ...) on a page."""
    defaults = []
    for node in ast.walk(ast.parse(_source(path))):
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "Collector version"
        ):
            defaults.extend(keyword.value for keyword in node.keywords if keyword.arg == "value")
    return defaults


@pytest.mark.parametrize("page", [PROVISION, MANAGE], ids=["provision", "manage"])
def test_installation_form_default_version_is_the_collector_version_authority(page):
    # The default is collector.__version__ itself (imported), never a copy of the number: a release
    # bump must not need this page -- or this test -- edited.
    imports = [n for n in ast.walk(ast.parse(_source(page))) if isinstance(n, ast.ImportFrom) and n.module == "collector"]
    assert [(a.name, a.asname) for n in imports for a in n.names] == [("__version__", "COLLECTOR_VERSION")]

    defaults = _collector_version_field_defaults(page)

    assert defaults, "the page no longer has a 'Collector version' field"
    assert [ast.unparse(d) for d in defaults].count("COLLECTOR_VERSION") == 1  # the add/provision form's default
    assert not [d for d in defaults if isinstance(d, ast.Constant)]  # ...and no field holds a literal release


@pytest.mark.parametrize("page", [PROVISION, MANAGE], ids=["provision", "manage"])
def test_the_pages_own_path_setup_can_import_the_version_authority_in_an_isolated_interpreter(page):
    # The deployed app (Streamlit, started from src/app.py) does not have the repository root on sys.path
    # by itself. Run the page's REAL import prelude -- extracted from its source -- in an isolated interpreter
    # (no PYTHONPATH, no working directory on sys.path) so only the page's own path handling can supply `collector`.
    path_names = {"ROOT_DIR", "SRC_DIR", "SUPER_ADMIN_DIR"}
    wanted = [
        node for node in ast.parse(_source(page)).body
        if (isinstance(node, ast.Import) and {a.name for a in node.names} <= {"os", "sys"})
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in path_names for t in node.targets))
        or (isinstance(node, ast.If) and "sys.path" in ast.unparse(node))
        or (isinstance(node, ast.ImportFrom) and node.module == "collector")
    ]
    program = chr(10).join([
        "import json, sys",
        f"__file__ = {str(page)!r}",
        "before = set(sys.modules)",
        ast.unparse(ast.Module(body=wanted, type_ignores=[])),
        (
            "print(json.dumps({'version': COLLECTOR_VERSION, 'new': sorted(set(sys.modules) - before), "
            "'last_path': sys.path[-1], 'first_path': sys.path[0], 'root': ROOT_DIR}))"
        ),
    ])

    result = subprocess.run(  # nosec B603 - test-only: this interpreter, code built from repo source
        [sys.executable, "-I", "-c", program], cwd=Path(tempfile.gettempdir()), capture_output=True, text=True,
        timeout=60, check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["version"] == collector.__version__
    # Importing the package pulls in nothing else -- no third-party module can be missing in the deployed app.
    assert set(report["new"]) - {"__future__"} == {"collector"}, report["new"]
    # The repository root is appended, so it cannot shadow anything that was already importable.
    assert report["last_path"] == report["root"] == str(ROOT) and report["first_path"] != report["root"]


# --- Manage Libraries -------------------------------------------------------------

def test_manage_libraries_config_builder_is_unchanged():
    namespace = {
        "st": SimpleNamespace(secrets={"AGENT_API_BASE_URL": "https://api.example.test/"}),
        "build_collector_agent_config": tenant_service.build_collector_agent_config,
        "operational_id": lambda value: None if value is None else int(value),
        "__builtins__": __builtins__,
    }
    build_agent_config = _load_function(MANAGE, "build_agent_config", namespace)
    assert list(inspect.signature(build_agent_config).parameters) == ["row"]

    config = build_agent_config({"operational_customer_id": 50, "operational_branch_id": 2})
    unmapped = build_agent_config({"operational_customer_id": None, "operational_branch_id": 2})

    assert set(config) == _LEGACY_CONFIG_KEYS
    assert unmapped is None


def test_manage_libraries_shows_the_three_installer_values_and_the_fragment():
    source = _source(MANAGE)
    section = source[source.index('st.subheader("Collector Installer Values")'):]
    section = section[: section.index('st.subheader("Pipeline Status")')]

    for label in ("Operational Customer ID", "Operational Branch ID", "Installation ID"):
        assert f'.metric("{label}"' in section, label
    assert "format_collector_install_parameters(" in section
    assert 'language="powershell"' in section


def test_manage_libraries_only_offers_live_installations_of_the_primary_branch():
    source = _source(MANAGE)

    assert 'INSTALLER_INSTALLATION_STATUSES = ("provisioning", "active")' in source
    # inactive/retired are rejected by the API, and the operational ids shown are
    # the PRIMARY branch's -- another branch's installation would never match.
    assert 'int(row["branch_id"]) == int(primary_branch_id)' in source
    assert 'row["status"] in INSTALLER_INSTALLATION_STATUSES' in source


def test_manage_libraries_labels_the_legacy_config_and_lists_installation_ids():
    source = _source(MANAGE)

    assert "Legacy agent_config.json" in source
    assert "NOT the scheduled" in source
    assert '"id": "Installation ID"' in source


def test_the_installation_id_is_never_a_free_text_or_number_input():
    for path in (PROVISION, MANAGE):
        source = _source(path)
        assert "number_input" not in source, path.name
        assert 'text_input("Installation ID' not in source, path.name


# --- the paste-ready fragment -------------------------------------------------------

def test_install_parameters_are_the_three_labelled_ids_in_installer_order():
    fragment = tenant_service.format_collector_install_parameters(
        operational_customer_id=50, operational_branch_id=2, installation_id=41,
    )

    assert fragment == "-CustomerId 50 -BranchId 2 -InstallationId 41"


def test_install_parameters_use_the_operational_ids_not_the_installation_id_for_scope():
    fragment = tenant_service.format_collector_install_parameters(50, 2, 41)

    assert "-CustomerId 50 " in fragment and "-BranchId 2 " in fragment
    assert fragment.endswith("-InstallationId 41")


@pytest.mark.parametrize(
    ("customer_id", "branch_id", "installation_id", "message"),
    [
        (None, 2, 41, "Operational identity is not assigned"),
        (50, None, 41, "Operational identity is not assigned"),
        (50, 2, None, "Installation ID is not available"),
        (50, 2, 0, "Installation ID is not available"),
        (50, 2, True, "Installation ID is not available"),
    ],
)
def test_install_parameters_are_refused_when_any_id_is_missing(customer_id, branch_id, installation_id, message):
    with pytest.raises(ValueError, match=message):
        tenant_service.format_collector_install_parameters(customer_id, branch_id, installation_id)
