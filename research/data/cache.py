"""Content-addressed parquet cache + reproducibility manifest (Phase 0.4).

Layout (per the design blueprint, partitioned ``symbol/tf/year``):

    <cache_dir>/<symbol>/1m/<start.year>/<key>.parquet
    <cache_dir>/<symbol>/1m/<start.year>/<key>.manifest.json

The ``<key>`` is a deterministic 16-char SHA-256 prefix over the canonical
cache parameters (symbol, window, feed, adjustment, provider, quarantine
snapshot, timeframe). **Same params ⇒ same key ⇒ cache hit ⇒ content
byte-identical** (validated via a stable :func:`_content_hash` of the bar
DataFrame; literal parquet file bytes can vary across pyarrow versions —
the contract is on *content*, not file bytes).

The sidecar JSON manifest records every input that could change a study's
output, so any backtest result can be re-traced to the exact cache entry:

* cache key, symbol, tf, window
* feed, adjustment, provider
* quarantine (sorted ISO dates — propagates KNOWN_BAD_DATES into audit)
* code_sha (caller-supplied; we don't shell out to git)
* fetched_at (UTC ISO)
* rows, first_ts, last_ts
* content_hash (stable hash of the bar values; tamper-detection)

Atomic writes: every parquet/manifest write goes to ``<path>.tmp`` then
``os.replace`` to the final name (atomic on the same filesystem). A
half-written cache file can never be observed by a concurrent reader.

Lazy ``pandas`` / ``pyarrow`` imports preserve separability. No production
engine imports. The cache module itself is pure: it never reads git, the
filesystem outside ``cache_dir``, or anything else implicit.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

KEY_LEN = 16
DEFAULT_PROVIDER = "alpaca"
_TF_1M_DIR = "1m"


# --- key / paths ----------------------------------------------------------

def _iso(dt: datetime | date) -> str:
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def _quarantine_iso(q: Mapping[date, str] | None) -> list[str]:
    return sorted(_iso(d) for d in (q or {}).keys())


def cache_key(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    adjustment: str,
    provider: str = DEFAULT_PROVIDER,
    quarantine: Mapping[date, str] | None = None,
) -> str:
    """Deterministic content-addressed key over canonical cache params.

    Any change to the inputs (window, feed, adjustment, provider,
    quarantine) yields a different key; same inputs always yield the same
    key. JSON-serialised with sorted keys + compact separators for
    cross-version stability.
    """
    canonical = {
        "symbol":     symbol,
        "start":      _iso(start),
        "end":        _iso(end),
        "feed":       feed,
        "adjustment": adjustment,
        "provider":   provider,
        "quarantine": _quarantine_iso(quarantine),
        "tf":         _TF_1M_DIR,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:KEY_LEN]


def cache_paths(cache_dir: str | Path, symbol: str, year: int,
                key: str) -> tuple[Path, Path]:
    """``(parquet_path, manifest_path)`` for ``key`` under ``cache_dir``."""
    base = Path(cache_dir) / symbol / _TF_1M_DIR / str(year)
    return base / f"{key}.parquet", base / f"{key}.manifest.json"


# --- atomic IO -------------------------------------------------------------

def _atomic_write_parquet(df, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, compression="snappy", index=True)
    os.replace(tmp, path)  # atomic on same fs


def _atomic_write_json(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


# --- content hash ----------------------------------------------------------

def _content_hash(df) -> str:
    """SHA-256 of the bar content. Stable across cache round-trips and
    pyarrow versions: hashed over the values + index, not file bytes."""
    import pandas as pd
    if df.empty:
        return hashlib.sha256(b"empty").hexdigest()
    h = pd.util.hash_pandas_object(df, index=True).values.tobytes()
    return hashlib.sha256(h).hexdigest()


# --- manifest --------------------------------------------------------------

def _build_manifest(
    df,
    *,
    key: str,
    symbol: str,
    start: datetime,
    end: datetime,
    feed: str,
    adjustment: str,
    provider: str,
    quarantine: Mapping[date, str] | None,
    code_sha: str | None,
) -> dict:
    first_ts = df.index[0].isoformat() if not df.empty else None
    last_ts  = df.index[-1].isoformat() if not df.empty else None
    return {
        "key":          key,
        "tf":           _TF_1M_DIR,
        "symbol":       symbol,
        "start":        _iso(start),
        "end":          _iso(end),
        "feed":         feed,
        "adjustment":   adjustment,
        "provider":     provider,
        "quarantine":   _quarantine_iso(quarantine),
        "code_sha":     code_sha or "unknown",
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
        "rows":         int(len(df)),
        "first_ts":     first_ts,
        "last_ts":      last_ts,
        "content_hash": _content_hash(df),
    }


# --- public API ------------------------------------------------------------

def cache_1m_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: str | Path,
    fetch_1m: Callable[..., Any] | None = None,
    feed: str = "iex",
    adjustment: str = "all",
    provider: str = DEFAULT_PROVIDER,
    quarantine: Mapping[date, str] | None = None,
    code_sha: str | None = None,
    force: bool = False,
):
    """Cache-aware fetch of 1-minute bars.

    Returns the bars as a pandas DataFrame (UTC tz-aware ``ts`` index,
    OHLCV columns). On cache hit (key match + parquet + manifest present)
    the fetcher is not called. ``force=True`` bypasses cache and re-fetches.

    Default ``fetch_1m`` is :func:`research.data.alpaca_source.fetch_1m_bars`;
    default ``quarantine`` is its ``KNOWN_BAD_DATES``. Both are injectable
    so tests / Phase-1 sim can supply deterministic alternatives.
    """
    import pandas as pd

    if quarantine is None:
        from research.data.alpaca_source import KNOWN_BAD_DATES
        quarantine = KNOWN_BAD_DATES

    key = cache_key(symbol, start, end, feed=feed, adjustment=adjustment,
                    provider=provider, quarantine=quarantine)
    parquet_path, manifest_path = cache_paths(cache_dir, symbol, start.year, key)

    if (not force) and parquet_path.exists() and manifest_path.exists():
        return pd.read_parquet(parquet_path)

    if fetch_1m is None:
        from research.data.alpaca_source import fetch_1m_bars
        fetch_1m = fetch_1m_bars

    df = fetch_1m(symbol, start, end, feed=feed, adjustment=adjustment)
    manifest = _build_manifest(
        df, key=key, symbol=symbol, start=start, end=end,
        feed=feed, adjustment=adjustment, provider=provider,
        quarantine=quarantine, code_sha=code_sha,
    )
    _atomic_write_parquet(df, parquet_path)
    _atomic_write_json(manifest, manifest_path)
    return df
