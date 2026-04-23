"""Structural tests for the exception hierarchy.

The tests here do not exercise behaviour; they pin down the inheritance
graph so refactors can't silently re-parent an exception and change the
semantics of `except` clauses elsewhere.
"""
from __future__ import annotations

import pytest

from strategy.errors import (
    AccountRestricted,
    AuditIntegrityError,
    BrokerError,
    ConfigError,
    DuplicateClientOrderId,
    KillSwitchActive,
    MarketClosedRejection,
    OrderRejected,
    PermanentBrokerError,
    ReconcileMismatch,
    RiskDeny,
    StaleDataError,
    StateCorruption,
    StrategyError,
    TransientBrokerError,
)


ALL_STRATEGY_ERRORS = [
    ConfigError,
    BrokerError,
    TransientBrokerError,
    PermanentBrokerError,
    DuplicateClientOrderId,
    OrderRejected,
    AccountRestricted,
    MarketClosedRejection,
    ReconcileMismatch,
    RiskDeny,
    StaleDataError,
    KillSwitchActive,
    StateCorruption,
    AuditIntegrityError,
]


@pytest.mark.parametrize("cls", ALL_STRATEGY_ERRORS)
def test_all_errors_inherit_strategy_error(cls: type) -> None:
    assert issubclass(cls, StrategyError)
    assert issubclass(cls, Exception)


@pytest.mark.parametrize(
    "cls",
    [TransientBrokerError, PermanentBrokerError],
)
def test_broker_subtypes_inherit_broker_error(cls: type) -> None:
    assert issubclass(cls, BrokerError)


@pytest.mark.parametrize(
    "cls",
    [DuplicateClientOrderId, OrderRejected, AccountRestricted, MarketClosedRejection],
)
def test_permanent_subtypes_inherit_permanent(cls: type) -> None:
    assert issubclass(cls, PermanentBrokerError)


def test_transient_and_permanent_are_disjoint() -> None:
    assert not issubclass(TransientBrokerError, PermanentBrokerError)
    assert not issubclass(PermanentBrokerError, TransientBrokerError)


def test_order_rejected_carries_reason_code() -> None:
    exc = OrderRejected("wash trade blocked", reason_code="wash_trade")
    assert exc.reason_code == "wash_trade"
    assert "wash trade blocked" in str(exc)


def test_order_rejected_default_reason_code_none() -> None:
    exc = OrderRejected("generic")
    assert exc.reason_code is None


def test_reconcile_mismatch_carries_report() -> None:
    sentinel = object()
    exc = ReconcileMismatch("x", report=sentinel)
    assert exc.report is sentinel


def test_reconcile_mismatch_default_report_none() -> None:
    exc = ReconcileMismatch("x")
    assert exc.report is None


def test_risk_deny_carries_decision() -> None:
    sentinel = object()
    exc = RiskDeny("daily loss cap", decision=sentinel)
    assert exc.decision is sentinel


def test_risk_deny_default_decision_none() -> None:
    exc = RiskDeny("x")
    assert exc.decision is None


def test_raising_and_catching_as_root() -> None:
    with pytest.raises(StrategyError):
        raise TransientBrokerError("boom")
    with pytest.raises(StrategyError):
        raise RiskDeny("denied")
    with pytest.raises(StrategyError):
        raise AuditIntegrityError("hash chain broken")
