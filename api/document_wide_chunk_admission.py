"""Disabled atomic admission for deterministic document-wide chunk plans.

This module reserves budget and stores a plan.  It cannot dispatch provider
work, persist model output, or make a plan publishable.
"""
from __future__ import annotations

from ai_spending_budget import BudgetLedger
from document_wide_chunk_store import ChunkPlanStore


class ChunkPlanAdmission:
    def __init__(self, store):
        self.store = store
        self.ledger = BudgetLedger(store._db)
        self.plans = ChunkPlanStore(store)

    def admit(self, plan: dict, chunks: list[dict]):
        if not isinstance(chunks, (list, tuple)) or not chunks:
            raise ValueError('chunks must be a nonempty ordered list')
        plan_id = plan.get('plan_id') if isinstance(plan, dict) else None
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError('plan_id must be a nonblank string')
        attempts = []
        bindings = []
        seen_chunks = set()
        for ordinal, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError('each chunk must be a mapping')
            chunk_id = chunk.get('chunk_id')
            attempt = {key: chunk.get(key) for key in
                       ('attempt_id', 'max_cost_units', 'pricing_ref')}
            if not isinstance(chunk_id, str) or not chunk_id.strip() or chunk_id in seen_chunks:
                raise ValueError('chunk IDs must be unique nonblank strings')
            seen_chunks.add(chunk_id)
            if attempt['attempt_id'] != f'{plan_id}:chunk:{ordinal}':
                raise ValueError('attempt ID must be derived from plan ID and chunk ordinal')
            attempts.append(attempt)
            bindings.append({'ordinal': ordinal, 'chunk_id': chunk_id, **attempt})

        normalized = self.ledger._normalize_many(attempts)
        admitted_plan = {**plan, 'budget_attempts': bindings}
        owner, run_id = admitted_plan.get('owner_id'), admitted_plan.get('run_id')
        # _locked owns the outer adapter transaction.  Plan rows and every
        # reservation become visible together or roll back together.
        with self.ledger._locked(owner, run_id) as (cur, budget):
            self.plans._create(cur, admitted_plan, chunks)
            rows = self.ledger._reserve_many_locked(cur, budget, normalized)
            if any(row['state'] != 'reserved' for row in rows):
                raise ValueError('admitted plan requires every bound attempt to remain reserved')
        return {'plan_id': plan_id,
                'attempt_ids': [row['attempt_id'] for row in rows],
                'state': 'admitted'}

    def cancel_reserved(self, owner_id: str, run_id: str, plan_id: str):
        """Cancel a disabled plan and release only work never dispatched.

        A later authority/source invalidation linearizes after admission.  Its
        cancellation path can call this operation; dispatched or uncertain
        attempts are deliberately retained for charge reconciliation.
        """
        with self.ledger._locked(owner_id, run_id) as (cur, budget):
            lock = " FOR UPDATE" if hasattr(self.store._db, "_url") else ""
            self.store._db.execute(cur, "SELECT state,plan_json FROM document_wide_chunk_plans "
                                        "WHERE owner_id=%s AND run_id=%s AND plan_id=%s" + lock,
                                   (owner_id, run_id, plan_id))
            saved = self.store._db.fetchone(cur)
            if not saved:
                return {'plan_id': plan_id, 'released_attempt_ids': []}
            import json
            bindings = json.loads(saved['plan_json']).get('budget_attempts') or []
            released = []
            for binding in bindings:
                attempt_id = binding.get('attempt_id') if isinstance(binding, dict) else None
                row = self.ledger._attempt(cur, owner_id, run_id, attempt_id)
                if row is not None and row['state'] == 'reserved':
                    self.ledger._update(cur, row, 'released', None)
                    released.append(attempt_id)
            self.store._db.execute(cur, "UPDATE document_wide_chunk_plans SET state='cancelled' "
                                        "WHERE owner_id=%s AND run_id=%s AND plan_id=%s "
                                        "AND state='planned'",
                                   (owner_id, run_id, plan_id))
        return {'plan_id': plan_id, 'released_attempt_ids': released}
