#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         formatting.py
#
#  Description: Pure display-formatting helpers shared by the dashboard
#               services and UI. Kept free of Streamlit and Altair so
#               services can format values without importing the UI
#               layer. ui_components re-exports these under the same
#               names for existing callers.
#
#***************************************************************

#***************************************************************
#
#  Function:     format_hour
#
#  Description: Formats a numeric hour value into dashboard HTML
#               using 12-hour time with a smaller AM/PM label.
#
#  Parameters:  hour - Numeric hour value using 24-hour time.
#
#  Returns:     str - HTML-formatted time label.
#
#***************************************************************

def format_hour(hour):
    if hour is None:
        return "N/A"

    if hour == 0:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    if hour < 12:
        return f"{hour}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>AM</span>"
    if hour == 12:
        return "12:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"
    return f"{hour-12}:00<span style='font-size:0.7rem; color:#6b7280; margin-left:4px;'>PM</span>"


#***************************************************************
#
#  Function:     format_relative_time
#
#  Description: Converts a datetime value into a readable relative
#               time string such as "just now", "5 min ago", or
#               "2 days ago".
#
#  Parameters:  dt_value - Earlier datetime value.
#               now_value - Current datetime value used for comparison.
#
#  Returns:     str - Relative time label.
#
#***************************************************************

def format_relative_time(dt_value, now_value):
    if dt_value is None:
        return "N/A"

    minutes = int((now_value - dt_value).total_seconds() // 60)

    if minutes < 1:
        return "just now"
    if minutes == 1:
        return "1 min ago"
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours == 1:
        return "1 hr ago"
    if hours < 24:
        return f"{hours} hrs ago"

    days = hours // 24
    if days == 1:
        return "1 day ago"
    return f"{days} days ago"
