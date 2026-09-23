"""Tests for src/ui_components.py::render_kpi_card.

WCAG 1.3.1 Info and Relationships fix: the card's outer element carries
role="group" with aria-labelledby referencing the title and value
elements' own ids, so a screen reader announces them as one named group
instead of two unrelated text nodes -- without duplicating that text into
a separate aria-label (which would cause it to be announced twice).

render_kpi_card has no widget state and no interaction -- it is a single
st.markdown(html, unsafe_allow_html=True) call -- so these tests
monkeypatch streamlit.markdown/get_option directly to capture the exact
HTML string produced, rather than driving a full AppTest script.
"""

from __future__ import annotations

import re

import pytest

import ui_components as ui


@pytest.fixture
def captured_html(monkeypatch):
    calls = []
    monkeypatch.setattr(ui.st, "markdown", lambda html, **_kwargs: calls.append(html))
    monkeypatch.setattr(ui.st, "get_option", lambda _name: "light")
    return calls


def _render(captured_html, **kwargs):
    defaults = {"title": "Checkins Today", "value": "1,204"}
    defaults.update(kwargs)
    ui.render_kpi_card(**defaults)
    assert len(captured_html) == 1
    return captured_html[0]


# --- semantic grouping ------------------------------------------------------


def test_card_is_exposed_as_one_aria_group(captured_html):
    html = _render(captured_html)
    assert 'role="group"' in html


def test_title_and_value_are_programmatically_associated(captured_html):
    html = _render(captured_html, title="Checkins Today", value="1,204")

    labelledby_match = re.search(r'aria-labelledby="([^"]+)"', html)
    assert labelledby_match, f"no aria-labelledby found in: {html}"
    referenced_ids = labelledby_match.group(1).split()
    assert len(referenced_ids) == 2

    # Every id aria-labelledby references must actually exist as a real
    # id="..." on an element in the same markup -- a dangling reference
    # would silently produce no accessible name at all.
    for ref_id in referenced_ids:
        assert f'id="{ref_id}"' in html, f"aria-labelledby references {ref_id!r}, but no element has that id"

    # The two referenced elements are specifically the title and the
    # value -- confirmed by checking each one's own text content, not
    # just that some id happens to match.
    title_id, value_id = referenced_ids
    assert re.search(rf'id="{title_id}"[^>]*>Checkins Today<', html)
    assert re.search(rf'id="{value_id}"[^>]*>1,204<', html)


def test_group_label_does_not_include_a_separate_duplicated_aria_label(captured_html):
    # The requirement is explicit: do not duplicate the whole card's
    # content into aria-label (which would cause repeated announcements
    # alongside the normal linear reading of the card's own text). The
    # association must come from referencing the existing elements only.
    html = _render(captured_html)
    assert "aria-label=" not in html


def test_two_cards_on_the_same_page_never_collide_on_id(captured_html):
    html_a = _render(captured_html, title="Card A", value="1")
    captured_html.clear()
    html_b = _render(captured_html, title="Card B", value="2")

    ids_a = set(re.findall(r'id="([^"]+)"', html_a))
    ids_b = set(re.findall(r'id="([^"]+)"', html_b))
    assert ids_a.isdisjoint(ids_b)


# --- subtitle content ---------------------------------------------------


def test_subtitle_text_is_rendered_when_provided(captured_html):
    html = _render(captured_html, subtitle="5% of all checkins")
    assert "5% of all checkins" in html


def test_no_subtitle_element_when_subtitle_is_empty(captured_html):
    html_with = _render(captured_html, subtitle="some context")
    captured_html.clear()
    html_without = _render(captured_html, subtitle="")
    # The subtitle div only exists when there is a subtitle -- unchanged
    # from the pre-existing behavior, just re-asserted here as a
    # regression guard alongside the new structural changes.
    assert "some context" in html_with
    assert html_without.count('font-size:0.98rem') == 0


