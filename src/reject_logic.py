#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         reject_logic.py
#
#  Description: Provides reject-message classification logic for
#               the SortView dashboard. This file converts raw AMH,
#               RFID, ILS, ACS, and routing error messages into
#               simplified categories that are easier to display and
#               summarize in reports.
#
#***************************************************************

import pandas as pd

#***************************************************************
#
#  Function:     simplify_error
#
#  Description: Converts a raw reject error message into a simplified
#               dashboard category. This helps group similar reject
#               messages together for cleaner reporting and trend
#               analysis.
#
#  Parameters:  msg - Raw error message value from reject data.
#
#  Returns:     str - Simplified reject category.
#
#***************************************************************

def simplify_error(msg):
    # Treat missing or null messages as unknown errors.
    if pd.isna(msg):
        return "Unknown"

    # Normalize the message for case-insensitive keyword matching.
    msg = str(msg).lower()

    # Item lookup failures usually mean the item was not found in the ILS.
    if "item not found" in msg or "no item found" in msg:
        return "Item Not Found"

    # ACS-related messages usually indicate communication or ILS integration problems.
    if "acs" in msg:
        return "ILS / ACS Failure"

    # Multiple RFID tags in range can cause the sorter to reject the item.
    if "multiple rfid" in msg or "multiple tags" in msg:
        return "RFID Collision"

    # Collection code errors usually point to call number, item type, or configuration issues.
    if "collection code" in msg:
        return "Call Number / Config Error"

    # Library lookup failures usually indicate routing or destination configuration issues.
    if "library not found" in msg:
        return "Routing Error"

    # Anything that does not match a known pattern is grouped as Other.
    return "Other"
