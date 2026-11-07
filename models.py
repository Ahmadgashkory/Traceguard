"""Data models and enumerations for TraceGuard."""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderStatus(str, Enum):
    PAID = "PAID"
    UNPAID = "UNPAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    DISPUTED = "DISPUTED"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


SUPPORTED_EVENT_TYPES = {
    "ORDER_CREATED",
    "PAYMENT_FAILED",
    "PAYMENT_CAPTURED",
    "REFUND_ISSUED",
    "CHARGEBACK_OPENED",
    "NOTE",
}

SUPPORTED_SOURCES = {"checkout", "payments", "support"}


@dataclass(frozen=True)
class EventRecord:
    """Normalized representation of a raw JSONL event line."""
    raw: Dict[str, Any]
    line_number: int
    tenant_id: Optional[str] = None
    order_id: Optional[str] = None
    event_id: Optional[str] = None
    source: Optional[str] = None
    event_type: Optional[str] = None
    occurred_at: Optional[str] = None
    ingested_at: Optional[str] = None
    attempt_id: Optional[str] = None
    capture_id: Optional[str] = None
    refund_id: Optional[str] = None
    amount_cents: Optional[int] = None
    currency: Optional[str] = None
    text: Optional[str] = None

    def substantive_signature(self) -> Dict[str, Any]:
        """Return all substantive fields (excluding ingested_at) for deduplication/conflict detection."""
        return {
            "tenant_id": self.tenant_id,
            "order_id": self.order_id,
            "event_id": self.event_id,
            "source": self.source,
            "type": self.event_type,
            "occurred_at": self.occurred_at,
            "attempt_id": self.attempt_id,
            "capture_id": self.capture_id,
            "refund_id": self.refund_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "text": self.text,
        }


@dataclass
class InvestigationResult:
    """Final output format required by the TraceGuard CLI specification."""
    status: OrderStatus
    captured_cents: Optional[int]
    refunded_cents: Optional[int]
    net_paid_cents: Optional[int]
    anomalies: List[str] = field(default_factory=list)
    evidence_event_ids: List[str] = field(default_factory=list)
    explanation: str = ""
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "captured_cents": self.captured_cents,
            "refunded_cents": self.refunded_cents,
            "net_paid_cents": self.net_paid_cents,
            "anomalies": self.anomalies,
            "evidence_event_ids": self.evidence_event_ids,
            "explanation": self.explanation,
            "confidence": self.confidence.value,
        }