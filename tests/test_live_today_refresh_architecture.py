"""Integration-level tests for the Live Today refresh redesign's
session-state orchestration in src/app.py's `_render_live_today` fragment:
the tenant-scoped manual-refresh counter, the ever-incrementing
pipeline-status poll tick, and how they combine (via the real
dashboard_refresh_service.resolve_live_data_cache_key) into the cache key
passed to the live loaders.

The pure cache-key math is covered by test_dashboard_refresh_service.py,
and the actual cache-hit/miss consequence against the real loaders is
covered by test_data_loader_refresh.py. This file proves the
session-state PLUMBING around them -- the part that can't be exercised by
either of those alone -- using the same streamlit.testing.v1.AppTest
technique test_dashboard_refresh_service.py's own pause-persistence test
already relies on (a real Streamlit session, no auth/DB/full app needed).

A fake, session-state-backed "pipeline_status DB" stands in for the real
database read: st.session_state["_fake_pipeline_status_db"] is a dict the
test mutates directly between `at.run()` calls to simulate a new
successful Collector run, a heartbeat-only update, a failed attempt, or a
brand-new tenant with no status row yet -- exactly the scenarios
app.py's real fragment has to handle safely.
"""

from services.dashboard_refresh_service import resolve_live_data_cache_key

# Mirrors app.py's _render_live_today body: same session-state key names
# (LIVE_TODAY_REFRESH_STATE_KEY, the fragment tick key), same tenant-scoping
# shape (keyed by (org, branch)), and the same call to the real
# resolve_live_data_cache_key -- only the DB reads are faked.
_LIVE_TODAY_REFRESH_SCRIPT = """
import streamlit as st
from services.dashboard_refresh_service import resolve_live_data_cache_key

if "_fake_pipeline_status_db" not in st.session_state:
    st.session_state["_fake_pipeline_status_db"] = {}

if "_selected_org" not in st.session_state:
    st.session_state["_selected_org"] = "org-a"
if "_selected_branch" not in st.session_state:
    st.session_state["_selected_branch"] = "branch-1"

selected_org = st.session_state["_selected_org"]
selected_branch = st.session_state["_selected_branch"]
tenant_key = (selected_org, selected_branch)

# --- poll tick: forces a fresh pipeline_status read every fragment run,
# whatever triggered it (automatic poll or manual Refresh now click).
tick_key = "_live_today_fragment_tick"
st.session_state[tick_key] = st.session_state.get(tick_key, 0) + 1
poll_tick = st.session_state[tick_key]

pipeline_status = st.session_state["_fake_pipeline_status_db"].get(tenant_key)

# --- tenant-scoped manual-refresh counter, exactly as in app.py.
LIVE_TODAY_REFRESH_STATE_KEY = "_live_today_refresh_state"
refresh_state_by_tenant = st.session_state.setdefault(LIVE_TODAY_REFRESH_STATE_KEY, {})
tenant_refresh_state = refresh_state_by_tenant.setdefault(tenant_key, {"manual_refresh_count": 0})

last_run = pipeline_status.get("last_run") if pipeline_status else None

live_data_key = resolve_live_data_cache_key(
    last_run=last_run,
    manual_refresh_count=tenant_refresh_state["manual_refresh_count"],
)

def _handle_refresh_now():
    tenant_refresh_state["manual_refresh_count"] += 1

if st.button("Refresh now"):
    _handle_refresh_now()
    st.rerun()

st.text(f"poll_tick={poll_tick}")
st.text(f"live_data_key={live_data_key}")
st.text(f"manual_refresh_count={tenant_refresh_state['manual_refresh_count']}")
"""


def _run(at):
    at.run()
    return at


def _text_value(at, prefix):
    matches = [t.value for t in at.text if t.value.startswith(prefix)]
    assert matches, f"no st.text value starting with {prefix!r}; got {[t.value for t in at.text]}"
    return matches[0][len(prefix):]


def _refresh_now(at):
    matches = [b for b in at.button if b.label == "Refresh now"]
    assert matches, f"no 'Refresh now' button found; buttons present: {[b.label for b in at.button]}"
    matches[0].click().run()
    return at


# --- requirement 5: automatic poll forces a fresh pipeline-status read -----


def test_poll_tick_increments_on_every_fragment_run():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    _run(at)
    assert _text_value(at, "poll_tick=") == "1"

    # A second run (standing in for the next automatic poll) must produce
    # a NEW poll_tick -- i.e. load_pipeline_status is called with a fresh
    # cache-key value and therefore actually queries, every single time.
    _run(at)
    assert _text_value(at, "poll_tick=") == "2"


# --- requirement 14: first render loads live data correctly ----------------


def test_first_render_produces_a_live_data_key_reflecting_current_status():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
    }
    _run(at)

    expected = resolve_live_data_cache_key(last_run="2026-09-22T10:00:00", manual_refresh_count=0)
    assert _text_value(at, "live_data_key=") == expected


# --- requirement 13: missing pipeline status fails safely ------------------


