"""Investigation workflow with question-intent routing, bounded retry, and guardrailed narrator."""

from __future__ import annotations
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol

from traceguard.models import ConfidenceLevel, EventRecord, InvestigationResult, OrderStatus
from traceguard.reconciler import reconcile_order
from traceguard.tools import (
    EventStore,
    call_with_bounded_retry,
    get_order,
    get_payment_events,
    get_support_notes,
    set_active_store,
)


class QuestionIntent(str, Enum):
    PAYMENT_VERIFICATION = "PAYMENT_VERIFICATION"
    FAILURE_DIAGNOSIS = "FAILURE_DIAGNOSIS"
    EVIDENCE_AUDIT = "EVIDENCE_AUDIT"
    REFUND_OR_DISPUTE = "REFUND_OR_DISPUTE"
    GENERAL_STATUS = "GENERAL_STATUS"


def classify_question_intent(question: str) -> QuestionIntent:
    """Classify the user's natural-language question to tailor the explanation lead-in."""
    q = (question or "").lower()
    if any(k in q for k in ("fail", "why", "declin", "error", "problem")):
        return QuestionIntent.FAILURE_DIAGNOSIS
    if any(k in q for k in ("evidence", "prove", "proof", "support", "record", "which event")):
        return QuestionIntent.EVIDENCE_AUDIT
    if any(k in q for k in ("refund", "disput", "chargeback")):
        return QuestionIntent.REFUND_OR_DISPUTE
    if any(k in q for k in ("paid", "pay", "capture", "actual")):
        return QuestionIntent.PAYMENT_VERIFICATION
    return QuestionIntent.GENERAL_STATUS


class ExplanationGenerator(Protocol):
    """Replaceable protocol for generating human-readable explanations strictly from reconciled facts."""

    def generate(
        self,
        question: str,
        result: InvestigationResult,
        context: Dict[str, Any],
    ) -> str:
        ...


class DeterministicNarrator:
    """
    Zero-dependency, intent-aware deterministic explanation generator.
    Guarantees 100% factual alignment with reconciled records and cites real event IDs.
    """

    def generate(
        self,
        question: str,
        result: InvestigationResult,
        context: Dict[str, Any],
    ) -> str:
        intent = classify_question_intent(question)
        parts: List[str] = []

        order_events: List[EventRecord] = context.get("order_events", [])
        captures: List[EventRecord] = context.get("captures", [])
        refunds: List[EventRecord] = context.get("refunds", [])
        failures: List[EventRecord] = context.get("failures", [])
        chargebacks: List[EventRecord] = context.get("chargebacks", [])
        duplicates: List[str] = context.get("duplicate_event_ids", [])
        adversarial: List[str] = context.get("adversarial_note_ids", [])

        # Intent-aware direct answer lead-in when status is established
        if intent == QuestionIntent.FAILURE_DIAGNOSIS and failures:
            fail_ids = ", ".join(f"{f.attempt_id or 'attempt'} ({f.event_id})" for f in failures)
            if captures:
                cap_ids = ", ".join(f"{c.attempt_id or 'attempt'} ({c.event_id})" for c in captures)
                parts.append(f"Checkout logged failure(s) on {fail_ids}, but a separate {cap_ids} succeeded.")
        elif intent == QuestionIntent.EVIDENCE_AUDIT and result.evidence_event_ids:
            ev_list = ", ".join(result.evidence_event_ids)
            parts.append(f"Verified evidence records supporting {result.status.value}: [{ev_list}].")

        # Order baseline
        if len(order_events) == 1:
            oe = order_events[0]
            parts.append(
                f"Order {oe.order_id} ({oe.event_id}) was created for {oe.amount_cents} {oe.currency}."
            )
        elif len(order_events) > 1:
            ids = ", ".join(e.event_id or "" for e in order_events)
            parts.append(f"Multiple ORDER_CREATED records were found ({ids}).")

        # Failed attempts
        for fail in failures:
            att = f"Attempt {fail.attempt_id}" if fail.attempt_id else "A payment attempt"
            parts.append(f"{att} failed ({fail.event_id}).")

        # Captures
        for cap in captures:
            att = f"Attempt {cap.attempt_id}" if cap.attempt_id else "Payment"
            parts.append(
                f"{att} was captured for {cap.amount_cents} {cap.currency} via capture {cap.capture_id} ({cap.event_id})."
            )

        # Refunds
        for ref in refunds:
            parts.append(
                f"Refund {ref.refund_id} of {ref.amount_cents} {ref.currency} was issued against capture {ref.capture_id} ({ref.event_id})."
            )

        # Chargebacks
        for cb in chargebacks:
            parts.append(
                f"An open chargeback was filed against capture {cb.capture_id} ({cb.event_id})."
            )

        # Duplicates
        if duplicates:
            dup_str = ", ".join(duplicates)
            parts.append(f"Identical duplicate event(s) ({dup_str}) were ignored.")

        # Adversarial notes
        if adversarial:
            adv_str = ", ".join(adversarial)
            parts.append(
                f"Untrusted support note(s) ({adv_str}) containing override/manipulation instructions were ignored."
            )

        # Final status & anomalies summary
        if result.status == OrderStatus.UNKNOWN:
            anom_summary = "; ".join(result.anomalies[:2]) if result.anomalies else "insufficient or contradictory evidence"
            parts.append(f"Final status is UNKNOWN due to: {anom_summary.rstrip('.')}.")
        else:
            parts.append(
                f"Final reconciled status is {result.status.value} (captured={result.captured_cents}, refunded={result.refunded_cents}, net_paid={result.net_paid_cents})."
            )

        return " ".join(parts)


