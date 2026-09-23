"""Chart accessibility tests (WCAG 1.1.1 Non-text Content).

Every Altair/Vega-Lite chart in the app now carries a real, specific
`description` -- the Vega-Lite spec's top-level "description" property.
Confirmed empirically against the actual generated spec (not assumed):
`.properties(description=...)` and a later `.properties(background=...)`
call (render_chart's own theming pass) MERGE rather than one replacing
the other, and Vega-Lite documents "description" as analogous to an
aria-label for the chart's outer element -- i.e. it is NOT rendered as
visible text the way a top-level "title" would be, so it adds no visual
duplication on the many pages that already have a heading right above
the chart. This file verifies that property lands correctly, is
required (not optional) on every one of the five shared builders in
src/ui_components.py, and survives render_chart's later styling pass.

For the six inline `alt.Chart(...)` call sites (src/views/*.py), fully
rendering those view functions would require standing up their full
dashboard-context/data dependencies for little extra confidence beyond
what a direct source inspection already gives -- so that coverage is a
static, AST-based check that every alt.Chart(...) construction in those
files is followed by a .properties(...) call carrying a real,
non-trivial description string literal, which is exactly what was hand-
verified while making each edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

import ui_components as ui

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

_GENERIC_DESCRIPTIONS = {"", "chart", "a chart", "graph"}
_MIN_DESCRIPTION_LENGTH = 20  # rules out a placeholder like "chart" or "data"


def _is_meaningful(description: str) -> bool:
    return bool(description) and description.strip().lower() not in _GENERIC_DESCRIPTIONS and len(description) >= _MIN_DESCRIPTION_LENGTH


# --- shared builders: live functional check against the real spec ---------


def _hour_df():
    return pd.DataFrame({"hour": [8, 9], "hour_label": ["8:00 AM", "9:00 AM"], "v": [3, 5]})


def _category_df():
    return pd.DataFrame({"cat": ["a", "b"], "v": [3, 5]})


def _date_df():
    return pd.DataFrame({"date": pd.to_datetime(["2026-01-01", "2026-01-02"]), "v": [3, 5]})


def _weekday_df():
    return pd.DataFrame({"wd": ["Monday", "Tuesday"], "v": [3, 5]})


def _series_hour_df():
    return pd.DataFrame({"hour": [8, 8], "hour_label": ["8:00 AM", "8:00 AM"], "v": [3, 5], "s": ["a", "b"]})


def _series_date_df():
    return pd.DataFrame({"date": pd.to_datetime(["2026-01-01"] * 2), "v": [3, 5], "s": ["a", "b"]})


def _series_weekday_df():
    return pd.DataFrame({"wd": ["Monday", "Monday"], "v": [3, 5], "s": ["a", "b"]})


@pytest.mark.parametrize(
    "build, kwargs",
    [
        (ui.build_hourly_bar_chart, {"df": _hour_df(), "value_col": "v", "title_y": "Value"}),
        (ui.build_category_bar_chart, {"df": _category_df(), "category_col": "cat", "value_col": "v", "y_title": "Value"}),
        (ui.build_date_line_chart, {"df": _date_df(), "date_col": "date", "value_col": "v", "y_title": "Value"}),
        (ui.build_weekday_line_chart, {"df": _weekday_df(), "weekday_col": "wd", "value_col": "v"}),
        (ui.build_hourly_line_chart, {"df": _hour_df(), "value_col": "v", "title_y": "Value"}),
    ],
    ids=["hourly_bar", "category_bar", "date_line", "weekday_line", "hourly_line"],
)
def test_each_shared_builder_produces_a_meaningful_description_in_the_spec(build, kwargs):
    chart = build(description="A specific, meaningful description of this chart.", **kwargs)
    spec = chart.to_dict()
    assert _is_meaningful(spec.get("description"))


@pytest.mark.parametrize(
    "build, kwargs",
    [
        (ui.build_date_line_chart, {"df": _series_date_df(), "date_col": "date", "value_col": "v", "y_title": "Value", "series_col": "s"}),
        (ui.build_weekday_line_chart, {"df": _series_weekday_df(), "weekday_col": "wd", "value_col": "v", "series_col": "s"}),
        (ui.build_hourly_line_chart, {"df": _series_hour_df(), "value_col": "v", "title_y": "Value", "series_col": "s"}),
    ],
    ids=["date_line_multi_series", "weekday_line_multi_series", "hourly_line_multi_series"],
)
def test_multi_series_branch_also_carries_the_description(build, kwargs):
    # These three builders have a SEPARATE code branch (with series_col vs
    # without) that builds an entirely different chart object -- both
    # branches must set description, not just the single-series one.
    chart = build(description="A specific, meaningful multi-series description.", **kwargs)
    spec = chart.to_dict()
    assert _is_meaningful(spec.get("description"))


@pytest.mark.parametrize(
    "build, args",
    [
        (ui.build_hourly_bar_chart, (_hour_df(), "v", "Value")),
        (ui.build_category_bar_chart, (_category_df(), "cat", "v", "Value")),
        (ui.build_date_line_chart, (_date_df(), "date", "v", "Value")),
        (ui.build_weekday_line_chart, (_weekday_df(), "wd", "v")),
        (ui.build_hourly_line_chart, (_hour_df(), "v", "Value")),
    ],
    ids=["hourly_bar", "category_bar", "date_line", "weekday_line", "hourly_line"],
)
def test_description_is_required_not_optional(build, args):
    # Required (no default) specifically so a caller cannot silently skip
    # writing a real, chart-specific description.
    with pytest.raises(TypeError):
        build(*args)


def test_render_chart_does_not_strip_the_description(monkeypatch):
    # render_chart applies its own .configure_*()/.properties(background=...)
    # theming pass AFTER a builder already set description -- confirms that
    # pass merges rather than replacing it (verified once already by directly
    # inspecting the generated spec; this locks the same property in as a
    # regression test against render_chart specifically).
    captured = []
    monkeypatch.setattr(ui.st, "altair_chart", lambda chart, **_kwargs: captured.append(chart))
    monkeypatch.setattr(ui.st, "get_option", lambda _name: "light")

    chart = ui.build_hourly_bar_chart(_hour_df(), "v", "Value", "A specific, meaningful description.")
    ui.render_chart(chart)

    assert len(captured) == 1
    assert captured[0].to_dict().get("description") == "A specific, meaningful description."


# --- inline alt.Chart(...) call sites: static source verification ---------


_INLINE_CHART_FILES = [
    SRC / "views" / "live_today_view.py",
    SRC / "views" / "reports_errors.py",
    SRC / "views" / "reports_routing.py",
]


def _find_alt_chart_constructions(tree: ast.AST) -> list[ast.Call]:
    """Every `alt.Chart(...)` call node in the module."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Chart"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "alt"
    ]


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_statement(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.stmt:
    """Walks up to the nearest enclosing statement (Assign/Expr/...) --
    each chart-building expression in these files is a single, self-
    contained statement (an assignment or a bare call), so this is
    reliable without needing to reconstruct the exact method-chain shape."""
    cursor = node
    while not isinstance(cursor, ast.stmt):
        cursor = parents[cursor]
    return cursor


def _properties_description_literals(statement: ast.stmt) -> list[str]:
    """Every `.properties(description=...)` string-literal value found
    anywhere within this statement's subtree."""
    found = []
    for node in ast.walk(statement):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "properties"
        ):
            for keyword in node.keywords:
                if keyword.arg == "description":
                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        found.append(keyword.value.value)
                    else:
                        found.append("")  # present but not a plain string literal -- can't verify content statically
    return found


@pytest.mark.parametrize("path", _INLINE_CHART_FILES, ids=lambda p: p.name)
def test_every_inline_alt_chart_call_site_has_a_meaningful_description(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = _build_parent_map(tree)
    chart_constructions = _find_alt_chart_constructions(tree)
    assert chart_constructions, f"expected at least one alt.Chart(...) call in {path.name}"

    missing = []
    for construction in chart_constructions:
        statement = _enclosing_statement(construction, parents)
        descriptions = _properties_description_literals(statement)
        if not descriptions or not any(_is_meaningful(d) for d in descriptions):
            missing.append((construction.lineno, descriptions))

    assert not missing, (
        f"{path.name}: alt.Chart(...) construction(s) at line(s) "
        f"{[ln for ln, _ in missing]} have no meaningful .properties(description=...) -- found {missing}"
    )


def test_inline_chart_file_inventory_matches_the_known_six_call_sites():
    # Pins the total count so a future alt.Chart(...) addition anywhere in
    # these files is caught by this test needing an update, rather than
    # silently shipping without a description.
    total = sum(len(_find_alt_chart_constructions(ast.parse(p.read_text(encoding="utf-8")))) for p in _INLINE_CHART_FILES)
    assert total == 6
