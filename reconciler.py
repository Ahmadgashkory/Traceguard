"""Deterministic financial reconciliation engine for TraceGuard."""

from __future__ import annotations
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from traceguard.models import (
    ConfidenceLevel,
    EventRecord,
    InvestigationResult,
    OrderStatus,
    SUPPORTED_EVENT_TYPES,
    SUPPORTED_SOURCES,
)

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"mark\s+this\s+order\s+(as\s+)?(paid|refunded|unpaid|disputed|partially_paid|partially_refunded)", re.IGNORECASE),
    re.compile(r"do\s+not\s+mention\s+this\s+(override|instruction|note)", re.IGNORECASE),
    re.compile(r"financial\s+records\s+are\s+outdated", re.IGNORECASE),
    re.compile(r"<\s*(system|admin|override|instruction)\s*>", re.IGNORECASE),
    re.compile(r'"status"\s*:\s*"(PAID|REFUNDED|UNPAID|DISPUTED)"', re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+(admin|developer|override)\s+mode", re.IGNORECASE),
]


def is_valid_iso8601(ts: Optional[str]) -> bool:
    """Validate ISO 8601 timestamp format and return True if parseable."""
    if not ts or not isinstance(ts, str):
        return False
    try:
        normalized = ts.replace("Z", "+00:00")
        datetime.fromisoformat(normalized)
        return True
    except ValueError:
        return False


def canonical_timestamp_key(ts: Optional[str]) -> Tuple[int, float]:
    """Return a UTC-normalized sortable epoch key for ISO 8601 timestamps."""
    if not ts or not isinstance(ts, str):
        return (1, 0.0)
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (0, dt.timestamp())
    except ValueError:
        return (1, 0.0)


def parse_event_dict(raw: Dict[str, Any], line_number: int = 0) -> Tuple[Optional[EventRecord], Optional[str]]:
    """Parse and structurally validate a raw JSON event dictionary."""
    if not isinstance(raw, dict):
        return None, f"Line {line_number}: Event record is not a JSON object."

    tenant_id = raw.get("tenant_id")
    order_id = raw.get("order_id")
    event_id = raw.get("event_id")
    source = raw.get("source")
    event_type = raw.get("type")
    occurred_at = raw.get("occurred_at")
    ingested_at = raw.get("ingested_at")
    attempt_id = raw.get("attempt_id")
    capture_id = raw.get("capture_id")
    refund_id = raw.get("refund_id")
    amount_cents = raw.get("amount_cents")
    currency = raw.get("currency")
    text = raw.get("text")

    if amount_cents is not None and (isinstance(amount_cents, bool) or not isinstance(amount_cents, int)):
        return None, f"Event {event_id or 'unknown'}: amount_cents must be an integer."

    record = EventRecord(
        raw=raw,
        line_number=line_number,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        order_id=str(order_id) if order_id is not None else None,
        event_id=str(event_id) if event_id is not None else None,
        source=str(source) if source is not None else None,
        event_type=str(event_type) if event_type is not None else None,
        occurred_at=str(occurred_at) if occurred_at is not None else None,
        ingested_at=str(ingested_at) if ingested_at is not None else None,
        attempt_id=str(attempt_id) if attempt_id is not None else None,
        capture_id=str(capture_id) if capture_id is not None else None,
        refund_id=str(refund_id) if refund_id is not None else None,
        amount_cents=amount_cents,
        currency=str(currency) if currency is not None else None,
        text=str(text) if text is not None else None,
    )
    return record, None


