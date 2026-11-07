#!/usr/bin/env python3
"""Interactive Showcase & Performance Benchmark for TraceGuard."""

from __future__ import annotations
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from traceguard.tools import EventStore
from traceguard.workflow import InvestigationWorkflow

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "events.jsonl"


def print_banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main() -> None:
    store = EventStore(events_path=DATA_PATH)
    wf = InvestigationWorkflow(store=store, max_retries=2)

    scenarios = [
        ("CASE 1: Normal Investigation + Deduplication + Failed Attempt (T1 / O-201)", "T1", "O-201", "Was this order paid?", None),
        ("CASE 2: Cross-Tenant Isolation Collision Check (T2 / O-201)", "T2", "O-201", "Why does checkout show a failure?", None),
        ("CASE 3: Adversarial Prompt Injection + Out-of-Order Events (T1 / O-202)", "T1", "O-202", "Was order O-202 refunded?", None),
        ("CASE 4: Open Chargeback / Disputed Status Preserving Totals (T1 / O-206)", "T1", "O-206", "What evidence supports the current status?", None),
        ("CASE 5: Conflicting Order Definitions Integrity Failure (T1 / O-208)", "T1", "O-208", "What is the status?", None),
        ("CASE 6: Transient Tool Timeout + Bounded Retry Recovery (T1 / O-201)", "T1", "O-201", "Was this order paid?", {"get_payment_events": 1}),
    ]

    for title, tenant, order, question, fault in scenarios:
        print_banner(title)
        s = EventStore(events_path=DATA_PATH, transient_failures=fault)
        w = InvestigationWorkflow(store=s, max_retries=2)
        res = w.investigate(tenant_id=tenant, order_id=order, question=question)
        print(json.dumps(res.to_dict(), indent=2))

    print_banner("BENCHMARK: O(1) Composite Index Lookup Latency on 124-Event Store")
    t0 = time.perf_counter()
    iterations = 1000
    for _ in range(iterations):
        wf.investigate("T1", "O-201", "Was this order paid?")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"Executed {iterations} full investigations in {elapsed_ms:.2f} ms ({elapsed_ms/iterations:.4f} ms / investigation).")


if __name__ == "__main__":
    main()