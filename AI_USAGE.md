
## 1. AI Tools Used
* **Claude**: Used for requirements analysis, brainstorming metamorphic/property-based test invariants, and structuring the CLI and dataset generator.

## 2. One Useful AI Suggestion Adopted
* **Metamorphic & Property-Based Test Suite (`tests/test_traceguard.py`):** AI suggested designing 6 mathematical invariant tests covering seeded event permutations (`test_22`), k-fold duplicate injection (`test_23`), adversarial support-note payload injection (`test_24`), cross-tenant collision noise (`test_25`), random multi-capture/partial-refund conservation (`test_26`), and timezone-offset equivalence (`test_30`). This caught the ISO-8601 timezone offset sorting bug early and proved deterministic compliance with Rules 02, 04, 08, and 09.

## 3. One AI Suggestion Corrected / Rejected
* **Rejected Suggestion — Treating `CHARGEBACK_OPENED` as Subtracting from `captured_cents` or `net_paid_cents`:**
  * **Initial Suggestion:** An early draft suggested setting `net_paid_cents = 0` when a `CHARGEBACK_OPENED` event arrived, assuming disputed funds should be deducted from net paid totals.
  * **Correction:** We rejected this by verifying Section 3 of the assessment specification (*"An open chargeback changes the order status but does not automatically change the captured or refunded totals"*). We corrected `reconcile_order()` so that `CHARGEBACK_OPENED` transitions `status` to `DISPUTED` (Precedence Rule 2) while strictly preserving `captured_cents`, `refunded_cents`, and `net_paid_cents = captured_cents - refunded_cents` (verified by `test_06_disputed_precedence_preserves_monetary_totals`).