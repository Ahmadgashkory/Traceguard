#!/usr/bin/env python3
"""Command-line entrypoint for TraceGuard: Incident Investigation System."""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from traceguard.models import ConfidenceLevel, InvestigationResult, OrderStatus
from traceguard.tools import EventStore
from traceguard.workflow import InvestigationWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="TraceGuard: Deterministic Financial Incident Investigation CLI"
    )
    parser.add_argument(
        "--events",
        required=True,
        help="Path to the input JSON Lines (.jsonl) events file.",
    )
    parser.add_argument(
        "--tenant",
        required=True,
        help="Tenant identifier (e.g., T1).",
    )
    parser.add_argument(
        "--order",
        required=True,
        help="Order identifier (e.g., O-201).",
    )
    parser.add_argument(
        "--question",
        required=False,
        default="What is the current status of this order?",
        help="Investigation question to answer.",
    )
    parser.add_argument(
        "--simulate-transient-failure",
        required=False,
        default=None,
        choices=["get_order", "get_payment_events", "get_support_notes"],
        help="Optional flag to simulate a 1-time transient tool failure followed by bounded retry recovery.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    events_path = Path(args.events)
    if not events_path.exists():
        fallback = InvestigationResult(
            status=OrderStatus.UNKNOWN,
            captured_cents=None,
            refunded_cents=None,
            net_paid_cents=None,
            anomalies=[f"FILE_NOT_FOUND: Events file '{args.events}' does not exist."],
            evidence_event_ids=[],
            explanation=f"Cannot investigate order {args.order} because events file '{args.events}' was not found.",
            confidence=ConfidenceLevel.LOW,
        )
        print(json.dumps(fallback.to_dict(), indent=2))
        return 0

    transient = {args.simulate_transient_failure: 1} if args.simulate_transient_failure else None

    try:
        store = EventStore(events_path=events_path, transient_failures=transient)
        workflow = InvestigationWorkflow(store=store, max_retries=2)
        result = workflow.investigate(
            tenant_id=args.tenant,
            order_id=args.order,
            question=args.question,
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    except Exception as exc:
        fallback = InvestigationResult(
            status=OrderStatus.UNKNOWN,
            captured_cents=None,
            refunded_cents=None,
            net_paid_cents=None,
            anomalies=[f"UNEXPECTED_ENGINE_ERROR: {exc}"],
            evidence_event_ids=[],
            explanation=f"Investigation aborted safely due to controlled error: {exc}",
            confidence=ConfidenceLevel.LOW,
        )
        print(json.dumps(fallback.to_dict(), indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())