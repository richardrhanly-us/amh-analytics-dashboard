import pandas as pd

CHECKINS_HISTORY_FILE = "data/processed/checkins_history.csv"
REJECTS_HISTORY_FILE = "data/processed/rejects_history.csv"

def clean(path, cols):
    df = pd.read_csv(path, low_memory=False)

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].astype(str).str.strip()

    use_cols = [c for c in cols if c in df.columns]

    before = len(df)

    if use_cols:
        df = df.drop_duplicates(subset=use_cols)
    else:
        df = df.drop_duplicates()

    if "datetime" in df.columns:
        df = df.sort_values("datetime")

    df.to_csv(path, index=False)

    print(path)
    print("before:", before)
    print("after:", len(df))
    print("removed:", before - len(df))
    print()

clean(CHECKINS_HISTORY_FILE, ["datetime", "barcode", "title", "destination"])
clean(REJECTS_HISTORY_FILE, ["datetime", "barcode", "error_message"])
