#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         alerts.py
#
#  Description: Contains alert-building logic for the SortView
#               dashboard. This file evaluates pipeline status,
#               data quality issues, live reject activity, and
#               routing trends to determine which system alerts
#               should be displayed to the user.
#
#***************************************************************

#***************************************************************
#
#  Function:     get_system_alerts
#
#  Description: Builds a list of dashboard alert messages based on
#               pipeline status, live reject activity, and routing
#               trends compared to historical averages.
#
#  Parameters:  pipeline_status - Dictionary containing the latest
#                                 pipeline health and data quality details.
#               show_live_alert - Boolean flag indicating whether today's
#                                 reject rate is significantly above normal.
#               westside_pct - Current percentage of items routed to Westside.
#               library_express_pct - Current percentage of items routed to
#                                     Library Express.
#               historical_westside_pct - Optional historical Westside routing
#                                         percentage used for comparison.
#               historical_library_express_pct - Optional historical Library
#                                                Express routing percentage
#                                                used for comparison.
#
#  Returns:     list - A list of alert dictionaries. Each dictionary contains
#                      an alert level and message text.
#
#***************************************************************

def get_system_alerts(
    pipeline_status,
    show_live_alert,
    westside_pct,
    library_express_pct,
    historical_westside_pct=None,
    historical_library_express_pct=None,
):
    # Create an empty list that will hold all active system alerts.
    alerts = []

    # Check pipeline status for operational or data quality problems.
    if pipeline_status:
        no_dest = pipeline_status.get("destination_breakdown", {}).get("No Agency Destination", 0)
        bad_checkins = pipeline_status.get("checkins_bad_datetime_rows", 0)
        bad_rejects = pipeline_status.get("rejects_bad_datetime_rows", 0)

        # Add a critical alert if any items are missing destination routing.
        if no_dest > 0:
            alerts.append({
                "level": "critical",
                "text": f"{no_dest} items missing destination routing (No Agency Destination)."
            })

        # Add a critical alert if checkin or reject rows contain invalid datetime values.
        if bad_checkins > 0 or bad_rejects > 0:
            alerts.append({
                "level": "critical",
                "text": "Some rows have invalid datetime values. Data quality issue."
            })

    # Add a critical alert when the current day's reject rate is unusually high.
    if show_live_alert:
        alerts.append({
            "level": "critical",
            "text": "Today's reject rate is significantly above normal."
        })

    # Compare current Westside routing against the historical average when available.
    if historical_westside_pct is not None and westside_pct >= historical_westside_pct + 3:
        alerts.append({
            "level": "info",
            "text": f"Westside routing is trending above typical levels ({westside_pct:.2f}% vs typical {historical_westside_pct:.2f}%)."
        })

    # Compare current Library Express routing against the historical average when available.
    if historical_library_express_pct is not None and library_express_pct <= max(historical_library_express_pct - 1, 0):
        alerts.append({
            "level": "info",
            "text": f"Library Express routing is trending below typical levels ({library_express_pct:.2f}% vs typical {historical_library_express_pct:.2f}%)."
        })

    # If no problems or notable trends were found, return a normal informational alert.
    if not alerts:
        alerts.append({
            "level": "info",
            "text": "No active system alerts."
        })

    return alerts
