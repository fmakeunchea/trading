"""Typed exception hierarchy.

Structural intent:
* TransientBrokerError is retryable by the broker wrapper; PermanentBrokerError is not.
* ReconcileMismatch and RiskDeny carry structured payloads so the orchestrator
  can audit-log the exact reason without reformatting.
* AccountRestricted signals bot-level halt, not just per-intent reject.
* MarketClosedRejection signals sleep-to-open, not retry-now.

All exceptions are children of StrategyError so a single except clause at
the top-level loop can capture anything raised by this package without
accidentally swallowing SystemExit / KeyboardInterrupt.
"""
from __future__ import annotations

from typing import Any


class StrategyError(Exception):
    """Root of the package exception hierarchy."""


class ConfigError(StrategyError):
    """Raised when configuration fails to load or validate."""


# --- Broker errors ---------------------------------------------------------


class BrokerError(StrategyError):
    """Abstract base for broker-layer errors.

    Do not raise this directly; raise one of its concrete subclasses.
    """


class TransientBrokerError(BrokerError):
    """Retryable broker error.

    The broker wrapper will retry up to max_retries with exponential backoff.
    Covers: HTTP 5xx, 429, connection refused, read timeout, TLS/DNS errors.
    """


class PermanentBrokerError(BrokerError):
    """Non-retryable broker error. The orchestrator must decide whether to
    reject the intent, halt the bot, or sleep.
    """


class DuplicateClientOrderId(PermanentBrokerError):
    """The broker rejected submission because our COID already exists.

    By design this is resolved by fetching the existing order by COID; the
    orchestrator treats this as 'already submitted' and continues.
    """


class OrderRejected(PermanentBrokerError):
    """Broker accepted the HTTP request but rejected the order for a
    business-rule reason (wash trade, PDT, untradable symbol, etc.).

    The intent is dropped; the bot continues.
    """

    def __init__(self, message: str, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class AccountRestricted(PermanentBrokerError):
    """Account is restricted or blocked from trading (HTTP 403).

    This is a bot-level halt, not a per-intent reject. The orchestrator
    must stop submitting new entries until an operator clears the block.
    """


class MarketClosedRejection(PermanentBrokerError):
    """Broker rejected because the market is closed.

    The orchestrator should sleep until next open rather than retry.
    """


# --- Risk / reconciliation / operational -----------------------------------


class ReconcileMismatch(StrategyError):
    """Local state and broker truth disagree.

    Carries the :class:`ReconcileReport` so the orchestrator can log the
    exact categories without re-running the diff.
    """

    def __init__(self, message: str, report: Any | None = None) -> None:
        super().__init__(message)
        self.report = report


class RiskDeny(StrategyError):
    """Risk engine denied an intent.

    Carries the :class:`RiskDecision` so the trade log captures the exact
    gate that triggered.
    """

    def __init__(self, message: str, decision: Any | None = None) -> None:
        super().__init__(message)
        self.decision = decision


class StaleDataError(StrategyError):
    """Latest market data is older than the configured staleness threshold."""


class KillSwitchActive(StrategyError):
    """Operator kill switch file is present; no new entries permitted."""


class StateCorruption(StrategyError):
    """StrategyState on disk is corrupt or schema-incompatible.

    The bot must not continue with unknown state. Fail closed.
    """


class AuditIntegrityError(StrategyError):
    """Trade-log hash chain is broken or tampered.

    The audit trail can no longer be trusted. Fail closed.
    """