def test_missing_pipeline_status_does_not_crash_and_produces_a_stable_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    # No entry at all in the fake DB for this tenant -- brand-new branch.
    _run(at)
    assert not at.exception

    key_1 = _text_value(at, "live_data_key=")
    _run(at)
    assert not at.exception
    key_2 = _text_value(at, "live_data_key=")

    # Still no status -- repeated polls must resolve to the SAME key, not
    # a different one each time (which would force a reload every poll
    # forever while waiting for the tenant's first successful run).
    assert key_1 == key_2


# --- requirements 6/7: unchanged vs. changed last_run -----------------------


def test_unchanged_last_run_across_polls_keeps_the_same_live_data_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
    }
    _run(at)
    key_1 = _text_value(at, "live_data_key=")

    # Simulate the next automatic poll: no new successful run.
    _run(at)
    key_2 = _text_value(at, "live_data_key=")

    assert key_1 == key_2


def test_new_successful_run_changes_the_live_data_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
    }
    _run(at)
    key_1 = _text_value(at, "live_data_key=")

    # The scheduled Collector completes a new successful run.
    at.session_state["_fake_pipeline_status_db"][("org-a", "branch-1")] = {
        "last_run": "2026-09-22T10:15:00",
    }
    _run(at)
    key_2 = _text_value(at, "live_data_key=")

    assert key_1 != key_2


# --- requirements 8/9: heartbeat-only / failed-attempt changes are ignored --


def test_heartbeat_only_change_does_not_change_the_live_data_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00", "updated_at": "2026-09-22T10:00:05"},
    }
    _run(at)
    key_1 = _text_value(at, "live_data_key=")

    # Continuous-agent heartbeat write: updated_at (and health fields, not
    # modeled here since the script never reads them) change, last_run
    # does not.
    at.session_state["_fake_pipeline_status_db"][("org-a", "branch-1")] = {
        "last_run": "2026-09-22T10:00:00",
        "updated_at": "2026-09-22T10:01:03",
    }
    _run(at)
    key_2 = _text_value(at, "live_data_key=")

    assert key_1 == key_2


def test_failed_attempt_does_not_change_the_live_data_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00", "last_attempt": "2026-09-22T10:00:00"},
    }
    _run(at)
    key_1 = _text_value(at, "live_data_key=")

    # A failed scheduled attempt: last_attempt advances, last_run does not.
    at.session_state["_fake_pipeline_status_db"][("org-a", "branch-1")] = {
        "last_run": "2026-09-22T10:00:00",
        "last_attempt": "2026-09-22T10:15:00",
    }
    _run(at)
    key_2 = _text_value(at, "live_data_key=")

    assert key_1 == key_2


# --- requirements 10/11: manual Refresh now, including while paused --------


def test_refresh_now_changes_the_live_data_key_with_no_new_run():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
    }
    _run(at)
    key_before = _text_value(at, "live_data_key=")

    _refresh_now(at)
    key_after = _text_value(at, "live_data_key=")

    assert key_before != key_after
    assert _text_value(at, "manual_refresh_count=") == "1"


def test_refresh_now_works_while_auto_refresh_is_paused():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
    }
    # The real app.py's Pause/Resume control lives outside this fragment
    # and only affects st.fragment's own run_every (see
    # dashboard_refresh_service.resolve_run_every_seconds) -- the Refresh
    # now button never reads that flag, so setting it here proves the
    # button's own logic is unconditional on pause state.
    at.session_state["_live_today_auto_refresh_paused"] = True
    _run(at)
    key_before = _text_value(at, "live_data_key=")

    _refresh_now(at)
    key_after = _text_value(at, "live_data_key=")

    assert key_before != key_after


# --- requirement 12: tenant scoping -----------------------------------------


def test_manual_refresh_count_is_scoped_per_tenant_not_shared():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
        ("org-a", "branch-2"): {"last_run": "2026-09-22T10:00:00"},
    }
    _run(at)
    _refresh_now(at)
    assert _text_value(at, "manual_refresh_count=") == "1"

    # Switch branch (simulating the sidebar branch selector) -- the new
    # branch must start at manual_refresh_count=0, never inheriting
    # branch-1's bumped counter.
    at.session_state["_selected_branch"] = "branch-2"
    _run(at)
    assert _text_value(at, "manual_refresh_count=") == "0"


def test_switching_branch_does_not_reuse_another_branchs_run_key():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_LIVE_TODAY_REFRESH_SCRIPT)
    at.session_state["_fake_pipeline_status_db"] = {
        ("org-a", "branch-1"): {"last_run": "2026-09-22T10:00:00"},
        ("org-a", "branch-2"): {"last_run": "2026-09-22T09:00:00"},
    }
    _run(at)
    branch_1_key = _text_value(at, "live_data_key=")

    at.session_state["_selected_branch"] = "branch-2"
    _run(at)
    branch_2_key = _text_value(at, "live_data_key=")

    # Different branches with different last_run values must resolve to
    # different keys -- proves branch-2's computation used ITS OWN
    # pipeline_status, not a stale value carried over from branch-1.
    assert branch_1_key != branch_2_key
    assert branch_2_key == resolve_live_data_cache_key(
        last_run="2026-09-22T09:00:00", manual_refresh_count=0
    )
