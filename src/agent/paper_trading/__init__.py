# -*- coding: utf-8 -*-
"""Persistent paper-trading shadow books for the selection pipeline.

Two isolated books share the same fills, fees and quotes:

* ``A`` — strict chain: only the AI's ``open`` action is executed.
* ``B`` — price-first: engine entry zones are executed regardless of AI wording.

Both books consume persisted artifacts (selection traces and single-stock
analysis history) instead of running an LLM, and both keep their own SQLite
file and NAV series.  Nothing here reads or writes ``portfolio_*``.
"""

from .runner import build_paper_report, ingest_only, run_paper_books, run_paper_session

__all__ = ["build_paper_report", "ingest_only", "run_paper_books", "run_paper_session"]
