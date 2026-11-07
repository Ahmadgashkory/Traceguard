# TraceGuard: Deterministic Financial Incident Investigation System

TraceGuard is an incident investigation service that reconstructs the financial state of e-commerce orders across asynchronous `checkout`, `payments`, and `support` systems. It isolates multi-tenant records with $O(1)$ composite indexing, resolves out-of-order and timezone-shifted event streams, defends against prompt injection in untrusted support notes, and produces verifiable explanations backed by specific event IDs.

## Setup & Execution Commands

* **Python Version:** Python 3.11+
* **External Dependencies:** Zero external packages required (uses Python standard library).

```bash
# 1. Generate the 124-event JSONL dataset (data/events.jsonl)
python scripts/generate_dataset.py

# 2. Run a single CLI investigation (exact assessment command format)
python run.py --events data/events.jsonl \
  --tenant T1 \
  --order O-201 \
  --question "Was this order paid?"

# 3. Run the 6-scenario showcase + latency benchmark
python scripts/run_demo.py

# 4. Run all 30 automated tests
python -m unittest discover -s tests -p "test_*.py" -v



Confidence Assignment Rules
HIGH: All required tools succeeded on the first attempt, ORDER_CREATED and payment records are consistent, and the status was established without data-integrity errors.

MEDIUM: The financial status was deterministically established from validated checkout and payments records, but a non-fatal operational event occurred (e.g., a transient tool timeout recovered via bounded retry, or get_support_notes experienced a service outage and fell back to empty notes).

LOW: The reconciled status is UNKNOWN due to an integrity error, missing ORDER_CREATED evidence, incomplete payment history, or exhausted tool retries on critical sources (get_order or get_payment_events).

Documented Assumptions
Null Monetary Totals on Integrity Failure: Whenever an integrity error or missing ORDER_CREATED forces status = "UNKNOWN" (Precedence Rule 1), captured_cents, refunded_cents, and net_paid_cents are set to null rather than 0.

Incomplete Payment History (Precedence Rule 8): When a valid ORDER_CREATED exists with zero captures and zero payment failures (O-215), the order falls through to Rule 8 (UNKNOWN) with 0 totals and LOW confidence.

UTC Epoch Normalization Across Timezone Offsets: Timestamps with explicit offsets (e.g., +05:00, -04:00) are normalized to UTC epoch seconds before chronological sorting.

Boolean Type Trap Prevention: Because bool is a subclass of int in Python, parse_event_dict explicitly rejects boolean values in amount_cents.

Failing Test Encountered & Fix
Failing Test: test_30_metamorphic_timezone_offset_equivalence failed when comparing events logged in UTC (Z) against the exact same instants logged with regional offsets (+05:00, -04:00, +02:00).

Root Cause: String-based ISO sorting (dt.isoformat()) compares local wall-clock digits before the timezone offset suffix, causing 2026-03-01T06:01:00-04:00 (10:01Z) to sort ahead of 2026-03-01T15:00:00+05:00 (10:00Z).

Fix: Updated canonical_timestamp_key() in traceguard/reconciler.py to project parsed timestamps to UTC epoch floats (dt.timestamp()), ensuring identical instants across any timezone sort identically.