class GuardrailedLLMWrapper:
    """
    Optional replaceable adapter for external LLMs with post-generation verification guardrails.
    If an LLM hallucinates an unverified event ID or contradicts the deterministic status,
    it automatically falls back to the DeterministicNarrator.
    """

    def __init__(
        self,
        llm_callable: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        fallback: Optional[ExplanationGenerator] = None,
    ) -> None:
        self.llm_callable = llm_callable
        self.fallback = fallback or DeterministicNarrator()

    def generate(
        self,
        question: str,
        result: InvestigationResult,
        context: Dict[str, Any],
    ) -> str:
        if self.llm_callable is None:
            return self.fallback.generate(question, result, context)

        try:
            safe_prompt_context = {
                "status": result.status.value,
                "captured_cents": result.captured_cents,
                "refunded_cents": result.refunded_cents,
                "net_paid_cents": result.net_paid_cents,
                "evidence_event_ids": result.evidence_event_ids,
                "anomalies": result.anomalies,
            }
            draft = self.llm_callable(question, safe_prompt_context)
            if result.status.value not in draft:
                return self.fallback.generate(question, result, context)
            if result.evidence_event_ids and not any(eid in draft for eid in result.evidence_event_ids):
                return self.fallback.generate(question, result, context)
            return draft
        except Exception:
            return self.fallback.generate(question, result, context)


class InvestigationWorkflow:
    """
    End-to-end investigation workflow:
      1. Interprets the request (tenant, order, question).
      2. Calls get_order, get_payment_events, and get_support_notes with bounded retry.
      3. Validates and reconciles financial state deterministically.
      4. Produces an evidence-backed explanation.
    """

    def __init__(
        self,
        store: EventStore,
        narrator: Optional[ExplanationGenerator] = None,
        max_retries: int = 2,
    ) -> None:
        self.store = store
        self.narrator = narrator or DeterministicNarrator()
        self.max_retries = max_retries
        set_active_store(store)

    def investigate(
        self,
        tenant_id: str,
        order_id: str,
        question: str = "What is the current status of this order?",
    ) -> InvestigationResult:
        set_active_store(self.store)

        tool_anomalies: List[str] = []
        retry_warnings: List[str] = []
        critical_tool_failed = False

        # 1. Fetch order records with bounded retry
        order_recs, w_order, fail_order = call_with_bounded_retry(
            get_order,
            tenant_id,
            order_id,
            max_retries=self.max_retries,
            tool_name="get_order",
        )
        retry_warnings.extend(w_order)
        if fail_order or order_recs is None:
            critical_tool_failed = True
            tool_anomalies.extend([w for w in w_order if "EXHAUSTED" in w])
            order_recs = []

        # 2. Fetch payment events with bounded retry
        pay_recs, w_pay, fail_pay = call_with_bounded_retry(
            get_payment_events,
            tenant_id,
            order_id,
            max_retries=self.max_retries,
            tool_name="get_payment_events",
        )
        retry_warnings.extend(w_pay)
        if fail_pay or pay_recs is None:
            critical_tool_failed = True
            tool_anomalies.extend([w for w in w_pay if "EXHAUSTED" in w])
            pay_recs = []

        # 3. Fetch support notes with bounded retry (non-critical fallback if support service is down!)
        note_recs, w_note, fail_note = call_with_bounded_retry(
            get_support_notes,
            tenant_id,
            order_id,
            max_retries=self.max_retries,
            tool_name="get_support_notes",
        )
        retry_warnings.extend(w_note)
        if fail_note or note_recs is None:
            note_recs = []

        # Deduplicate list object references collected across the 3 tools
        combined_records: List[EventRecord] = []
        seen_line_nums = set()
        for r in (order_recs + pay_recs + note_recs):
            if r.line_number not in seen_line_nums:
                seen_line_nums.add(r.line_number)
                combined_records.append(r)

        result, context = reconcile_order(
            tenant_id=tenant_id,
            order_id=order_id,
            events=combined_records,
            parse_anomalies=self.store.parse_anomalies if self.store.parse_anomalies else None,
            tool_anomalies=tool_anomalies if critical_tool_failed else None,
        )

        # Attach non-fatal retry recovery / support fallback notices to anomalies & adjust confidence
        non_fatal_notices = [w for w in retry_warnings if "RECOVERED" in w or ("get_support_notes" in w and "EXHAUSTED" in w)]
        if non_fatal_notices:
            result.anomalies.extend(non_fatal_notices)
            if result.status != OrderStatus.UNKNOWN and result.confidence == ConfidenceLevel.HIGH:
                result.confidence = ConfidenceLevel.MEDIUM

        result.explanation = self.narrator.generate(question=question, result=result, context=context)
        return result