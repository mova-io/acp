"""Durable admission ledger for bounded metered provider charges (not infrastructure).

See docs/ai-spending-budget-contract.md before integrating a provider. No prices or
provider calls live here. All amounts are integer millionths of the budget currency.
"""
from __future__ import annotations

from contextlib import contextmanager
import re


class BudgetError(RuntimeError):
    """Admission or a state transition was refused; never dispatch on this error."""


class BudgetExceeded(BudgetError):
    pass


class UnknownPricing(BudgetError):
    pass


class AttemptConflict(BudgetError):
    pass


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ai_spending_budgets (
        owner_id TEXT NOT NULL, run_id TEXT NOT NULL, cap_units BIGINT NOT NULL,
        currency TEXT NOT NULL, PRIMARY KEY (owner_id, run_id),
        CHECK (cap_units >= 0))""",
    """CREATE TABLE IF NOT EXISTS ai_spending_attempts (
        owner_id TEXT NOT NULL, run_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
        max_cost_units BIGINT NOT NULL, pricing_ref TEXT NOT NULL,
        state TEXT NOT NULL, actual_cost_units BIGINT,
        PRIMARY KEY (owner_id, run_id, attempt_id),
        FOREIGN KEY (owner_id, run_id) REFERENCES ai_spending_budgets(owner_id, run_id),
        CHECK (max_cost_units >= 0), CHECK (actual_cost_units >= 0),
        CHECK (state IN ('reserved','dispatched','uncertain','settled','released','breached')))
    """,
)


def _units(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("amount must be a nonnegative signed-64-bit integer")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("identifier must be a nonblank string of at most 512 characters")
    return value


class BudgetLedger:
    """Accept an existing Store._db adapter; no global Store construction or fallback.

    Every operation uses one adapter cursor/commit. A no-op UPDATE locks the budget
    before any read, serializing decisions across processes on SQLite and Postgres.
    Ambient Store transactions are rejected: dispatch permission must be committed.
    """

    def __init__(self, db_adapter):
        self.db = db_adapter

    def _standalone(self):
        active = getattr(self.db, "_transaction_conn", None)
        if active is not None and active.get() is not None:
            raise BudgetError("ledger must run outside an ambient transaction")

    def init_schema(self):
        """Explicit migration seam; call during startup, never during dispatch."""
        self._standalone()
        with self.db.cursor() as cur:
            for statement in SCHEMA:
                self.db.execute(cur, statement)

    def create_budget(self, owner_id, run_id, cap_units, currency="USD"):
        _identifier(owner_id)
        _identifier(run_id)
        _units(cap_units)
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be an uppercase three-letter code")
        self._standalone()
        with self.db.cursor() as cur:
            self.db.execute(cur, """INSERT INTO ai_spending_budgets
                (owner_id,run_id,cap_units,currency) VALUES (%s,%s,%s,%s)
                ON CONFLICT (owner_id,run_id) DO NOTHING""",
                (owner_id, run_id, cap_units, currency))
            self.db.execute(cur, """SELECT * FROM ai_spending_budgets
                WHERE owner_id=%s AND run_id=%s""", (owner_id, run_id))
            budget = self.db.fetchone(cur)
            if (budget["cap_units"], budget["currency"]) != (cap_units, currency):
                raise AttemptConflict("budget cap and currency are immutable")
        return budget

    @contextmanager
    def _locked(self, owner_id, run_id):
        _identifier(owner_id)
        _identifier(run_id)
        self._standalone()
        # Keep the lock and every nested admission write on one connection and
        # one commit boundary.  Ordinary ledger callers still commit exactly
        # once; disabled compound admissions may safely add their own rows
        # before this context exits.
        with self.db.transaction(), self.db.cursor() as cur:
            self.db.execute(cur, """UPDATE ai_spending_budgets SET cap_units=cap_units
                WHERE owner_id=%s AND run_id=%s""", (owner_id, run_id))
            self.db.execute(cur, """SELECT * FROM ai_spending_budgets
                WHERE owner_id=%s AND run_id=%s""", (owner_id, run_id))
            budget = self.db.fetchone(cur)
            if budget is None:
                raise BudgetError("budget does not exist for owner and run")
            yield cur, budget

    def _attempt(self, cur, owner_id, run_id, attempt_id):
        _identifier(attempt_id)
        self.db.execute(cur, """SELECT * FROM ai_spending_attempts
            WHERE owner_id=%s AND run_id=%s AND attempt_id=%s""",
            (owner_id, run_id, attempt_id))
        return self.db.fetchone(cur)

    def _snapshot(self, cur, budget):
        self.db.execute(cur, """SELECT * FROM ai_spending_attempts
            WHERE owner_id=%s AND run_id=%s""", (budget["owner_id"], budget["run_id"]))
        rows = self.db.fetchall(cur)
        spent = sum(r["actual_cost_units"] for r in rows if r["state"] in ("settled", "breached"))
        held = sum(r["max_cost_units"] for r in rows
                   if r["state"] in ("reserved", "dispatched", "uncertain"))
        blocked = any(r["state"] in ("uncertain", "breached") for r in rows)
        return {**budget, "spent_units": spent, "held_units": held,
                "available_units": max(0, budget["cap_units"] - spent - held),
                "blocked": blocked, "cost_scope": "metered_provider"}

    def snapshot(self, owner_id, run_id):
        with self._locked(owner_id, run_id) as (cur, budget):
            result = self._snapshot(cur, budget)
        return result

    def reserve(self, owner_id, run_id, attempt_id, max_cost_units, pricing_ref):
        if max_cost_units is None or not pricing_ref:
            raise UnknownPricing("a verified maximum charge and pricing reference are required")
        _units(max_cost_units)
        _identifier(pricing_ref)
        _identifier(attempt_id)
        with self._locked(owner_id, run_id) as (cur, budget):
            result = self._attempt(cur, owner_id, run_id, attempt_id)
            if result is not None:
                if (result["max_cost_units"], result["pricing_ref"]) != (max_cost_units, pricing_ref):
                    raise AttemptConflict("attempt ID reused with a different bound or price")
            else:
                snapshot = self._snapshot(cur, budget)
                if snapshot["blocked"]:
                    raise BudgetError("unresolved charge or breached bound blocks admission")
                if max_cost_units > snapshot["available_units"]:
                    raise BudgetExceeded("maximum provider exposure exceeds remaining budget")
                self.db.execute(cur, """INSERT INTO ai_spending_attempts
                    (owner_id,run_id,attempt_id,max_cost_units,pricing_ref,state)
                    VALUES (%s,%s,%s,%s,%s,'reserved')""",
                    (owner_id, run_id, attempt_id, max_cost_units, pricing_ref))
                result = self._attempt(cur, owner_id, run_id, attempt_id)
        return result

    def _normalize_many(self, attempts):
        """Atomically reserve one bounded charge per attempt.

        Exact full-batch replay returns the durable rows in request order. A
        partially replayed batch may be completed only while every existing
        row is still reserved; once any member has advanced, the batch cannot
        authorize new reservations.
        """
        if not isinstance(attempts, (list, tuple)) or not attempts:
            raise ValueError("attempts must be a nonempty list or tuple")
        normalized = []
        seen = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError("each attempt must be a mapping")
            attempt_id = attempt.get("attempt_id")
            max_cost_units = attempt.get("max_cost_units")
            pricing_ref = attempt.get("pricing_ref")
            if max_cost_units is None or not pricing_ref:
                raise UnknownPricing("a verified maximum charge and pricing reference are required")
            _identifier(attempt_id)
            _units(max_cost_units)
            _identifier(pricing_ref)
            if attempt_id in seen:
                raise AttemptConflict("duplicate attempt ID in reservation batch")
            seen.add(attempt_id)
            normalized.append((attempt_id, max_cost_units, pricing_ref))

        return normalized

    def _reserve_many_locked(self, cur, budget, normalized):
        owner_id, run_id = budget["owner_id"], budget["run_id"]
        rows = []
        new = []
        for attempt_id, max_cost_units, pricing_ref in normalized:
            row = self._attempt(cur, owner_id, run_id, attempt_id)
            if row is not None:
                if (row["max_cost_units"], row["pricing_ref"]) != (max_cost_units, pricing_ref):
                    raise AttemptConflict("attempt ID reused with a different bound or price")
                rows.append(row)
            else:
                rows.append(None)
                new.append((attempt_id, max_cost_units, pricing_ref))

        if new:
            existing = [row for row in rows if row is not None]
            if any(row["state"] != "reserved" for row in existing):
                raise AttemptConflict("advanced attempt cannot admit new batch reservations")
            snapshot = self._snapshot(cur, budget)
            if snapshot["blocked"]:
                raise BudgetError("unresolved charge or breached bound blocks admission")
            exposure = sum(max_cost_units for _, max_cost_units, _ in new)
            if exposure > snapshot["available_units"]:
                raise BudgetExceeded("maximum provider exposure exceeds remaining budget")
            for attempt_id, max_cost_units, pricing_ref in new:
                self.db.execute(cur, """INSERT INTO ai_spending_attempts
                    (owner_id,run_id,attempt_id,max_cost_units,pricing_ref,state)
                    VALUES (%s,%s,%s,%s,%s,'reserved')""",
                    (owner_id, run_id, attempt_id, max_cost_units, pricing_ref))
            rows = [self._attempt(cur, owner_id, run_id, attempt_id)
                    for attempt_id, _, _ in normalized]
        return rows

    def reserve_many(self, owner_id, run_id, attempts):
        """Atomically reserve one bounded charge per attempt.

        Exact full-batch replay returns the durable rows in request order. A
        partially replayed batch may be completed only while every existing
        row is still reserved; once any member has advanced, the batch cannot
        authorize new reservations.
        """
        normalized = self._normalize_many(attempts)
        with self._locked(owner_id, run_id) as (cur, budget):
            rows = self._reserve_many_locked(cur, budget, normalized)
        return rows

    def claim_dispatch(self, owner_id, run_id, attempt_id):
        """True once, after durable commit. False is NEVER permission to send again."""
        with self._locked(owner_id, run_id) as (cur, budget):
            row = self._attempt(cur, owner_id, run_id, attempt_id)
            if row is None:
                raise BudgetError("attempt has no reservation")
            if row["state"] != "reserved":
                return False
            if self._snapshot(cur, budget)["blocked"]:
                raise BudgetError("unresolved charge or breached bound blocks dispatch")
            self._update(cur, row, "dispatched", None)
        return True

    def _update(self, cur, row, state, actual):
        self.db.execute(cur, """UPDATE ai_spending_attempts SET state=%s, actual_cost_units=%s
            WHERE owner_id=%s AND run_id=%s AND attempt_id=%s""",
            (state, actual, row["owner_id"], row["run_id"], row["attempt_id"]))
        return {**row, "state": state, "actual_cost_units": actual}

    def _finish(self, owner_id, run_id, attempt_id, state, actual=None, confirmed=False):
        with self._locked(owner_id, run_id) as (cur, _):
            row = self._attempt(cur, owner_id, run_id, attempt_id)
            if row is None:
                raise BudgetError("attempt does not exist")
            if state == "settled" and actual > row["max_cost_units"]:
                state = "breached"  # Record reality and block; raising would roll back evidence.
            if row["state"] == state and row["actual_cost_units"] == actual:
                return row
            allowed = row["state"] in ("dispatched", "uncertain")
            if state == "released":
                allowed = row["state"] == "reserved" or (allowed and confirmed)
            if not allowed:
                raise AttemptConflict("invalid attempt transition or conflicting reconciliation")
            result = self._update(cur, row, state, actual)
        return result

    def settle(self, owner_id, run_id, attempt_id, actual_cost_units):
        """Reconcile an authoritative final charge; incomplete usage is uncertain."""
        _units(actual_cost_units)
        return self._finish(owner_id, run_id, attempt_id, "settled", actual_cost_units)

    def release(self, owner_id, run_id, attempt_id, *, confirmed_not_charged=False):
        """After dispatch, requires authoritative evidence of no charge, not a timeout."""
        if type(confirmed_not_charged) is not bool:
            raise ValueError("confirmed_not_charged must be boolean")
        return self._finish(owner_id, run_id, attempt_id, "released",
                            confirmed=confirmed_not_charged)

    def mark_uncertain(self, owner_id, run_id, attempt_id):
        """Retain the full hold indefinitely; never expire potentially billable attempts."""
        return self._finish(owner_id, run_id, attempt_id, "uncertain")