def detect_prompt_injection(text: Optional[str]) -> bool:
    """Detect adversarial prompt injection strings inside support notes."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in PROMPT_INJECTION_PATTERNS)


def compute_audit_digest(
    tenant_id: str,
    order_id: str,
    status: str,
    captured_cents: Optional[int],
    refunded_cents: Optional[int],
    net_paid_cents: Optional[int],
    evidence_event_ids: List[str],
) -> str:
    """Compute a deterministic SHA-256 audit digest over the reconciled financial state."""
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "order_id": order_id,
            "status": status,
            "captured_cents": captured_cents,
            "refunded_cents": refunded_cents,
            "net_paid_cents": net_paid_cents,
            "evidence_event_ids": evidence_event_ids,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def reconcile_order(
    tenant_id: str,
    order_id: str,
    events: List[EventRecord],
    parse_anomalies: Optional[List[str]] = None,
    tool_anomalies: Optional[List[str]] = None,
) -> Tuple[InvestigationResult, Dict[str, Any]]:
    """
    Deterministic reconciliation engine implementing Rules 01-10 and Precedence 1-8.
    Returns (InvestigationResult, diagnostic_context_dict).
    """
    anomalies: List[str] = []
    if parse_anomalies:
        anomalies.extend(parse_anomalies)
    if tool_anomalies:
        anomalies.extend(tool_anomalies)

    integrity_error = bool(parse_anomalies or tool_anomalies)
    duplicate_event_ids: List[str] = []
    adversarial_note_ids: List[str] = []

    # Rule 09: Strict tenant and order isolation
    scoped_events = [
        e for e in events
        if e.tenant_id == tenant_id and e.order_id == order_id
    ]

    if not scoped_events and not anomalies:
        return (
            InvestigationResult(
                status=OrderStatus.UNKNOWN,
                captured_cents=None,
                refunded_cents=None,
                net_paid_cents=None,
                anomalies=["MISSING_ORDER_EVIDENCE: No events found for the specified tenant and order."],
                evidence_event_ids=[],
                explanation=f"No events found for tenant {tenant_id} and order {order_id}.",
                confidence=ConfidenceLevel.LOW,
            ),
            {},
        )

    # Rule 04 & Rule 05: Deduplication by event_id within tenant, and conflict detection
    deduped_by_id: Dict[str, EventRecord] = {}
    for ev in scoped_events:
        if not ev.event_id:
            anomalies.append(f"MISSING_EVENT_ID: Record at line {ev.line_number} is missing event_id.")
            integrity_error = True
            continue

        if ev.event_id in deduped_by_id:
            existing = deduped_by_id[ev.event_id]
            if existing.substantive_signature() == ev.substantive_signature():
                if ev.event_id not in duplicate_event_ids:
                    duplicate_event_ids.append(ev.event_id)
                continue
            else:
                anomalies.append(
                    f"EVENT_ID_CONFLICT: Event '{ev.event_id}' appears multiple times with conflicting substantive fields."
                )
                integrity_error = True
                continue
        deduped_by_id[ev.event_id] = ev

    # Rule 08: Sort deterministically by canonical (occurred_at, event_id) so arrival order never alters output
    unique_events = sorted(
        deduped_by_id.values(),
        key=lambda x: (canonical_timestamp_key(x.occurred_at), x.event_id or ""),
    )

    order_events: List[EventRecord] = []
    capture_events: List[EventRecord] = []
    refund_events: List[EventRecord] = []
    failed_events: List[EventRecord] = []
    chargeback_events: List[EventRecord] = []
    note_events: List[EventRecord] = []
    evidence_ids: List[str] = []

    # Validate individual event schemas, timestamps, and sources
    for ev in unique_events:
        if ev.source not in SUPPORTED_SOURCES:
            anomalies.append(f"UNSUPPORTED_SOURCE: Event '{ev.event_id}' has invalid source '{ev.source}'.")
            integrity_error = True
            continue

        if ev.event_type not in SUPPORTED_EVENT_TYPES:
            anomalies.append(f"UNSUPPORTED_EVENT_TYPE: Event '{ev.event_id}' has unsupported type '{ev.event_type}'.")
            integrity_error = True
            continue

        if not is_valid_iso8601(ev.occurred_at) or not is_valid_iso8601(ev.ingested_at):
            anomalies.append(f"INVALID_TIMESTAMP: Event '{ev.event_id}' has malformed ISO-8601 timestamp.")
            integrity_error = True
            continue

        # Rule 01 & Rule 02: Source & Type alignment
        if ev.event_type == "ORDER_CREATED":
            if ev.source != "checkout":
                anomalies.append(
                    f"UNAUTHORIZED_ORDER_SOURCE: Event '{ev.event_id}' is ORDER_CREATED from non-checkout source '{ev.source}'."
                )
                integrity_error = True
                continue
            if ev.amount_cents is None or ev.amount_cents <= 0 or not ev.currency:
                anomalies.append(
                    f"INVALID_ORDER_DEFINITION: Event '{ev.event_id}' has invalid amount_cents or missing currency."
                )
                integrity_error = True
                continue
            order_events.append(ev)

        elif ev.event_type in {"PAYMENT_FAILED", "PAYMENT_CAPTURED", "REFUND_ISSUED", "CHARGEBACK_OPENED"}:
            if ev.source != "payments":
                anomalies.append(
                    f"UNAUTHORIZED_PAYMENT_SOURCE: Event '{ev.event_id}' ({ev.event_type}) originated from '{ev.source}' instead of 'payments'."
                )
                integrity_error = True
                continue

            if ev.event_type == "PAYMENT_CAPTURED":
                if not ev.capture_id or ev.amount_cents is None or ev.amount_cents <= 0 or not ev.currency:
                    anomalies.append(
                        f"MALFORMED_CAPTURE: Event '{ev.event_id}' is missing capture_id, valid positive amount_cents, or currency."
                    )
                    integrity_error = True
                    continue
                capture_events.append(ev)

            elif ev.event_type == "REFUND_ISSUED":
                if not ev.refund_id or not ev.capture_id or ev.amount_cents is None or ev.amount_cents <= 0 or not ev.currency:
                    anomalies.append(
                        f"MALFORMED_REFUND: Event '{ev.event_id}' is missing refund_id, capture_id, valid positive amount_cents, or currency."
                    )
                    integrity_error = True
                    continue
                refund_events.append(ev)

            elif ev.event_type == "PAYMENT_FAILED":
                if ev.amount_cents is not None and ev.amount_cents <= 0:
                    anomalies.append(f"MALFORMED_FAILURE: Event '{ev.event_id}' has non-positive amount_cents.")
                    integrity_error = True
                    continue
                failed_events.append(ev)

            elif ev.event_type == "CHARGEBACK_OPENED":
                if not ev.capture_id:
                    anomalies.append(f"MALFORMED_CHARGEBACK: Event '{ev.event_id}' is missing required capture_id.")
                    integrity_error = True
                    continue
                chargeback_events.append(ev)

        elif ev.event_type == "NOTE":
            note_events.append(ev)
            if detect_prompt_injection(ev.text):
                adversarial_note_ids.append(ev.event_id)
                anomalies.append(
                    f"ADVERSARIAL_NOTE_IGNORED: Support note '{ev.event_id}' contains prompt-injection instructions and was ignored as untrusted."
                )

    # Rule 01 & Rule 10: Check ORDER_CREATED existence and consistency
    expected_amount: Optional[int] = None
    expected_currency: Optional[str] = None

    if not order_events:
        anomalies.append("MISSING_ORDER_CREATED: No valid checkout ORDER_CREATED event exists for this order.")
        integrity_error = True
    else:
        first_order = order_events[0]
        expected_amount = first_order.amount_cents
        expected_currency = first_order.currency
        evidence_ids.append(first_order.event_id)

        for oe in order_events[1:]:
            if oe.event_id not in evidence_ids:
                evidence_ids.append(oe.event_id)
            if oe.amount_cents != expected_amount or oe.currency != expected_currency:
                anomalies.append(
                    f"CONFLICTING_ORDER_DEFINITION: ORDER_CREATED event '{oe.event_id}' conflicts with '{first_order.event_id}'."
                )
                integrity_error = True

    # Rule 06: Validate capture_id uniqueness and currency match
    captures_by_id: Dict[str, EventRecord] = {}
    for cap in capture_events:
        if cap.event_id not in evidence_ids:
            evidence_ids.append(cap.event_id)
        if expected_currency and cap.currency != expected_currency:
            anomalies.append(
                f"CURRENCY_MISMATCH: Capture '{cap.event_id}' uses currency '{cap.currency}', expected '{expected_currency}'."
            )
            integrity_error = True
        if cap.capture_id in captures_by_id:
            prev = captures_by_id[cap.capture_id]
            if (
                prev.amount_cents != cap.amount_cents
                or prev.currency != cap.currency
                or prev.attempt_id != cap.attempt_id
            ):
                anomalies.append(
                    f"CAPTURE_ID_CONFLICT: capture_id '{cap.capture_id}' reused with conflicting details in '{cap.event_id}' and '{prev.event_id}'."
                )
                integrity_error = True
            else:
                anomalies.append(
                    f"DUPLICATE_CAPTURE_ID: capture_id '{cap.capture_id}' reused across distinct event IDs '{prev.event_id}' and '{cap.event_id}'."
                )
                integrity_error = True
        else:
            captures_by_id[cap.capture_id] = cap

    # Rule 06 & Rule 07: Validate refund_id uniqueness, referenced capture_id, currency, and per-capture refund cap
    refunds_by_id: Dict[str, EventRecord] = {}
    refunds_per_capture: Dict[str, int] = {cid: 0 for cid in captures_by_id}

    for ref in refund_events:
        if ref.event_id not in evidence_ids:
            evidence_ids.append(ref.event_id)
        if expected_currency and ref.currency != expected_currency:
            anomalies.append(
                f"CURRENCY_MISMATCH: Refund '{ref.event_id}' uses currency '{ref.currency}', expected '{expected_currency}'."
            )
            integrity_error = True

        if ref.refund_id in refunds_by_id:
            prev_ref = refunds_by_id[ref.refund_id]
            anomalies.append(
                f"REFUND_ID_CONFLICT: refund_id '{ref.refund_id}' reused across events '{prev_ref.event_id}' and '{ref.event_id}'."
            )
            integrity_error = True
            continue
        refunds_by_id[ref.refund_id] = ref

        if ref.capture_id not in captures_by_id:
            anomalies.append(
                f"INVALID_REFUND_REFERENCE: Refund '{ref.event_id}' references non-existent capture_id '{ref.capture_id}'."
            )
            integrity_error = True
            continue

        refunds_per_capture[ref.capture_id] += ref.amount_cents or 0
        cap_amount = captures_by_id[ref.capture_id].amount_cents or 0
        if refunds_per_capture[ref.capture_id] > cap_amount:
            anomalies.append(
                f"EXCESS_REFUND: Refunds against capture '{ref.capture_id}' ({refunds_per_capture[ref.capture_id]} cents) exceed captured amount ({cap_amount} cents)."
            )
            integrity_error = True

    # Validate failed payment events currency and include in evidence
    for fail in failed_events:
        if fail.event_id not in evidence_ids:
            evidence_ids.append(fail.event_id)
        if expected_currency and fail.currency and fail.currency != expected_currency:
            anomalies.append(
                f"CURRENCY_MISMATCH: Failed payment '{fail.event_id}' uses currency '{fail.currency}', expected '{expected_currency}'."
            )
            integrity_error = True

    # Validate chargebacks reference an existing capture_id
    valid_chargebacks: List[EventRecord] = []
    for cb in chargeback_events:
        if cb.event_id not in evidence_ids:
            evidence_ids.append(cb.event_id)
        if cb.capture_id not in captures_by_id:
            anomalies.append(
                f"INVALID_CHARGEBACK_REFERENCE: Chargeback '{cb.event_id}' references non-existent capture_id '{cb.capture_id}'."
            )
            integrity_error = True
        else:
            valid_chargebacks.append(cb)

    # Compute monetary totals
    total_captured = sum((c.amount_cents or 0) for c in captures_by_id.values())
    total_refunded = sum((r.amount_cents or 0) for r in refunds_by_id.values())

    # Rule 10: Check over-capture against expected order amount
    if expected_amount is not None and total_captured > expected_amount:
        anomalies.append(
            f"OVER_CAPTURE: Total captured amount ({total_captured} cents) exceeds expected order amount ({expected_amount} cents)."
        )
        integrity_error = True

    # Status Decision Precedence (1 to 8)
    # 1. Integrity failure or missing essential order evidence -> UNKNOWN
    if integrity_error or expected_amount is None:
        status = OrderStatus.UNKNOWN
        captured_out: Optional[int] = None
        refunded_out: Optional[int] = None
        net_paid_out: Optional[int] = None
        confidence = ConfidenceLevel.LOW
    else:
        captured_out = total_captured
        refunded_out = total_refunded
        net_paid_out = total_captured - total_refunded

        # 2. Valid open chargeback on the order -> DISPUTED
        if valid_chargebacks:
            status = OrderStatus.DISPUTED
        # 3. No captures, with verified payment failure evidence -> UNPAID
        elif total_captured == 0 and len(failed_events) > 0:
            status = OrderStatus.UNPAID
        # 4. Refunded amount equals captured amount, both greater than zero -> REFUNDED
        elif total_captured > 0 and total_refunded == total_captured:
            status = OrderStatus.REFUNDED
        # 5. Refunded amount is positive and less than captured amount -> PARTIALLY_REFUNDED
        elif total_captured > 0 and 0 < total_refunded < total_captured:
            status = OrderStatus.PARTIALLY_REFUNDED
        # 6. Captured amount equals expected order amount -> PAID
        elif total_captured == expected_amount:
            status = OrderStatus.PAID
        # 7. Captured amount is positive but less than expected amount -> PARTIALLY_PAID
        elif 0 < total_captured < expected_amount:
            status = OrderStatus.PARTIALLY_PAID
        # 8. No rule above can be established (e.g. only ORDER_CREATED, no payment events) -> UNKNOWN
        else:
            status = OrderStatus.UNKNOWN
            anomalies.append(
                "INCOMPLETE_PAYMENT_HISTORY: Order was created but has no payment captures or failure records."
            )

        if status == OrderStatus.UNKNOWN:
            confidence = ConfidenceLevel.LOW
        else:
            confidence = ConfidenceLevel.HIGH

    # Ensure evidence_event_ids are ordered chronologically by (occurred_at, event_id)
    evidence_set = set(evidence_ids)
    ordered_evidence_ids = [ev.event_id for ev in unique_events if ev.event_id in evidence_set and ev.event_id is not None]

    audit_digest = compute_audit_digest(
        tenant_id=tenant_id,
        order_id=order_id,
        status=status.value,
        captured_cents=captured_out,
        refunded_cents=refunded_out,
        net_paid_cents=net_paid_out,
        evidence_event_ids=ordered_evidence_ids,
    )

    context = {
        "tenant_id": tenant_id,
        "order_id": order_id,
        "expected_amount": expected_amount,
        "expected_currency": expected_currency,
        "order_events": order_events,
        "captures": list(captures_by_id.values()),
        "refunds": list(refunds_by_id.values()),
        "failures": failed_events,
        "chargebacks": valid_chargebacks,
        "notes": note_events,
        "duplicate_event_ids": duplicate_event_ids,
        "adversarial_note_ids": adversarial_note_ids,
        "audit_digest": audit_digest,
    }

    result = InvestigationResult(
        status=status,
        captured_cents=captured_out,
        refunded_cents=refunded_out,
        net_paid_cents=net_paid_out,
        anomalies=anomalies,
        evidence_event_ids=ordered_evidence_ids,
        explanation="",
        confidence=confidence,
    )
    return result, context