"""WCAG 2.1 AA contrast verification for every pipeline-status color pair
(src/services/pipeline_context_service.py::_STATUS_COLOR_PAIRS).

The prior hardcoded pairs failed AA (>=4.5:1, normal-size text) in 3 of 4
states -- as low as 3.07:1 for the degraded/running amber in light mode.
This file is the enforcement mechanism: every (foreground, background)
pair the app can actually render is computed here with a real WCAG
relative-luminance/contrast-ratio formula and asserted >= 4.5:1, so a
future edit to _STATUS_COLOR_PAIRS that reintroduces a failing pair fails
this test, not just a manual review.

The contrast-ratio helper below is deliberately test-only, not a
production utility: nothing at runtime needs to compute a contrast ratio,
only this test does, so it lives here rather than adding a new module
under src/.
"""

from __future__ import annotations

from services.pipeline_context_service import _STATUS_COLOR_PAIRS, _status_colors

AA_NORMAL_TEXT_MINIMUM = 4.5


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 relative luminance (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG 2.1 contrast ratio (https://www.w3.org/TR/WCAG21/#dfn-contrast-ratio),
    lighter-over-darker so the result is always >= 1."""
    l1, l2 = _relative_luminance(fg_hex), _relative_luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def test_relative_luminance_matches_known_reference_values():
    # Pure black and pure white are the two unambiguous reference points
    # for the WCAG luminance formula -- if this drifts, every ratio below
    # is meaningless.
    assert _relative_luminance("#000000") == 0.0
    assert _relative_luminance("#ffffff") == 1.0


def test_contrast_ratio_matches_known_reference_value():
    # Black-on-white (and white-on-black) is the maximum possible ratio,
    # 21:1, per the WCAG formula -- an independent sanity check on the
    # helper itself before trusting it to grade the app's real colors.
    assert round(_contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert round(_contrast_ratio("#ffffff", "#000000"), 2) == 21.0


def test_every_status_color_pair_meets_aa_for_normal_text():
    failures = []
    for family, by_theme in _STATUS_COLOR_PAIRS.items():
        for theme, (fg, bg) in by_theme.items():
            ratio = _contrast_ratio(fg, bg)
            if ratio < AA_NORMAL_TEXT_MINIMUM:
                failures.append(f"{family}/{theme}: {fg} on {bg} = {ratio:.2f}:1")

    assert not failures, "AA contrast failures:\n" + "\n".join(failures)


def test_status_colors_helper_returns_the_same_pairs_the_dict_holds():
    # _status_colors is the seam pipeline_context_service.py actually
    # calls -- this pins it to the dict this file already verified,
    # rather than re-testing the dict twice under two different names.
    for family, by_theme in _STATUS_COLOR_PAIRS.items():
        assert _status_colors(family, "light") == by_theme["light"]
        assert _status_colors(family, "dark") == by_theme["dark"]


def test_status_colors_treats_unrecognized_theme_base_as_light():
    # Matches every other theme_base check already in this codebase
    # (`"..." if theme_base == "dark" else "..."`) -- anything that isn't
    # literally "dark" must fail safe to the light pair, never KeyError.
    for family, by_theme in _STATUS_COLOR_PAIRS.items():
        assert _status_colors(family, "light") == by_theme["light"]
        assert _status_colors(family, "") == by_theme["light"]
        assert _status_colors(family, "system") == by_theme["light"]


def test_all_four_semantic_families_are_present():
    # Locks in that healthy/degraded/failed/unknown all still exist --
    # pipeline_context_service.py's own classification logic is untouched
    # by this change, but this guards against a future edit silently
    # dropping one of the four families this module promises to cover.
    assert set(_STATUS_COLOR_PAIRS.keys()) == {"healthy", "degraded", "failed", "unknown"}


def test_each_family_defines_both_light_and_dark():
    for family, by_theme in _STATUS_COLOR_PAIRS.items():
        assert set(by_theme.keys()) == {"light", "dark"}, family
