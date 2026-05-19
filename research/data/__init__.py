"""Research data layer (Phase 0).

Placeholder. Implemented in later sub-tasks, strictly in order:

* 0.1 alpaca_source  — read-only raw 1-minute fetch
* 0.2 calendar       — XNYS sessions / holidays / half-days
* 0.3 resample       — 1m -> 5/15/60, left-labeled ts=open, anti-leak
* 0.4 cache          — content-addressed parquet + reproducibility manifest
* 0.5 fetch (CLI)    — populate cache + integrity report

No logic in sub-task 0.0 (scaffold only).
"""

__all__: list[str] = []
