"""Canonical Tech Logic AMH line-parsers (Continuous Ingestion Phase B).

These modules are the single source of truth for turning raw pipe/tag
-delimited Tech Logic log lines into structured rows. The logic here was
not rewritten -- it was moved, unchanged, from the deployed production
parsers (agent/parse_checkins.py, agent/parse_rejects.py, agent/parse_acs.py,
which trace back to `agent/SortViewAgent - What is currently sitting on the
AMH computer/`, the real 5-month-proven deployment). Confirmed byte-for-byte
identical before the move -- see tests/test_parser_canonical.py.

Deliberately decoupled from file I/O: every function here takes an
in-memory list of lines and returns a DataFrame. Reading bytes off disk is
agent/tailer.py's job, not this package's.

LOGGING: THIS PACKAGE EMITS, IT DOES NOT CONFIGURE. Each module logs its
parse summaries to a plain named logger -- "parser.checkins",
"parser.rejects", "parser.acs" -- and attaches no handler, creates no
directory and opens no file. Importing it has no effect on the filesystem.
Where those records go is the importing APPLICATION's decision:
  - the legacy mirror (agent/parse_*.py, agent/run_pipeline.py) attaches its
    own file and console handlers to these three names;
  - the Collector attaches none, so the records go nowhere (and Contract v2
    additionally disables the three loggers while it parses -- see
    collector/v2_transform.py's _parser_logs_off).
These modules used to call agent/logger_config.py's get_logger() at import,
which created `logs/parser.*.log` under whatever the CURRENT DIRECTORY was --
inside a release bundle while its own preflight ran, and inside the install
root on every run. tests/test_parser_logging_ownership.py holds this.
"""
