"""Tool interfaces for TraceGuard with O(1) composite indexing, bounded retry, and fault injection."""

from __future__ import annotations
from collections import defaultdict
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from traceguard.models import EventRecord
from traceguard.reconciler import parse_event_dict

T = TypeVar("T")


class ToolError(Exception):
    """Raised when a tool call fails or times out."""


class ToolTimeoutError(ToolError):
    """Raised when a tool call exceeds its execution deadline."""


class EventStore:
    """
    In-memory indexed store loaded from a JSONL file exposing the three required tools:
      - get_order(tenant_id, order_id)
      - get_payment_events(tenant_id, order_id)
      - get_support_notes(tenant_id, order_id)
    Uses an O(1) composite hash index on (tenant_id, order_id) for fast lookup.
    Supports simulated transient failures/timeouts to demonstrate bounded retry.
    """

    def __init__(
        self,
        events_path: Optional[str | Path] = None,
        raw_lines: Optional[List[str]] = None,
        transient_failures: Optional[Dict[str, int]] = None,
    ) -> None:
        self.records: List[EventRecord] = []
        self._index: Dict[Tuple[Optional[str], Optional[str]], List[EventRecord]] = defaultdict(list)
        self.parse_anomalies: List[str] = []
        self.transient_failures: Dict[str, int] = dict(transient_failures or {})
        self.call_counts: Dict[str, int] = {
            "get_order": 0,
            "get_payment_events": 0,
            "get_support_notes": 0,
        }

        if events_path is not None:
            self._load_file(Path(events_path))
        elif raw_lines is not None:
            self._load_lines(raw_lines)

    def _load_file(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Events file not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            self._load_lines(f.readlines())

    def _load_lines(self, lines: List[str]) -> None:
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                self.parse_anomalies.append(f"MALFORMED_JSON_LINE_{idx}: {exc.msg}")
                continue

            record, err = parse_event_dict(raw_obj, line_number=idx)
            if err or record is None:
                self.parse_anomalies.append(f"STRUCTURAL_PARSE_ERROR_LINE_{idx}: {err}")
            else:
                self.records.append(record)
                self._index[(record.tenant_id, record.order_id)].append(record)

    def _maybe_fail(self, tool_name: str) -> None:
        self.call_counts[tool_name] = self.call_counts.get(tool_name, 0) + 1
        remaining = self.transient_failures.get(tool_name, 0)
        if remaining > 0:
            self.transient_failures[tool_name] = remaining - 1
            raise ToolTimeoutError(f"Transient timeout in {tool_name} (attempt {self.call_counts[tool_name]})")

    def get_order(self, tenant_id: str, order_id: str) -> List[EventRecord]:
        """Return checkout / order-definition records for (tenant_id, order_id) in O(1) indexed time."""
        self._maybe_fail("get_order")
        scoped = self._index.get((tenant_id, order_id), [])
        return [
            r for r in scoped
            if r.source == "checkout" or r.event_type == "ORDER_CREATED"
        ]

    def get_payment_events(self, tenant_id: str, order_id: str) -> List[EventRecord]:
        """Return payment records and any non-note/non-order records for (tenant_id, order_id) in O(1) indexed time."""
        self._maybe_fail("get_payment_events")
        scoped = self._index.get((tenant_id, order_id), [])
        return [
            r for r in scoped
            if (
                r.source == "payments"
                or r.event_type in {"PAYMENT_FAILED", "PAYMENT_CAPTURED", "REFUND_ISSUED", "CHARGEBACK_OPENED"}
                or (r.source not in {"checkout", "support"} and r.event_type not in {"ORDER_CREATED", "NOTE"})
            )
        ]

    def get_support_notes(self, tenant_id: str, order_id: str) -> List[EventRecord]:
        """Return support note records for (tenant_id, order_id) in O(1) indexed time."""
        self._maybe_fail("get_support_notes")
        scoped = self._index.get((tenant_id, order_id), [])
        return [
            r for r in scoped
            if r.source == "support" or r.event_type == "NOTE"
        ]


_ACTIVE_STORE: Optional[EventStore] = None


def set_active_store(store: EventStore) -> None:
    global _ACTIVE_STORE
    _ACTIVE_STORE = store


def get_order(tenant_id: str, order_id: str) -> List[EventRecord]:
    if _ACTIVE_STORE is None:
        raise ToolError("EventStore is not initialized.")
    return _ACTIVE_STORE.get_order(tenant_id, order_id)


def get_payment_events(tenant_id: str, order_id: str) -> List[EventRecord]:
    if _ACTIVE_STORE is None:
        raise ToolError("EventStore is not initialized.")
    return _ACTIVE_STORE.get_payment_events(tenant_id, order_id)


def get_support_notes(tenant_id: str, order_id: str) -> List[EventRecord]:
    if _ACTIVE_STORE is None:
        raise ToolError("EventStore is not initialized.")
    return _ACTIVE_STORE.get_support_notes(tenant_id, order_id)


def call_with_bounded_retry(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 2,
    tool_name: str = "tool",
) -> Tuple[Optional[T], List[str], bool]:
    """
    Execute a tool function with a strict bounded retry limit (default max_retries=2, i.e., up to 3 total attempts).
    Returns: (result_or_none, warning_messages, exhausted_failure_flag)
    """
    if max_retries < 0 or max_retries > 5:
        raise ValueError("max_retries must be bounded between 0 and 5.")

    warnings: List[str] = []
    attempts = 0
    max_attempts = max_retries + 1

    while attempts < max_attempts:
        attempts += 1
        try:
            res = func(*args)
            return res, warnings, False
        except Exception as exc:
            if attempts < max_attempts:
                warnings.append(
                    f"TOOL_RETRY_RECOVERED: {tool_name} failed on attempt {attempts}/{max_attempts} ({exc}); succeeded on retry."
                )
            else:
                warnings.append(
                    f"TOOL_FAILURE_EXHAUSTED: {tool_name} failed after {max_attempts} bounded attempts ({exc})."
                )
                return None, warnings, True

    return None, warnings, True