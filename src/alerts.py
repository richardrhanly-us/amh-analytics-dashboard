def get_system_alerts(
    pipeline_status,
    show_live_alert,
    westside_pct,
    library_express_pct,
    historical_westside_pct=None,
    historical_library_express_pct=None,
):
    alerts = []

    if pipeline_status:
        no_dest = pipeline_status.get("destination_breakdown", {}).get("No Agency Destination", 0)
        bad_checkins = pipeline_status.get("checkins_bad_datetime_rows", 0)
        bad_rejects = pipeline_status.get("rejects_bad_datetime_rows", 0)

        if no_dest > 0:
            alerts.append({
                "level": "critical",
                "text": f"{no_dest} items missing destination routing (No Agency Destination)."
            })

        if bad_checkins > 0 or bad_rejects > 0:
            alerts.append({
                "level": "critical",
                "text": "Some rows have invalid datetime values. Data quality issue."
            })

    if show_live_alert:
        alerts.append({
            "level": "critical",
            "text": "Today's reject rate is significantly above normal."
        })

    if historical_westside_pct is not None:
        if westside_pct >= historical_westside_pct + 3:
            alerts.append({
                "level": "info",
                "text": f"Westside routing is trending above typical levels ({westside_pct:.2f}% vs typical {historical_westside_pct:.2f}%)."
            })

    if historical_library_express_pct is not None:
        if library_express_pct <= max(historical_library_express_pct - 1, 0):
            alerts.append({
                "level": "info",
                "text": f"Library Express routing is trending below typical levels ({library_express_pct:.2f}% vs typical {historical_library_express_pct:.2f}%)."
            })

    if not alerts:
        alerts.append({
            "level": "info",
            "text": "No active system alerts."
        })

    return alerts
