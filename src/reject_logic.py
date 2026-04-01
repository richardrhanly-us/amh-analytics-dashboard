import pandas as pd


def simplify_error(msg):
    if pd.isna(msg):
        return "Unknown"

    msg = str(msg).lower()

    if "no item found" in msg:
        return "Item Not Found"

    if "acs" in msg:
        return "ILS / ACS Failure"

    if "multiple rfid" in msg:
        return "RFID Collision"

    if "collection code" in msg:
        return "Call Number / Config Error"

    if "library not found" in msg:
        return "Routing Error"

    return "Other"