# --- HTML escaping / injection safety ------------------------------------


@pytest.mark.parametrize(
    "field", ["title", "value", "subtitle"],
)
def test_special_characters_are_escaped_not_rendered_as_markup(captured_html, field):
    dangerous = '<script>alert(1)</script> & "quoted" \'quoted\''
    html = _render(captured_html, **{field: dangerous})

    assert "<script>alert(1)</script>" not in html
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quoted&quot; &#x27;quoted&#x27;"
        in html
    )


def test_value_is_escaped_by_default(captured_html):
    html = _render(captured_html, value="<b>not bold</b>")
    assert "<b>not bold</b>" not in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_value_is_html_true_renders_real_markup_for_trusted_content(captured_html):
    # The one legitimate use: ui_components.format_hour's own trusted,
    # developer-authored output (a small styled AM/PM span), never
    # end-user or admin-supplied text.
    trusted_html_value = ui.format_hour(7)
    html = _render(captured_html, value=trusted_html_value, value_is_html=True)
    assert "<span style=" in html
    assert "&lt;span" not in html


def test_value_is_html_defaults_to_false(captured_html):
    # Passing real HTML without the explicit opt-in must NOT render as
    # markup -- the safe default protects every call site that doesn't
    # know to ask for it.
    html = _render(captured_html, value="<span>x</span>")
    assert "<span>x</span>" not in html
    assert "&lt;span&gt;x&lt;/span&gt;" in html


def test_escaped_content_cannot_break_out_of_the_surrounding_element(captured_html):
    # A title/subtitle containing an unescaped '>' or '"' could otherwise
    # prematurely close a tag or attribute it is embedded near. The
    # payload legitimately appears in the output -- but only as INERT
    # escaped text, never as a live, parseable <img ... onerror=...> tag.
    html = _render(captured_html, title='"><img src=x onerror=alert(1)>')
    assert "<img" not in html
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in html


# --- visual styling is preserved -----------------------------------------


def test_default_visual_styling_is_unchanged(captured_html):
    html = _render(captured_html)
    assert "border-radius:12px" in html
    assert "min-height:185px" in html
    assert "text-align:center" in html


def test_custom_value_font_size_and_border_color_still_apply(captured_html):
    html = _render(captured_html, value_font_size="1.4rem", border_color="#34d399")
    assert "font-size:1.4rem" in html
    assert "border:1px solid #34d399" in html


def test_custom_value_color_still_applies(captured_html):
    html = _render(captured_html, value_color="#dc2626")
    assert "color:#dc2626" in html


def test_value_wrap_still_controls_white_space_and_word_break(captured_html):
    wrapped = _render(captured_html, value_wrap=True)
    captured_html.clear()
    not_wrapped = _render(captured_html, value_wrap=False)
    assert "white-space:normal" in wrapped and "word-break:break-word" in wrapped
    assert "white-space:nowrap" in not_wrapped and "word-break:normal" in not_wrapped


def test_fill_pct_still_renders_the_fill_overlay(captured_html):
    html = _render(captured_html, fill_pct=0.5)
    assert "height:50.0%" in html


def test_fill_pct_none_renders_no_fill_overlay(captured_html):
    html = _render(captured_html, fill_pct=None)
    assert "height:50.0%" not in html
    assert "transition:height 0.6s ease" not in html


# --- call-site API is preserved -------------------------------------------


def test_positional_call_signature_still_works(captured_html):
    # The vast majority of existing call sites call render_kpi_card with
    # positional args (title, value, subtitle, subtitle_color) -- this
    # must keep working exactly as before; only the 5 call sites that
    # pass pre-built HTML needed a new keyword argument.
    ui.render_kpi_card("Total Transit Items", "1,204", "5% of all checkins", "#6b7280")
    assert len(captured_html) == 1
    assert "Total Transit Items" in captured_html[0]
    assert "1,204" in captured_html[0]
    assert "5% of all checkins" in captured_html[0]
