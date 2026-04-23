"""Tests for strategy.broker.

The alpaca-py SDK is substituted at the module boundary by a plain mock
that implements the duck-typed methods the wrapper uses. We verify:

* The exact OTO LimitOrderRequest is built with disaster stop, DAY TIF,
  extended_hours=False, integer qty, and the intent's idempotent COID.
* Error classification maps every HTTP status / code path correctly.
* Retry policy respects max_retries and backoff (we use a fake sleep).
* Idempotent COID: a DuplicateClientOrderId on resubmit is resolved via
  get_order_by_client_id and returns the existing order.
* poll_terminal returns on terminal status, raises TimeoutError otherwise.
* flatten_symbol follows the 5-step sequence and never calls close_position.
* cancel_order on an already-terminal order is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy.broker import (
    AlpacaBroker,
    APIError,
    BarTimeframe,
    LimitOrderRequest,
    RetryPolicy,
    SdkOrderClass,
    SdkOrderSide,
    SdkOrderStatus,
    SdkTimeInForce,
    StopLossRequest,
    _classify,
    _map_status,
)
from strategy.dto import (
    COID_PREFIX,
    OrderClass,
    OrderIntent,
    OrderSide,
    OrderStatus,
    TimeInForce,
)
from strategy.errors import (
    AccountRestricted,
    DuplicateClientOrderId,
    MarketClosedRejection,
    OrderRejected,
    PermanentBrokerError,
    TransientBrokerError,
)


UTC = timezone.utc


def _intent(**over) -> OrderIntent:
    defaults = dict(
        intent_id="i-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10,
        limit_price=Decimal("200.10"),
        disaster_stop_price=Decimal("196.00"),
        tif=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        reason="breakout",
        ref_price=Decimal("200.00"),
        atr=Decimal("1.5"),
        spread_bps=Decimal("4"),
        ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
    )
    defaults.update(over)
    return OrderIntent(**defaults)


def _sdk_order(
    *,
    broker_id: str = "b-1",
    coid: str = "TBv1-xxx",
    symbol: str = "AAPL",
    side: SdkOrderSide = SdkOrderSide.BUY,
    qty: int = 10,
    filled_qty: int = 0,
    status: SdkOrderStatus = SdkOrderStatus.NEW,
    order_class: SdkOrderClass = SdkOrderClass.OTO,
    legs: list | None = None,
    filled_avg_price: str | None = None,
    parent_coid: str | None = None,
    order_type: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=broker_id,
        client_order_id=coid,
        symbol=symbol,
        side=side,
        qty=str(qty),
        filled_qty=str(filled_qty),
        status=status,
        order_class=order_class,
        submitted_at=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        filled_at=None,
        legs=legs,
        filled_avg_price=filled_avg_price,
        parent_client_order_id=parent_coid,
        order_type=order_type,
    )


def _fake_sleep() -> list[float]:
    calls: list[float] = []
    def sleep(s: float) -> None:
        calls.append(s)
    sleep.calls = calls  # type: ignore[attr-defined]
    return sleep  # type: ignore[return-value]


def _broker(
    client: MagicMock | None = None,
    *,
    poll_timeout_s: float = 1.0,
    retry_policy: RetryPolicy | None = None,
    data_client: MagicMock | None = None,
) -> tuple[AlpacaBroker, MagicMock, list[float]]:
    client = client or MagicMock()
    sleeps: list[float] = []
    def sleep(s: float) -> None:
        sleeps.append(s)
    b = AlpacaBroker(
        client,
        data_client,
        data_feed="iex",
        retry_policy=retry_policy or RetryPolicy(max_retries=2, base_s=0.01, cap_s=0.01),
        poll_interval_s=0.001,
        poll_timeout_s=poll_timeout_s,
        sleep=sleep,
    )
    return b, client, sleeps


# ---------------------------------------------------------------------------
# Error classification matrix
# ---------------------------------------------------------------------------


def _api_error(status: int, message: str, code: int | None = None) -> APIError:
    """Build an APIError the way alpaca-py actually constructs it.

    APIError derives status_code / code / message from (a) a JSON-encoded
    error payload passed as the positional arg and (b) an http_error
    object that exposes ``response.status_code``. We mimic both.
    """
    import json
    payload = json.dumps({"code": code if code is not None else 0, "message": message})
    http_err = SimpleNamespace(
        response=SimpleNamespace(status_code=status),
        request=None,
    )
    return APIError(payload, http_error=http_err)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_classify_5xx_is_transient(status: int) -> None:
    assert isinstance(_classify(_api_error(status, "boom")), TransientBrokerError)


def test_classify_429_is_transient() -> None:
    assert isinstance(_classify(_api_error(429, "rate limited")), TransientBrokerError)


def test_classify_422_duplicate_coid() -> None:
    exc = _api_error(422, "client_order_id already used")
    assert isinstance(_classify(exc), DuplicateClientOrderId)


def test_classify_422_market_closed() -> None:
    exc = _api_error(422, "the market is closed")
    assert isinstance(_classify(exc), MarketClosedRejection)


def test_classify_422_other_is_order_rejected() -> None:
    exc = _api_error(422, "wash trade blocked", code=4200)
    c = _classify(exc)
    assert isinstance(c, OrderRejected)
    assert c.reason_code == "4200"


def test_classify_403_is_account_restricted() -> None:
    exc = _api_error(403, "account is restricted")
    assert isinstance(_classify(exc), AccountRestricted)


def test_classify_403_forbidden_is_still_account_restricted() -> None:
    exc = _api_error(403, "forbidden")
    assert isinstance(_classify(exc), AccountRestricted)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_classify_4xx_other_is_permanent(status: int) -> None:
    exc = _api_error(status, "bad")
    c = _classify(exc)
    assert isinstance(c, PermanentBrokerError)
    assert not isinstance(c, TransientBrokerError)


def test_classify_connection_error_is_transient() -> None:
    assert isinstance(_classify(ConnectionError("boom")), TransientBrokerError)


def test_classify_timeout_is_transient() -> None:
    assert isinstance(_classify(TimeoutError("slow")), TransientBrokerError)


def test_classify_oserror_is_transient() -> None:
    # TLS / DNS failures surface as OSError.
    assert isinstance(_classify(OSError("dns")), TransientBrokerError)


def test_classify_unknown_exception_fails_closed_permanent() -> None:
    class Weird(Exception):
        pass
    c = _classify(Weird("???"))
    assert isinstance(c, PermanentBrokerError)
    # Importantly NOT transient → does not retry.
    assert not isinstance(c, TransientBrokerError)


# ---------------------------------------------------------------------------
# Retry bounds
# ---------------------------------------------------------------------------


def test_transient_retries_up_to_max_then_raises() -> None:
    b, client, sleeps = _broker(retry_policy=RetryPolicy(max_retries=2, base_s=0.01, cap_s=0.01))
    client.get_account.side_effect = _api_error(503, "nope")
    with pytest.raises(TransientBrokerError):
        b.get_account_snapshot()
    # max_retries=2 means up to 2 retries after the initial attempt → 3 calls.
    assert client.get_account.call_count == 3
    # We slept between retries.
    assert len(sleeps) == 2


def test_permanent_error_does_not_retry() -> None:
    b, client, _ = _broker()
    client.get_account.side_effect = _api_error(400, "bad request")
    with pytest.raises(PermanentBrokerError):
        b.get_account_snapshot()
    assert client.get_account.call_count == 1


def test_transient_then_success() -> None:
    b, client, _ = _broker()
    account = SimpleNamespace(
        equity="25000.00",
        last_equity="24800.00",
        buying_power="50000.00",
        cash="12500.00",
        pattern_day_trader=False,
    )
    client.get_account.side_effect = [_api_error(503, "boom"), account]
    snap = b.get_account_snapshot()
    assert snap.equity == Decimal("25000.00")


# ---------------------------------------------------------------------------
# submit_entry_with_protection — OTO construction
# ---------------------------------------------------------------------------


def test_submit_builds_exact_oto_request() -> None:
    b, client, _ = _broker()
    intent = _intent()
    parent = _sdk_order(coid=intent.client_order_id(), order_class=SdkOrderClass.OTO)
    parent.legs = [
        _sdk_order(
            broker_id="b-child",
            coid="TBv1-child",
            side=SdkOrderSide.SELL,
            status=SdkOrderStatus.NEW,
            order_class=SdkOrderClass.SIMPLE,
            parent_coid=intent.client_order_id(),
            order_type="stop",
        )
    ]
    client.submit_order.return_value = parent

    submitted = b.submit_entry_with_protection(intent)

    # Exactly one submit_order call.
    assert client.submit_order.call_count == 1
    kwargs = client.submit_order.call_args.kwargs
    req = kwargs["order_data"]
    assert isinstance(req, LimitOrderRequest)
    assert req.symbol == "AAPL"
    assert req.qty == 10
    assert req.side is SdkOrderSide.BUY
    assert req.time_in_force is SdkTimeInForce.DAY
    assert req.extended_hours is False
    assert req.limit_price == pytest.approx(200.10)
    assert req.client_order_id == intent.client_order_id()
    assert req.order_class is SdkOrderClass.OTO
    assert isinstance(req.stop_loss, StopLossRequest)
    assert req.stop_loss.stop_price == pytest.approx(196.00)

    # Return shape
    assert submitted.parent.client_order_id == intent.client_order_id()
    assert submitted.stop_child is not None
    assert submitted.stop_child.parent_client_order_id == intent.client_order_id()


def test_submit_duplicate_coid_resolves_to_existing() -> None:
    b, client, _ = _broker()
    intent = _intent()
    coid = intent.client_order_id()

    client.submit_order.side_effect = _api_error(
        422, "client_order_id already used"
    )
    existing = _sdk_order(coid=coid, order_class=SdkOrderClass.OTO)
    existing.legs = [
        _sdk_order(
            broker_id="b-child",
            coid="TBv1-child",
            side=SdkOrderSide.SELL,
            parent_coid=coid,
            order_type="stop",
        )
    ]
    client.get_order_by_client_id.return_value = existing

    submitted = b.submit_entry_with_protection(intent)
    assert submitted.parent.client_order_id == coid
    # Was fetched, not re-submitted.
    client.get_order_by_client_id.assert_called_once_with(coid)


def test_submit_duplicate_coid_but_missing_raises() -> None:
    b, client, _ = _broker()
    intent = _intent()
    client.submit_order.side_effect = _api_error(422, "client_order_id already used")
    client.get_order_by_client_id.side_effect = _api_error(404, "not found")
    with pytest.raises(DuplicateClientOrderId):
        b.submit_entry_with_protection(intent)


# ---------------------------------------------------------------------------
# poll_terminal
# ---------------------------------------------------------------------------


def test_poll_terminal_returns_when_filled() -> None:
    b, client, _ = _broker(poll_timeout_s=1.0)
    new = _sdk_order(status=SdkOrderStatus.NEW)
    filled = _sdk_order(status=SdkOrderStatus.FILLED, filled_qty=10, filled_avg_price="200.05")
    client.get_order_by_client_id.side_effect = [new, new, filled]
    order = b.poll_terminal("TBv1-xxx")
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.avg_fill_price == Decimal("200.05")


def test_poll_terminal_timeout() -> None:
    b, client, _ = _broker(poll_timeout_s=0.01)
    client.get_order_by_client_id.return_value = _sdk_order(status=SdkOrderStatus.NEW)
    with pytest.raises(TimeoutError):
        b.poll_terminal("TBv1-xxx")


def test_poll_terminal_catches_done_for_day_as_terminal() -> None:
    b, client, _ = _broker(poll_timeout_s=1.0)
    dfd = _sdk_order(status=SdkOrderStatus.DONE_FOR_DAY, filled_qty=4)
    client.get_order_by_client_id.return_value = dfd
    order = b.poll_terminal("TBv1-xxx")
    assert order.is_terminal()
    assert order.status is OrderStatus.EXPIRED
    assert order.filled_qty == 4   # partial-fill qty preserved


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


def test_cancel_on_terminal_is_noop() -> None:
    b, client, _ = _broker()
    client.cancel_order_by_id.side_effect = _api_error(422, "order is not cancelable")
    # Should NOT raise.
    b.cancel_order("b-1")


def test_cancel_on_missing_is_noop() -> None:
    b, client, _ = _broker()
    client.cancel_order_by_id.side_effect = _api_error(404, "not found")
    b.cancel_order("b-1")


def test_cancel_permanent_non_recognised_raises() -> None:
    b, client, _ = _broker()
    client.cancel_order_by_id.side_effect = _api_error(400, "malformed id")
    with pytest.raises(PermanentBrokerError):
        b.cancel_order("b-1")


# ---------------------------------------------------------------------------
# flatten_symbol — the 5-step sequence
# ---------------------------------------------------------------------------


def test_flatten_sequence(monkeypatch) -> None:
    """The sequence must be:
    (1) list open orders, (2) cancel each, (3) wait for cancels terminal,
    (4) submit market close, (5) verify position==0.

    We assert the call order by tracking invocation indices.
    """
    b, client, _ = _broker(poll_timeout_s=1.0)
    # Calls in the order the wrapper should make them.
    call_seq: list[str] = []

    stop_child = _sdk_order(
        broker_id="b-stop",
        coid="TBv1-stop",
        side=SdkOrderSide.SELL,
        status=SdkOrderStatus.NEW,
        order_class=SdkOrderClass.SIMPLE,
        parent_coid="TBv1-parent",
        order_type="stop",
    )
    parent = _sdk_order(
        broker_id="b-parent",
        coid="TBv1-parent",
        status=SdkOrderStatus.NEW,
        order_class=SdkOrderClass.OTO,
        legs=[stop_child],
    )

    # get_orders is called several times:
    # 1) enumerate orders for the symbol
    # 2+) poll inside _wait_for_cancel_terminal
    # 3) inside flatten's position lookup is via get_all_positions
    open_orders_responses = [
        [parent],   # initial listing — includes parent & its legs (nested)
        [],         # post-cancel: cleared
    ]
    def get_orders(filter=None):  # noqa: A002
        call_seq.append("get_orders")
        return open_orders_responses.pop(0) if open_orders_responses else []
    client.get_orders.side_effect = get_orders

    def cancel(oid):
        call_seq.append(f"cancel:{oid}")
    client.cancel_order_by_id.side_effect = cancel

    long_pos = SimpleNamespace(
        symbol="AAPL", qty="10", avg_entry_price="200",
        market_value="2000", unrealized_pl="0", side="long",
    )
    zero_after = []
    positions_responses = [[long_pos], zero_after]
    def get_all_positions():
        call_seq.append("get_positions")
        return positions_responses.pop(0)
    client.get_all_positions.side_effect = get_all_positions

    close_filled = _sdk_order(
        broker_id="b-close",
        coid="TBv1-close",
        side=SdkOrderSide.SELL,
        status=SdkOrderStatus.FILLED,
        filled_qty=10,
        filled_avg_price="200.04",
        order_class=SdkOrderClass.SIMPLE,
    )
    # submit_order is the market close. poll_terminal then fetches by COID.
    def submit_order(order_data=None):
        call_seq.append("submit_close")
        return close_filled
    client.submit_order.side_effect = submit_order

    client.get_order_by_client_id.return_value = close_filled

    result = b.flatten_symbol("AAPL", close_client_order_id="TBv1-close")

    assert result.final_position_qty == 0
    assert "b-parent" in result.cancelled_order_ids or "b-stop" in result.cancelled_order_ids

    # close_position endpoint NEVER called.
    assert not hasattr(client, "close_position") or not client.close_position.called

    # Ordering: all cancels happen before submit_close.
    first_submit = call_seq.index("submit_close")
    cancels = [i for i, x in enumerate(call_seq) if x.startswith("cancel:")]
    assert cancels
    assert all(i < first_submit for i in cancels)


def test_flatten_with_no_position_is_safe() -> None:
    b, client, _ = _broker(poll_timeout_s=0.5)
    client.get_orders.return_value = []
    client.get_all_positions.return_value = []
    result = b.flatten_symbol("AAPL", close_client_order_id="TBv1-close")
    assert result.final_position_qty == 0
    # No submit_order call when there's nothing to close.
    client.submit_order.assert_not_called()


def test_flatten_verifies_position_is_zero_or_raises() -> None:
    b, client, _ = _broker(poll_timeout_s=0.5)
    client.get_orders.side_effect = [[], []]
    pos = SimpleNamespace(
        symbol="AAPL", qty="10", avg_entry_price="200",
        market_value="2000", unrealized_pl="0", side="long",
    )
    # After the close, position still shows 10 → must raise.
    client.get_all_positions.side_effect = [[pos], [pos]]
    close_filled = _sdk_order(
        broker_id="b-close",
        coid="TBv1-close",
        side=SdkOrderSide.SELL,
        status=SdkOrderStatus.FILLED,
        filled_qty=10,
        filled_avg_price="200.04",
    )
    client.submit_order.return_value = close_filled
    client.get_order_by_client_id.return_value = close_filled
    with pytest.raises(PermanentBrokerError, match="still"):
        b.flatten_symbol("AAPL", close_client_order_id="TBv1-close")


# ---------------------------------------------------------------------------
# submit_market_close validates qty
# ---------------------------------------------------------------------------


def test_market_close_rejects_nonpositive_qty() -> None:
    b, client, _ = _broker()
    with pytest.raises(PermanentBrokerError):
        b.submit_market_close("AAPL", qty=0, client_order_id="TBv1-c")


# ---------------------------------------------------------------------------
# get_open_orders flattens legs
# ---------------------------------------------------------------------------


def test_get_open_orders_flattens_legs() -> None:
    b, client, _ = _broker()
    stop = _sdk_order(
        broker_id="b-stop",
        coid="TBv1-stop",
        side=SdkOrderSide.SELL,
        order_class=SdkOrderClass.SIMPLE,
        parent_coid="TBv1-parent",
        order_type="stop",
    )
    parent = _sdk_order(
        broker_id="b-parent",
        coid="TBv1-parent",
        order_class=SdkOrderClass.OTO,
        legs=[stop],
    )
    client.get_orders.return_value = [parent]
    out = b.get_open_orders("AAPL")
    assert len(out) == 2
    ids = {o.broker_order_id for o in out}
    assert ids == {"b-parent", "b-stop"}
    # Leg carries parent_client_order_id even if SDK omitted it.
    child = next(o for o in out if o.broker_order_id == "b-stop")
    assert child.parent_client_order_id == "TBv1-parent"


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


def test_get_positions_maps_long_to_buy() -> None:
    b, client, _ = _broker()
    client.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="200",
                        market_value="2000", unrealized_pl="0", side="long")
    ]
    positions = b.get_positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].side is OrderSide.BUY


def test_get_positions_maps_short_to_sell() -> None:
    b, client, _ = _broker()
    client.get_all_positions.return_value = [
        SimpleNamespace(symbol="AAPL", qty="10", avg_entry_price="200",
                        market_value="2000", unrealized_pl="0", side="short")
    ]
    positions = b.get_positions()
    assert positions[0].side is OrderSide.SELL


# ---------------------------------------------------------------------------
# Map status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sdk_status,our_status,terminal",
    [
        (SdkOrderStatus.FILLED, OrderStatus.FILLED, True),
        (SdkOrderStatus.CANCELED, OrderStatus.CANCELED, True),
        (SdkOrderStatus.EXPIRED, OrderStatus.EXPIRED, True),
        (SdkOrderStatus.REJECTED, OrderStatus.REJECTED, True),
        (SdkOrderStatus.DONE_FOR_DAY, OrderStatus.EXPIRED, True),
        (SdkOrderStatus.NEW, OrderStatus.NEW, False),
        (SdkOrderStatus.ACCEPTED, OrderStatus.ACCEPTED, False),
        (SdkOrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED, False),
        (SdkOrderStatus.PENDING_NEW, OrderStatus.PENDING_NEW, False),
        (SdkOrderStatus.HELD, OrderStatus.HELD, False),
        (SdkOrderStatus.REPLACED, OrderStatus.UNKNOWN, False),
        (SdkOrderStatus.PENDING_CANCEL, OrderStatus.UNKNOWN, False),
        (SdkOrderStatus.PENDING_REPLACE, OrderStatus.UNKNOWN, False),
    ],
)
def test_status_mapping(sdk_status, our_status, terminal) -> None:
    from strategy.dto import TERMINAL_STATUSES
    mapped = _map_status(sdk_status)
    assert mapped is our_status
    assert (mapped in TERMINAL_STATUSES) is terminal


# ---------------------------------------------------------------------------
# SDK isolation check
# ---------------------------------------------------------------------------


def test_from_credentials_factory(monkeypatch) -> None:
    """Verify the factory wires the SDK TradingClient without hitting the network."""
    import strategy.broker as br

    captured: dict = {}

    class _FakeSdkClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(br, "TradingClient", _FakeSdkClient)
    b = AlpacaBroker.from_credentials("k", "s", paper=True)
    assert isinstance(b, AlpacaBroker)
    assert captured == {"api_key": "k", "secret_key": "s", "paper": True}


def test_get_order_by_coid_returns_none_on_404() -> None:
    b, client, _ = _broker()
    client.get_order_by_client_id.side_effect = _api_error(404, "not found")
    assert b.get_order_by_coid("TBv1-missing") is None


def test_get_order_by_coid_other_permanent_raises() -> None:
    b, client, _ = _broker()
    client.get_order_by_client_id.side_effect = _api_error(400, "bad id")
    with pytest.raises(PermanentBrokerError):
        b.get_order_by_coid("TBv1-x")


def test_map_status_accepts_string() -> None:
    assert _map_status("filled") is OrderStatus.FILLED
    assert _map_status("unknown_value") is OrderStatus.UNKNOWN


def test_map_status_accepts_unknown_enum_value() -> None:
    # Something that isn't a valid SdkOrderStatus at all.
    assert _map_status(object()) is OrderStatus.UNKNOWN


def test_map_side_unrecognised_raises() -> None:
    from strategy.broker import _map_side
    with pytest.raises(PermanentBrokerError):
        _map_side("diagonal")


def test_map_order_class_paths() -> None:
    from strategy.broker import _map_order_class
    assert _map_order_class(SdkOrderClass.SIMPLE) is OrderClass.SIMPLE
    assert _map_order_class(SdkOrderClass.OTO) is OrderClass.OTO
    assert _map_order_class("simple") is OrderClass.SIMPLE
    assert _map_order_class("bogus") is OrderClass.SIMPLE   # fail-closed default


def test_coerce_utc_paths() -> None:
    from strategy.broker import _coerce_utc
    # naive → UTC
    naive = datetime(2026, 4, 23, 14, 0)
    out = _coerce_utc(naive)
    assert out.tzinfo is timezone.utc
    # string → parsed
    out2 = _coerce_utc("2026-04-23T14:00:00Z")
    assert out2.tzinfo is timezone.utc
    # non-string, non-datetime → fallback to now()
    out3 = _coerce_utc(None)
    assert out3.tzinfo is timezone.utc


def test_pick_stop_child_no_legs_returns_none() -> None:
    from strategy.broker import _pick_stop_child
    raw = _sdk_order(legs=None)
    assert _pick_stop_child(raw) is None


def test_poll_terminal_tolerates_missing_then_appears() -> None:
    b, client, _ = _broker(poll_timeout_s=1.0)
    filled = _sdk_order(status=SdkOrderStatus.FILLED, filled_qty=10)
    # First lookup: 404 → None; second: the filled order.
    client.get_order_by_client_id.side_effect = [
        _api_error(404, "not found"),
        filled,
    ]
    out = b.poll_terminal("TBv1-x")
    assert out.status is OrderStatus.FILLED


def test_flatten_cancel_wait_times_out() -> None:
    """If cancel never reaches terminal, flatten_symbol surfaces TimeoutError."""
    b, client, _ = _broker(poll_timeout_s=0.02)
    parent = _sdk_order(broker_id="b-parent", coid="TBv1-parent")
    # Listings return the same open order forever → cancel never goes terminal.
    client.get_orders.return_value = [parent]
    client.cancel_order_by_id.return_value = None
    with pytest.raises(TimeoutError):
        b.flatten_symbol("AAPL", close_client_order_id="TBv1-close")


# ---------------------------------------------------------------------------
# Market-data methods
# ---------------------------------------------------------------------------


def _bar(ts: datetime, o="100", h="101", l="99", c="100.5", v=1000) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=ts, open=o, high=h, low=l, close=c, volume=v,
        trade_count=10, vwap="100.1",
    )


def test_get_bars_returns_empty_when_symbol_absent() -> None:
    data_client = MagicMock()
    data_client.get_stock_bars.return_value = SimpleNamespace(data={})
    b, _, _ = _broker(data_client=data_client)
    out = b.get_bars(
        "AAPL",
        BarTimeframe.M5,
        start=datetime(2026, 4, 23, 13, 0, tzinfo=UTC),
        end=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        limit=20,
    )
    assert out == []


def test_get_bars_parses_decimal_and_utc() -> None:
    data_client = MagicMock()
    data_client.get_stock_bars.return_value = SimpleNamespace(
        data={
            "AAPL": [
                _bar(datetime(2026, 4, 23, 13, 35, tzinfo=UTC), o="200.00", c="200.50"),
                _bar(datetime(2026, 4, 23, 13, 40, tzinfo=UTC), o="200.55", c="200.80"),
            ]
        }
    )
    b, _, _ = _broker(data_client=data_client)
    bars = b.get_bars(
        "AAPL",
        BarTimeframe.M5,
        start=datetime(2026, 4, 23, 13, 30, tzinfo=UTC),
        end=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
    )
    assert len(bars) == 2
    assert bars[0].open == Decimal("200.00")
    assert bars[0].close == Decimal("200.50")
    assert bars[0].symbol == "AAPL"
    assert bars[0].ts.tzinfo is UTC


def test_get_bars_fails_if_no_data_client() -> None:
    b, _, _ = _broker()
    with pytest.raises(PermanentBrokerError, match="market data client"):
        b.get_bars(
            "AAPL",
            BarTimeframe.M5,
            start=datetime(2026, 4, 23, tzinfo=UTC),
            end=datetime(2026, 4, 23, tzinfo=UTC),
        )


def test_get_latest_quote_parses_decimal() -> None:
    data_client = MagicMock()
    data_client.get_stock_latest_quote.return_value = SimpleNamespace(
        data={
            "AAPL": SimpleNamespace(
                timestamp=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
                bid_price="199.98",
                ask_price="200.02",
                bid_size=100,
                ask_size=200,
            )
        }
    )
    b, _, _ = _broker(data_client=data_client)
    q = b.get_latest_quote("AAPL")
    assert q.bid_price == Decimal("199.98")
    assert q.ask_price == Decimal("200.02")
    assert q.symbol == "AAPL"


def test_get_latest_quote_raises_when_missing() -> None:
    data_client = MagicMock()
    data_client.get_stock_latest_quote.return_value = SimpleNamespace(data={})
    b, _, _ = _broker(data_client=data_client)
    with pytest.raises(PermanentBrokerError, match="missing"):
        b.get_latest_quote("AAPL")


def test_get_latest_trade_parses_decimal() -> None:
    data_client = MagicMock()
    data_client.get_stock_latest_trade.return_value = SimpleNamespace(
        data={
            "AAPL": SimpleNamespace(
                timestamp=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
                price="200.05",
                size=150,
            )
        }
    )
    b, _, _ = _broker(data_client=data_client)
    t = b.get_latest_trade("AAPL")
    assert t.price == Decimal("200.05")
    assert t.size == 150


def test_timeframe_mapping() -> None:
    from strategy.broker import _to_sdk_timeframe
    # Round-trip to the str form the SDK uses in its URL params.
    assert str(_to_sdk_timeframe(BarTimeframe.M5)) == "5Min"
    assert str(_to_sdk_timeframe(BarTimeframe.M15)) == "15Min"
    assert str(_to_sdk_timeframe(BarTimeframe.H1)) == "1Hour"


def test_feed_mapping_rejects_bogus() -> None:
    from strategy.broker import _to_sdk_feed
    with pytest.raises(PermanentBrokerError):
        _to_sdk_feed("nasdaq_only")


# ---------------------------------------------------------------------------
# Diagnostics helper used by scripts/paper_smoke.py
# ---------------------------------------------------------------------------


def test_diagnose_uses_model_dump_when_available() -> None:
    """pydantic-like objects: return their model_dump verbatim."""
    from strategy.broker import _order_attrs_for_diagnostics

    class _FakePydantic:
        def model_dump(self, mode: str = "python") -> dict:
            return {"id": "parent-uuid", "client_order_id": "TBv1-xyz",
                    "legs": [{"id": "child-uuid"}]}

    out = _order_attrs_for_diagnostics(_FakePydantic())
    assert out["id"] == "parent-uuid"
    assert out["client_order_id"] == "TBv1-xyz"
    assert out["legs"][0]["id"] == "child-uuid"


def test_diagnose_falls_back_to_dict_v1() -> None:
    """pydantic v1 exposes ``.dict()`` instead of model_dump — support it."""
    from strategy.broker import _order_attrs_for_diagnostics

    class _FakeV1:
        def dict(self) -> dict:
            return {"id": "p", "client_order_id": "TBv1-abc"}

    out = _order_attrs_for_diagnostics(_FakeV1())
    assert out["client_order_id"] == "TBv1-abc"


def test_diagnose_walks_attrs_on_plain_object() -> None:
    """Plain SimpleNamespace (or mock): fall back to getattr on a known list."""
    from strategy.broker import _order_attrs_for_diagnostics

    parent = SimpleNamespace(
        id="p-id",
        client_order_id="TBv1-parent",
        symbol="AAPL",
        qty="10",
        status="new",
        order_class="oto",
        order_type="limit",
        legs=[
            SimpleNamespace(
                id="c-id",
                client_order_id="alpaca-generated-uuid",
                symbol="AAPL",
                qty="10",
                status="held",
                order_class="simple",
                order_type="stop",
                parent_id="p-id",
                parent_client_order_id=None,
                legs=None,
            )
        ],
    )
    out = _order_attrs_for_diagnostics(parent)
    assert out["client_order_id"] == "TBv1-parent"
    assert out["legs"][0]["parent_id"] == "p-id"
    assert out["legs"][0]["order_type"] == "stop"


def test_diagnose_none_input() -> None:
    from strategy.broker import _order_attrs_for_diagnostics
    assert _order_attrs_for_diagnostics(None) is None


def test_diagnose_order_by_coid_calls_sdk_and_returns_dict() -> None:
    b, client, _ = _broker()
    fake = SimpleNamespace(
        id="p-id",
        client_order_id="TBv1-xyz",
        symbol="AAPL",
        qty="10",
        status="new",
        order_class="oto",
        order_type="limit",
        legs=[],
    )
    client.get_order_by_client_id.return_value = fake
    out = b.diagnose_order_by_coid("TBv1-xyz")
    assert out["client_order_id"] == "TBv1-xyz"
    assert out["id"] == "p-id"


def test_alpaca_imports_only_in_broker() -> None:
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "strategy"
    offenders: list[str] = []
    for p in root.glob("*.py"):
        if p.name == "broker.py":
            continue
        txt = p.read_text(encoding="utf-8")
        for lineno, line in enumerate(txt.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import alpaca", "from alpaca")):
                offenders.append(f"{p.name}:{lineno}: {stripped}")
    assert not offenders, f"alpaca SDK imported outside broker.py:\n" + "\n".join(offenders)
