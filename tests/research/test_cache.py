"""Offline tests for research.data.cache (Phase 0.4).

Pure-Python cache-key tests run unconditionally; parquet round-trips need
``pyarrow`` (research-only dep) and are gated by ``@requires_pyarrow``.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timezone

import pytest

from research.data import cache

_HAS_PYARROW = importlib.util.find_spec("pyarrow") is not None
requires_pyarrow = pytest.mark.skipif(
    not _HAS_PYARROW,
    reason="pyarrow not installed (research-only dep)",
)


# --- unconditional --------------------------------------------------------

def test_module_has_no_production_imports() -> None:
    import inspect
    s = inspect.getsource(cache)
    for forbidden in ("strategy.broker", "strategy.strategy",
                      "strategy.recovery", "run_strategy", "autoflow"):
        assert forbidden not in s


def _key_args(**over):
    base = dict(
        symbol="SPY",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 2, tzinfo=timezone.utc),
        feed="iex",
        adjustment="all",
        provider="alpaca",
        quarantine={date(2024, 12, 23): "iex truncated"},
    )
    base.update(over)
    return base


def test_cache_key_is_deterministic() -> None:
    a = cache.cache_key(**_key_args())
    b = cache.cache_key(**_key_args())
    assert a == b
    assert len(a) == cache.KEY_LEN


@pytest.mark.parametrize("change", [
    {"symbol":     "QQQ"},
    {"feed":       "sip"},
    {"adjustment": "raw"},
    {"provider":   "polygon"},
    {"start":      datetime(2024, 1, 3, tzinfo=timezone.utc)},
    {"end":        datetime(2024, 1, 4, tzinfo=timezone.utc)},
    {"quarantine": {}},
    {"quarantine": {date(2025, 1, 1): "different date"}},
])
def test_cache_key_changes_when_any_param_changes(change) -> None:
    base_key = cache.cache_key(**_key_args())
    other = cache.cache_key(**_key_args(**change))
    assert other != base_key, f"key did not change for {change}"


def test_cache_paths_partitions_by_symbol_tf_year(tmp_path) -> None:
    pq, mf = cache.cache_paths(tmp_path, "SPY", 2025, "abcdef0123456789")
    assert pq.parent == tmp_path / "SPY" / "1m" / "2025"
    assert pq.name == "abcdef0123456789.parquet"
    assert mf.name == "abcdef0123456789.manifest.json"


# --- parquet-bound (need pyarrow → run in .venv-research) -----------------

pd = pytest.importorskip("pandas")


def _bars(n, *, start_ts):
    idx = pd.DatetimeIndex(
        [start_ts + pd.Timedelta(minutes=i) for i in range(n)],
        tz="UTC", name="ts",
    )
    return pd.DataFrame({
        "open":   [100.0 + i * 0.01 for i in range(n)],
        "high":   [100.5 + i * 0.01 for i in range(n)],
        "low":    [ 99.5 + i * 0.01 for i in range(n)],
        "close":  [100.2 + i * 0.01 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    }, index=idx)


def _fetcher(df, *, counter=None):
    def fetch(symbol, start, end, **kw):
        if counter is not None:
            counter.append((symbol, start, end))
        return df.copy()
    return fetch


@requires_pyarrow
def test_first_call_fetches_second_call_hits_cache(tmp_path) -> None:
    df = _bars(10, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    calls: list = []
    args = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df, counter=calls),
        code_sha="abc",
    )
    out1 = cache.cache_1m_bars(**args)
    assert len(calls) == 1
    out2 = cache.cache_1m_bars(**args)
    assert len(calls) == 1, "cache hit should NOT call fetcher"
    pd.testing.assert_frame_equal(out1, out2)


@requires_pyarrow
def test_cache_hit_never_calls_fetcher(tmp_path) -> None:
    df = _bars(3, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    args = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
    )
    cache.cache_1m_bars(**args, fetch_1m=_fetcher(df))

    def boom(*a, **kw):
        raise AssertionError("fetcher must not be called on cache hit")

    out = cache.cache_1m_bars(**args, fetch_1m=boom)
    assert len(out) == 3


@requires_pyarrow
def test_force_bypasses_cache(tmp_path) -> None:
    df = _bars(5, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    calls: list = []
    args = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df, counter=calls),
    )
    cache.cache_1m_bars(**args)
    cache.cache_1m_bars(**args, force=True)
    assert len(calls) == 2


@requires_pyarrow
def test_changing_params_creates_distinct_cache_entries(tmp_path) -> None:
    df = _bars(5, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    common = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df),
    )
    cache.cache_1m_bars(**common, feed="iex")
    cache.cache_1m_bars(**common, feed="sip")
    files = sorted((tmp_path / "SPY" / "1m" / "2025").glob("*.parquet"))
    assert len(files) == 2


@requires_pyarrow
def test_manifest_records_reproducibility_fields(tmp_path) -> None:
    df = _bars(5, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    cache.cache_1m_bars(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df),
        code_sha="deadbeef",
        quarantine={date(2024, 12, 23): "iex truncated"},
    )
    manifests = list((tmp_path / "SPY" / "1m" / "2025").glob("*.manifest.json"))
    assert len(manifests) == 1
    m = json.loads(manifests[0].read_text())
    for f in ("key", "tf", "symbol", "start", "end", "feed", "adjustment",
              "provider", "quarantine", "code_sha", "fetched_at",
              "rows", "first_ts", "last_ts", "content_hash"):
        assert f in m, f"manifest missing field: {f}"
    assert m["symbol"] == "SPY"
    assert m["feed"] == "iex"
    assert m["adjustment"] == "all"
    assert m["provider"] == "alpaca"
    assert m["code_sha"] == "deadbeef"
    assert m["rows"] == 5
    assert "2024-12-23" in m["quarantine"]


@requires_pyarrow
def test_atomic_write_leaves_no_tmp_file_behind(tmp_path) -> None:
    df = _bars(5, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    cache.cache_1m_bars(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df),
    )
    leftover = list((tmp_path / "SPY" / "1m" / "2025").glob("*.tmp"))
    assert leftover == []


@requires_pyarrow
def test_content_hash_stable_across_cache_round_trip(tmp_path) -> None:
    """Byte-identical *content*: cache hit returns a frame whose
    content_hash matches the originally written one."""
    df = _bars(7, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    args = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df),
    )
    out1 = cache.cache_1m_bars(**args)
    out2 = cache.cache_1m_bars(**args)
    h1, h2 = cache._content_hash(out1), cache._content_hash(out2)
    assert h1 == h2
    # And the manifest's content_hash matches what we read back.
    manifests = list((tmp_path / "SPY" / "1m" / "2025").glob("*.manifest.json"))
    m = json.loads(manifests[0].read_text())
    assert m["content_hash"] == h1


@requires_pyarrow
def test_round_trip_preserves_index_and_dtypes(tmp_path) -> None:
    df = _bars(4, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    args = dict(
        symbol="SPY",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(df),
    )
    cache.cache_1m_bars(**args)        # write
    out = cache.cache_1m_bars(**args)  # read
    assert out.index.name == "ts"
    assert str(out.index.tz) == "UTC"
    assert out.index.is_monotonic_increasing and out.index.is_unique
    for c in ("open", "high", "low", "close", "volume"):
        assert c in out.columns


@requires_pyarrow
def test_write_strips_non_jsonable_attrs(tmp_path) -> None:
    """``alpaca_source.fetch_1m_bars`` stamps ``df.attrs["quarantined"]``
    with a tuple of ``date`` objects. Pandas serializes ``df.attrs`` via
    ``json.dumps`` into parquet metadata, which crashes on ``date``.
    The cache writer must scrub attrs before write — the durable
    metadata record is the sibling manifest JSON.

    Regression: surfaced on the first real-data spike (2026-05-20),
    when a 6-month AAPL 1m fetch tripped the parquet writer.
    """
    import pandas as pd
    df = _bars(3, start_ts=pd.Timestamp("2025-01-02 14:30", tz="UTC"))
    df.attrs.update(
        symbol="AAPL", feed="iex", adjustment="all",
        quarantined=(date(2024, 12, 23),),  # the failing non-JSON case
    )

    def fetch_with_attrs(symbol, start, end, **kw):
        return df.copy()  # .copy() preserves attrs

    args = dict(
        symbol="AAPL",
        start=datetime(2025, 1, 2, tzinfo=timezone.utc),
        end=datetime(2025, 1, 3, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=fetch_with_attrs,
        code_sha="abc",
    )
    # Must not raise — would have raised TypeError pre-fix.
    out = cache.cache_1m_bars(**args)
    assert len(out) == 3
    # Cache round-trip succeeded; read-back drops attrs (pandas behaviour).
    out2 = cache.cache_1m_bars(**args)  # cache hit
    pd.testing.assert_frame_equal(out, out2)


@requires_pyarrow
def test_empty_fetch_caches_empty_frame_with_manifest(tmp_path) -> None:
    """Edge case: a fetch returning no bars still writes an empty cache
    entry + manifest (so we don't re-fetch identical empty windows)."""
    from research.data.alpaca_source import _bars_to_df
    empty = _bars_to_df([])
    args = dict(
        symbol="SPY",
        start=datetime(2030, 1, 1, tzinfo=timezone.utc),
        end=datetime(2030, 1, 2, tzinfo=timezone.utc),
        cache_dir=tmp_path,
        fetch_1m=_fetcher(empty),
    )
    out = cache.cache_1m_bars(**args)
    assert out.empty
    manifests = list((tmp_path / "SPY" / "1m" / "2030").glob("*.manifest.json"))
    assert len(manifests) == 1
    m = json.loads(manifests[0].read_text())
    assert m["rows"] == 0
    assert m["first_ts"] is None and m["last_ts"] is None
