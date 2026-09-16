"""Disabled durable storage for document-wide chunk plans and validated receipts."""
from __future__ import annotations

import json


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS document_wide_chunk_plans (
      owner_id TEXT NOT NULL, run_id TEXT NOT NULL, plan_id TEXT NOT NULL,
      scan_id TEXT NOT NULL, file TEXT NOT NULL, source_sha256 TEXT NOT NULL,
      assessment_snapshot_id TEXT NOT NULL, remediation_source_revision TEXT NOT NULL,
      format TEXT NOT NULL, eligible_finding_ids_json TEXT NOT NULL,
      plan_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', created_at TEXT NOT NULL,
      PRIMARY KEY(owner_id,run_id,plan_id),
      FOREIGN KEY(owner_id,run_id) REFERENCES ai_spending_run_policies(owner_id,run_id),
      CHECK(state IN ('planned','cancelled','complete')))
    """,
    """CREATE TABLE IF NOT EXISTS document_wide_chunks (
      owner_id TEXT NOT NULL, run_id TEXT NOT NULL, plan_id TEXT NOT NULL,
      ordinal INT NOT NULL, chunk_id TEXT NOT NULL, boundary_json TEXT NOT NULL,
      finding_ids_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
      receipt_json TEXT, validated_at TEXT,
      PRIMARY KEY(owner_id,run_id,plan_id,ordinal),
      UNIQUE(owner_id,run_id,chunk_id),
      FOREIGN KEY(owner_id,run_id,plan_id)
        REFERENCES document_wide_chunk_plans(owner_id,run_id,plan_id),
      CHECK(ordinal >= 0), CHECK(state IN ('planned','validated')))
    """,
    "CREATE INDEX IF NOT EXISTS idx_document_wide_chunk_plan_scan ON document_wide_chunk_plans(scan_id,file)",
)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ChunkPlanStore:
    def __init__(self, store):
        self.store, self.db = store, store._db

    def _authority(self, cur, owner, run_id, scan_id, snapshot, *, allowed_states):
        self.db.execute(cur, """SELECT e.owner_email,e.scan_id,e.input_snapshot_id,e.stage,
          e.is_current,e.cancel_requested_at,e.state,p.scan_id AS policy_scan
          FROM stage_executions e JOIN ai_spending_run_policies p
            ON p.owner_id=e.owner_email AND p.run_id=e.execution_id
          WHERE e.execution_id=%s""", (run_id,))
        row = self.db.fetchone(cur)
        if (not row or
            (row['owner_email'], row['scan_id'], row['policy_scan'], row['input_snapshot_id'], row['stage']) != (owner, scan_id, scan_id, snapshot, 'remediate') or
            row['is_current'] != 1 or row.get('cancel_requested_at') or
            row['state'] not in allowed_states):
            raise ValueError('chunk plan owner/run/scan authority mismatch')

    def create(self, plan: dict, chunks: list[dict]):
        required = ('owner_id','run_id','plan_id','scan_id','file','source_sha256',
                    'assessment_snapshot_id','remediation_source_revision','format',
                    'eligible_finding_ids','created_at')
        if any(not plan.get(key) for key in required):
            raise ValueError('incomplete chunk plan')
        eligible = tuple(plan['eligible_finding_ids'])
        found = tuple(fid for chunk in chunks for fid in chunk.get('finding_ids', ()))
        if len(set(eligible)) != len(eligible) or set(found) != set(eligible) or len(found) != len(set(found)):
            raise ValueError('chunk plan must exactly partition eligible findings')
        with self.store.transaction(), self.db.cursor() as cur:
            self._authority(cur, plan['owner_id'], plan['run_id'], plan['scan_id'],
                            plan['assessment_snapshot_id'],
                            allowed_states=('accepted','queued','processing'))
            if self.store.remediation_source_revision(plan['scan_id']) != plan['remediation_source_revision']:
                raise ValueError('stale remediation source revision')
            record = self.store.get_file_record(plan['scan_id'], plan['file']) or {}
            if record.get('corrected_sha256') != plan['source_sha256']:
                raise ValueError('stale corrected source')
            values = tuple(plan[key] for key in required[:9]) + (_encoded(eligible), _encoded(plan), plan['created_at'])
            self.db.execute(cur, """INSERT INTO document_wide_chunk_plans
              (owner_id,run_id,plan_id,scan_id,file,source_sha256,assessment_snapshot_id,
               remediation_source_revision,format,eligible_finding_ids_json,plan_json,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", values)
            self.db.execute(cur, "SELECT * FROM document_wide_chunk_plans WHERE owner_id=%s AND run_id=%s AND plan_id=%s",
                            (plan['owner_id'],plan['run_id'],plan['plan_id']))
            saved = self.db.fetchone(cur)
            if not saved or any(saved[key] != plan[key] for key in required[:9]) or saved['eligible_finding_ids_json'] != _encoded(eligible) or saved['plan_json'] != _encoded(plan):
                raise ValueError('chunk plan identity is immutable')
            for ordinal, chunk in enumerate(chunks):
                values = (plan['owner_id'],plan['run_id'],plan['plan_id'],ordinal,chunk['chunk_id'],
                          _encoded(chunk['boundary']),_encoded(tuple(chunk['finding_ids'])))
                self.db.execute(cur, """INSERT INTO document_wide_chunks
                  (owner_id,run_id,plan_id,ordinal,chunk_id,boundary_json,finding_ids_json)
                  VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", values)
                self.db.execute(cur, """SELECT chunk_id,boundary_json,finding_ids_json FROM document_wide_chunks
                  WHERE owner_id=%s AND run_id=%s AND plan_id=%s AND ordinal=%s""", values[:4])
                if self.db.fetchone(cur) != {'chunk_id': values[4], 'boundary_json': values[5], 'finding_ids_json': values[6]}:
                    raise ValueError('chunk identity is immutable')

    def complete_receipts(self, owner, run_id, plan_id, *, source_sha256,
                          assessment_snapshot_id, remediation_source_revision):
        """Read publish authority only for an exact current and complete validated set."""
        with self.store.transaction(), self.db.cursor() as cur:
            self.db.execute(cur, "SELECT * FROM document_wide_chunk_plans WHERE owner_id=%s AND run_id=%s AND plan_id=%s",
                            (owner,run_id,plan_id))
            plan = self.db.fetchone(cur)
            if not plan or plan['state'] != 'complete' or (plan['source_sha256'],plan['assessment_snapshot_id'],plan['remediation_source_revision']) != (source_sha256,assessment_snapshot_id,remediation_source_revision):
                return None
            try:
                self._authority(cur, owner, run_id, plan['scan_id'],
                                plan['assessment_snapshot_id'],
                                allowed_states=('accepted','queued','processing',
                                                'processing_complete','reconciling'))
            except ValueError:
                return None
            self.db.execute(cur, """SELECT * FROM document_wide_chunks WHERE owner_id=%s AND run_id=%s AND plan_id=%s ORDER BY ordinal""",
                            (owner,run_id,plan_id))
            rows = self.db.fetchall(cur)
            if (self.store.remediation_source_revision(plan['scan_id']) != plan['remediation_source_revision'] or
                    (self.store.get_file_record(plan['scan_id'], plan['file']) or {}).get('corrected_sha256') != plan['source_sha256']):
                return None
            eligible = set(json.loads(plan['eligible_finding_ids_json']))
            covered = [fid for row in rows for fid in json.loads(row['finding_ids_json'])]
            if not rows or any(row['state'] != 'validated' or not row['receipt_json'] for row in rows) or len(covered) != len(set(covered)) or set(covered) != eligible:
                return None
            return [json.loads(row['receipt_json']) for row in rows]
