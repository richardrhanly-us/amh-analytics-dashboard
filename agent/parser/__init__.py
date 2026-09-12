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
"""
