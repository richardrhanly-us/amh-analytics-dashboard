from src.alerts import get_system_alerts


def alert_texts(alerts):
    return [a["text"] for a in alerts]


def alert_levels(alerts):
    return [a["level"] for a in alerts]


def test_info_alert_when_everything_is_normal():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=8.0,
        library_express_pct=3.0,
        historical_westside_pct=7.0,
        historical_library_express_pct=3.0,
    )

    assert len(alerts) == 1
    assert alerts[0]["level"] == "info"
    assert alerts[0]["text"] == "No active system alerts."


def test_no_agency_destination_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 5
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=8.0,
        library_express_pct=3.0,
        historical_westside_pct=7.0,
        historical_library_express_pct=3.0,
    )

    assert "5 items missing destination routing (No Agency Destination)." in alert_texts(alerts)
    assert "critical" in alert_levels(alerts)


def test_bad_datetime_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 2,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=8.0,
        library_express_pct=3.0,
        historical_westside_pct=7.0,
        historical_library_express_pct=3.0,
    )

    assert "Some rows have invalid datetime values. Data quality issue." in alert_texts(alerts)
    assert "critical" in alert_levels(alerts)


def test_live_reject_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=True,
        westside_pct=8.0,
        library_express_pct=3.0,
        historical_westside_pct=7.0,
        historical_library_express_pct=3.0,
    )

    assert "Today's reject rate is significantly above normal." in alert_texts(alerts)
    assert "critical" in alert_levels(alerts)


def test_dynamic_westside_high_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=12.0,
        library_express_pct=3.0,
        historical_westside_pct=8.0,
        historical_library_express_pct=3.0,
    )

    assert "Westside routing is elevated today (12.00% vs typical 8.00%)." in alert_texts(alerts)
    assert "warning" in alert_levels(alerts)


def test_dynamic_library_express_low_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=8.0,
        library_express_pct=0.5,
        historical_westside_pct=8.0,
        historical_library_express_pct=2.0,
    )

    assert "Library Express routing is below typical levels (0.50% vs typical 2.00%)." in alert_texts(alerts)
    assert "warning" in alert_levels(alerts)


def test_transit_imbalance_alert():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=9.0,
        library_express_pct=1.0,
        historical_westside_pct=8.0,
        historical_library_express_pct=1.5,
    )

    assert "Transit imbalance: Westside dominating over Library Express." in alert_texts(alerts)
    assert "warning" in alert_levels(alerts)


def test_transit_imbalance_guard_when_library_express_zero():
    pipeline_status = {
        "destination_breakdown": {
            "No Agency Destination": 0
        },
        "checkins_bad_datetime_rows": 0,
        "rejects_bad_datetime_rows": 0,
    }

    alerts = get_system_alerts(
        pipeline_status=pipeline_status,
        show_live_alert=False,
        westside_pct=9.0,
        library_express_pct=0.0,
        historical_westside_pct=8.0,
        historical_library_express_pct=1.5,
    )

    assert "Transit imbalance: Westside dominating over Library Express." not in alert_texts(alerts)
