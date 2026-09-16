import json
import os

import pytest

from document_wide_chunk_store import ChunkPlanStore


OWNER, SID, RUN, PLAN, FILE = 'owner@example.com', 'scan', 'run', 'plan', 'deck.pptx'
SHA, SNAPSHOT, SOURCE = 'a' * 64, 'assessment-snapshot', 'remediation-source'


def seed(store, monkeypatch):
    with store._db.cursor() as cur:
        store._db.execute(cur, "INSERT INTO ai_spending_budgets(owner_id,run_id,cap_units,currency) VALUES(%s,%s,1,'USD')", (OWNER,RUN))
        store._db.execute(cur, "INSERT INTO ai_spending_run_policies(owner_id,run_id,scan_id,policy_json) VALUES(%s,%s,%s,'{}')", (OWNER,RUN,SID))
        store._db.execute(cur, """INSERT INTO stage_executions
          (execution_id,workflow_id,workflow_revision,scan_id,owner_email,stage,input_snapshot_id,
           request_fingerprint,state,created_at,updated_at) VALUES(%s,'workflow',1,%s,%s,'remediate',%s,'request','accepted','now','now')""",
          (RUN,SID,OWNER,SNAPSHOT))
    monkeypatch.setattr(store, 'remediation_source_revision', lambda _sid: SOURCE)
    monkeypatch.setattr(store, 'get_file_record', lambda _sid, _file: {'corrected_sha256': SHA})


def plan(owner=OWNER, scan=SID, source=SOURCE):
    return {'owner_id':owner,'run_id':RUN,'plan_id':PLAN,'scan_id':scan,'file':FILE,
            'source_sha256':SHA,'assessment_snapshot_id':SNAPSHOT,
            'remediation_source_revision':source,'format':'pptx',
            'eligible_finding_ids':['f1','f2'],'created_at':'now'}


def chunks():
    return [{'chunk_id':'c1','boundary':{'first_slide':1,'last_slide':1},'finding_ids':['f1']},
            {'chunk_id':'c2','boundary':{'first_slide':2,'last_slide':2},'finding_ids':['f2']}]


def test_plan_is_immutable_owner_scoped_and_exactly_partitions_findings(isolated_store, monkeypatch):
    seed(isolated_store, monkeypatch)
    ledger = ChunkPlanStore(isolated_store)
    ledger.create(plan(), chunks())
    ledger.create(plan(), chunks())
    changed = chunks(); changed[1]['finding_ids'] = ['f1']
    with pytest.raises(ValueError): ledger.create(plan(), changed)
    with pytest.raises(ValueError, match='authority'):
        ledger.create(plan(owner='other@example.com'), chunks())
    with pytest.raises(ValueError, match='stale'):
        ledger.create(plan(source='older-source'), chunks())


def test_partial_or_stale_rows_are_never_publish_authority(isolated_store, monkeypatch):
    seed(isolated_store, monkeypatch)
    ledger = ChunkPlanStore(isolated_store); ledger.create(plan(), chunks())
    assert ledger.complete_receipts(OWNER,RUN,PLAN,source_sha256=SHA,
        assessment_snapshot_id=SNAPSHOT,remediation_source_revision=SOURCE) is None
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE document_wide_chunk_plans SET state='complete' WHERE plan_id=%s", (PLAN,))
        isolated_store._db.execute(cur, "UPDATE document_wide_chunks SET state='validated',receipt_json=%s WHERE chunk_id='c1'", (json.dumps({'chunk_id':'c1'}),))
    assert ledger.complete_receipts(OWNER,RUN,PLAN,source_sha256=SHA,
        assessment_snapshot_id=SNAPSHOT,remediation_source_revision=SOURCE) is None
    assert ledger.complete_receipts(OWNER,RUN,PLAN,source_sha256='b'*64,
        assessment_snapshot_id=SNAPSHOT,remediation_source_revision=SOURCE) is None


def test_global_reset_deletes_chunk_children_before_plans(isolated_store, monkeypatch):
    seed(isolated_store, monkeypatch); ChunkPlanStore(isolated_store).create(plan(), chunks())
    isolated_store.reset_analytics()
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'SELECT COUNT(*) AS n FROM document_wide_chunks')
        assert isolated_store._db.fetchone(cur)['n'] == 0
        isolated_store._db.execute(cur, 'SELECT COUNT(*) AS n FROM document_wide_chunk_plans')
        assert isolated_store._db.fetchone(cur)['n'] == 0


@pytest.mark.skipif(not os.getenv('ACP_BUDGET_TEST_PG_URL'), reason='disposable Postgres not configured')
def test_postgres_immutability_and_incomplete_rows_fail_closed(monkeypatch):
    from conftest import require_disposable_postgres
    from urllib.parse import urlparse
    import store as store_mod
    url = os.environ['ACP_BUDGET_TEST_PG_URL']; parsed = urlparse(url)
    require_disposable_postgres(url)
    assert parsed.hostname in ('127.0.0.1','localhost') and parsed.path == '/acp_budget_test'
    monkeypatch.setattr(store_mod, '_DATABASE_URL', url)
    pg = store_mod.Store()
    with pg._db.cursor() as cur:
        require_disposable_postgres(url, conn=cur.connection)
        pg._db.execute(cur, 'TRUNCATE document_wide_chunks,document_wide_chunk_plans,ai_spending_attempts,ai_spending_run_policies,ai_spending_budgets,stage_executions CASCADE')
    seed(pg, monkeypatch); ledger = ChunkPlanStore(pg)
    ledger.create(plan(), chunks()); ledger.create(plan(), chunks())
    assert ledger.complete_receipts(OWNER,RUN,PLAN,source_sha256=SHA,
        assessment_snapshot_id=SNAPSHOT,remediation_source_revision=SOURCE) is None
    if pg._db._pool: pg._db._pool.closeall()
