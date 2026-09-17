"""Tests for collector/task_settings.py -- SortView Collector v1 (Phase
4c). Pure Python XML generation, no Windows/Task Scheduler dependency --
runs cross-platform (including this project's ubuntu-latest CI), which is
exactly why the real registration settings are generated here and
verified by these tests, rather than only existing as PowerShell-native
cmdlet parameters nothing here could check.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from collector.task_settings import (
    DEFAULT_CADENCE_MINUTES,
    DEFAULT_ENABLED,
    DEFAULT_EXECUTION_TIME_LIMIT_HOURS,
    DEFAULT_PRINCIPAL,
    DEFAULT_RESTART_COUNT,
    DEFAULT_RESTART_INTERVAL_MINUTES,
    TASK_NAME,
    TaskDefinition,
    build_task_xml,
)

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _parse(xml_text: str) -> ET.Element:
    return ET.fromstring(xml_text)


def _definition(**overrides) -> TaskDefinition:
    kwargs = {
        "python_exe": r"C:\SortView\Collector\.venv\Scripts\python.exe",
        "config_path": r"C:\ProgramData\SortViewCollector\config\collector_config.json",
        "working_dir": r"C:\SortView\Collector",
        "start_boundary": "2026-09-15T00:00:00",
    }
    kwargs.update(overrides)
    return TaskDefinition(**kwargs)


# --- task name / constants -------------------------------------------------


def test_task_name_is_distinct_from_legacy_and_continuous_agent_names():
    assert TASK_NAME == "SortView Collector"
    assert "sortview-scheduler" not in TASK_NAME.lower()
    assert "canonical" not in TASK_NAME.lower()


def test_defaults_match_approved_phase_4c_values():
    assert DEFAULT_CADENCE_MINUTES == 15
    assert DEFAULT_RESTART_COUNT == 3
    assert DEFAULT_RESTART_INTERVAL_MINUTES == 5
    assert DEFAULT_EXECUTION_TIME_LIMIT_HOURS == 1
    assert DEFAULT_PRINCIPAL == "SYSTEM"


def test_default_enabled_is_false():
    # Real production finding: an enabled trigger is armed the instant
    # registration completes, regardless of Start-ScheduledTask ever
    # being called. Disabled-by-default is the actual safety mechanism,
    # not just wording in a printed message.
    assert DEFAULT_ENABLED is False


# --- TaskDefinition validation ----------------------------------------------


def test_empty_python_exe_rejected():
    with pytest.raises(ValueError):
        _definition(python_exe="")


def test_empty_config_path_rejected():
    with pytest.raises(ValueError):
        _definition(config_path="   ")


def test_non_positive_cadence_rejected():
    with pytest.raises(ValueError):
        _definition(cadence_minutes=0)


def test_negative_restart_count_rejected():
    with pytest.raises(ValueError):
        _definition(restart_count=-1)


def test_non_positive_execution_limit_rejected():
    with pytest.raises(ValueError):
        _definition(execution_time_limit_hours=0)


# --- generated XML: cadence -------------------------------------------------


def test_default_cadence_is_15_minutes():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    interval = root.find(".//t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", _NS)
    assert interval is not None
    assert interval.text == "PT15M"


def test_custom_cadence_is_reflected():
    xml_text = build_task_xml(_definition(cadence_minutes=30))
    root = _parse(xml_text)
    interval = root.find(".//t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", _NS)
    assert interval.text == "PT30M"


def test_repetition_has_no_duration_repeats_indefinitely():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    repetition = root.find(".//t:Triggers/t:TimeTrigger/t:Repetition", _NS)
    assert repetition.find("t:Duration", _NS) is None


def test_no_boot_trigger_present():
    # Deliberate -- see task_settings.py's module docstring for why a
    # separate BootTrigger would be redundant with StartWhenAvailable and
    # risk double-firing right after reboot.
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    assert root.find(".//t:Triggers/t:BootTrigger", _NS) is None


# --- enabled / disabled (real production finding) ---------------------------


def test_default_registration_is_disabled():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    enabled = root.find(".//t:Settings/t:Enabled", _NS)
    assert enabled.text == "false"


def test_explicit_enabled_true_is_reflected():
    xml_text = build_task_xml(_definition(enabled=True))
    root = _parse(xml_text)
    enabled = root.find(".//t:Settings/t:Enabled", _NS)
    assert enabled.text == "true"


def test_trigger_itself_stays_enabled_regardless_of_task_level_setting():
    # The Settings-level Enabled is the master switch (see build_task_xml's
    # docstring) -- the TimeTrigger's own Enabled is intentionally left
    # true either way; it only takes effect once the task itself is
    # enabled, so there's nothing to toggle here.
    for enabled in (True, False):
        xml_text = build_task_xml(_definition(enabled=enabled))
        root = _parse(xml_text)
        trigger_enabled = root.find(".//t:Triggers/t:TimeTrigger/t:Enabled", _NS)
        assert trigger_enabled.text == "true"


# --- overlap policy ----------------------------------------------------------


def test_multiple_instances_policy_is_ignore_new():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    policy = root.find(".//t:Settings/t:MultipleInstancesPolicy", _NS)
    assert policy.text == "IgnoreNew"


# --- restart-on-failure ------------------------------------------------------


def test_restart_on_failure_defaults():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    restart = root.find(".//t:Settings/t:RestartOnFailure", _NS)
    assert restart.find("t:Count", _NS).text == "3"
    assert restart.find("t:Interval", _NS).text == "PT5M"


def test_restart_on_failure_custom_values():
    xml_text = build_task_xml(_definition(restart_count=5, restart_interval_minutes=2))
    root = _parse(xml_text)
    restart = root.find(".//t:Settings/t:RestartOnFailure", _NS)
    assert restart.find("t:Count", _NS).text == "5"
    assert restart.find("t:Interval", _NS).text == "PT2M"


# --- execution time limit ----------------------------------------------------


def test_execution_time_limit_default_is_one_hour():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    limit = root.find(".//t:Settings/t:ExecutionTimeLimit", _NS)
    assert limit.text == "PT1H"


def test_execution_time_limit_is_bounded_not_unlimited():
    # Unlike the (frozen, unused) continuous agent's task, the collector
    # is a short-lived one-shot script -- a finite limit is correct here.
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    limit = root.find(".//t:Settings/t:ExecutionTimeLimit", _NS)
    assert limit.text != "PT0S"  # PT0S is Task Scheduler's "no limit" value


# --- principal / run identity -----------------------------------------------


def test_default_principal_is_system():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    user_id = root.find(".//t:Principals/t:Principal/t:UserId", _NS)
    assert user_id.text == "SYSTEM"


def test_custom_principal_is_reflected():
    xml_text = build_task_xml(_definition(principal="DOMAIN\\svc-sortview"))
    root = _parse(xml_text)
    user_id = root.find(".//t:Principals/t:Principal/t:UserId", _NS)
    assert user_id.text == "DOMAIN\\svc-sortview"


def test_run_level_is_highest_available():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    run_level = root.find(".//t:Principals/t:Principal/t:RunLevel", _NS)
    assert run_level.text == "HighestAvailable"


# --- action: command / arguments / working directory ------------------------


def test_action_command_is_exact_venv_python_path():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    command = root.find(".//t:Actions/t:Exec/t:Command", _NS)
    assert command.text == r"C:\SortView\Collector\.venv\Scripts\python.exe"


def test_action_arguments_invoke_collector_run_with_config():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert arguments.text == (
        '-m collector.run --config "C:\\ProgramData\\SortViewCollector\\config\\collector_config.json"'
    )


def test_action_working_directory_matches_install_root():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    working_dir = root.find(".//t:Actions/t:Exec/t:WorkingDirectory", _NS)
    assert working_dir.text == r"C:\SortView\Collector"


# --- start-when-available (reboot survival mechanism) ------------------------


def test_start_when_available_is_true():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    start_when_available = root.find(".//t:Settings/t:StartWhenAvailable", _NS)
    assert start_when_available.text == "true"


# --- generated XML is well-formed and stable ---------------------------------


def test_generated_xml_parses_without_error():
    xml_text = build_task_xml(_definition())
    ET.fromstring(xml_text)  # raises if malformed


def test_start_boundary_defaults_to_now_when_not_specified():
    definition = TaskDefinition(
        python_exe=r"C:\x\python.exe", config_path=r"C:\x\config.json", working_dir=r"C:\x",
    )
    xml_text = build_task_xml(definition)
    root = _parse(xml_text)
    start_boundary = root.find(".//t:Triggers/t:TimeTrigger/t:StartBoundary", _NS)
    assert start_boundary is not None
    assert start_boundary.text  # non-empty, some ISO-shaped timestamp


def test_special_characters_in_config_path_are_escaped():
    xml_text = build_task_xml(_definition(config_path=r"C:\path with & spaces\config.json"))
    root = _parse(xml_text)  # would raise on unescaped "&" if broken
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert "path with & spaces" in arguments.text


# --- frozen (PyInstaller) install: Arguments must not reference -m collector.run ---


def test_frozen_defaults_to_false_source_mode_arguments_unchanged():
    xml_text = build_task_xml(_definition())
    root = _parse(xml_text)
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert arguments.text == (
        '-m collector.run --config "C:\\ProgramData\\SortViewCollector\\config\\collector_config.json"'
    )


def test_frozen_true_uses_dispatcher_subcommand_not_python_module():
    xml_text = build_task_xml(_definition(frozen=True))
    root = _parse(xml_text)
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert arguments.text == (
        'run --config "C:\\ProgramData\\SortViewCollector\\config\\collector_config.json"'
    )
    assert "-m collector.run" not in arguments.text


def test_frozen_true_command_is_whatever_python_exe_was_given():
    # python_exe's field NAME is unchanged (backward compatible) -- for a
    # frozen install the caller passes SortViewCollector.exe's own path
    # through it; build_task_xml doesn't care, it only decides Arguments.
    xml_text = build_task_xml(_definition(frozen=True, python_exe=r"C:\SortView\Collector\SortViewCollector.exe"))
    root = _parse(xml_text)
    command = root.find(".//t:Actions/t:Exec/t:Command", _NS)
    assert command.text == r"C:\SortView\Collector\SortViewCollector.exe"


def test_frozen_true_everything_else_unchanged():
    # Cadence/overlap/restart/execution-limit/principal/enabled-default
    # are identical regardless of frozen -- only Arguments differs.
    source_xml = _parse(build_task_xml(_definition(frozen=False)))
    frozen_xml = _parse(build_task_xml(_definition(frozen=True, start_boundary="2026-09-15T00:00:00")))
    for xpath in (
        ".//t:Triggers/t:TimeTrigger/t:Repetition/t:Interval",
        ".//t:Settings/t:MultipleInstancesPolicy",
        ".//t:Settings/t:RestartOnFailure/t:Count",
        ".//t:Settings/t:RestartOnFailure/t:Interval",
        ".//t:Settings/t:ExecutionTimeLimit",
        ".//t:Settings/t:Enabled",
        ".//t:Principals/t:Principal/t:UserId",
        ".//t:Principals/t:Principal/t:RunLevel",
    ):
        assert source_xml.find(xpath, _NS).text == frozen_xml.find(xpath, _NS).text, xpath


def test_cli_frozen_flag_produces_dispatcher_style_arguments(tmp_path):
    from collector.task_settings import main

    output_path = tmp_path / "task.xml"
    exit_code = main([
        "--python-exe", r"C:\SortView\Collector\SortViewCollector.exe",
        "--config-path", r"C:\ProgramData\SortViewCollector\config\collector_config.json",
        "--working-dir", r"C:\SortView\Collector",
        "--output", str(output_path),
        "--frozen",
    ])
    assert exit_code == 0
    root = ET.fromstring(output_path.read_text(encoding="utf-16"))
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert arguments.text.startswith("run --config ")


def test_cli_without_frozen_flag_keeps_python_module_arguments(tmp_path):
    from collector.task_settings import main

    output_path = tmp_path / "task.xml"
    exit_code = main([
        "--python-exe", r"C:\SortView\Collector\.venv\Scripts\python.exe",
        "--config-path", r"C:\ProgramData\SortViewCollector\config\collector_config.json",
        "--working-dir", r"C:\SortView\Collector",
        "--output", str(output_path),
    ])
    assert exit_code == 0
    root = ET.fromstring(output_path.read_text(encoding="utf-16"))
    arguments = root.find(".//t:Actions/t:Exec/t:Arguments", _NS)
    assert arguments.text.startswith("-m collector.run --config ")
