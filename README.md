# AMH Analytics Dashboard

AMH Analytics is a data pipeline and Streamlit dashboard for analyzing Automated Materials Handler (AMH) activity at a library system.

It processes raw check-in and reject logs, generates cleaned datasets, and provides operational insights including transit activity, reject trends, and system alerts.

---

## Features

* Parse raw AMH check-in and reject logs
* Clean and normalize data for analysis
* Generate pipeline status summaries
* Streamlit dashboard with:

  * Live daily metrics
  * Transit analytics
  * Reject analysis
  * Dynamic alert system with severity levels
* Logging for all pipeline and parsing steps
* Unit tests for core logic layers

---

## Project Structure

```
amh_analytics/
├─ data/
│  ├─ raw/                # Raw AMH log files
│  └─ processed/          # Cleaned CSV outputs + pipeline status
│
├─ logs/
│  └─ pipeline.log        # Pipeline + parser logs
│
├─ scripts/
│  ├─ parse_checkins.py   # Check-in parser
│  ├─ parse_rejects.py    # Reject parser
│  └─ run_pipeline.py     # End-to-end pipeline runner
│
├─ src/
│  ├─ app.py              # Streamlit dashboard
│  ├─ alerts.py           # Alert logic (dynamic + severity)
│  ├─ data_loader.py      # Data loading utilities
│  ├─ logger_config.py    # Logging setup
│  ├─ metrics.py          # KPI + summary calculations
│  ├─ reject_logic.py     # Reject analytics
│  └─ transit_logic.py    # Transit analytics
│
├─ tests/
│  ├─ test_alerts.py
│  ├─ test_metrics.py
│  ├─ test_transit_logic.py
│  ├─ test_parse_checkins.py
│  └─ test_parse_rejects.py
│
├─ requirements.txt
└─ README.md
```

---

## Setup

Create and activate a virtual environment:

```
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```
pip install -r requirements.txt
```

---

## Running the Pipeline

Run the full pipeline:

```
python -m scripts.run_pipeline
```

This will:

* parse raw checkins and rejects
* save cleaned CSV files to `data/processed/`
* generate `pipeline_status.json`
* write logs to `logs/pipeline.log`

---

## Running Parsers Individually

```
python -m scripts.parse_checkins
python -m scripts.parse_rejects
```

---

## Running the Dashboard

```
streamlit run src/app.py
```

---

## Running Tests

Run all tests:

```
python -m pytest
```

Run a specific test file:

```
python -m pytest tests/test_alerts.py
```

---

## Logging

Logs are written to:

```
logs/pipeline.log
```

Includes:

* pipeline execution steps
* row counts
* data quality checks
* destination and reject breakdowns

---

## Pipeline Output

Generated files:

```
data/processed/checkins_clean.csv
data/processed/rejects_clean.csv
data/processed/pipeline_status.json
```

`pipeline_status.json` includes:

* last run timestamp
* row counts
* transit item counts
* reject stats
* destination breakdown

---

## Alerts System

The dashboard includes dynamic alerts with severity levels:

* CRITICAL

  * Data quality issues
  * Reject spikes
  * Missing routing (No Agency Destination)

* WARNING

  * Transit imbalance
  * Westside elevated vs historical baseline
  * Library Express below baseline

* INFO

  * No active system alerts

Alerts are based on:

* real-time metrics
* historical baselines
* system thresholds

---

## Notes

* Do not commit `.venv/`, `logs/`, or large raw data files
* Close CSV files before rerunning pipeline (Windows file lock issue)
* Tests ensure stability of parsing and analytics logic

---

## Future Improvements

* Persist alert history
* Add database (Neon / Supabase)
* Optimize transit time calculations
* Add automated scheduled runs (cron / VM)
* Expand dashboard visualizations

---
