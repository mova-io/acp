"""Durable, explicitly authorized automatic Release intent and atomic continuation jobs."""
from datetime import datetime, timedelta, timezone
import json

ACTIVE = frozenset({'active', 'waiting', 'processing', 'blocked'})
TERMINAL = frozenset({'completed', 'failed', 'stopped'})
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS automatic_release_authorizations (
        id TEXT PRIMARY KEY,owner_email TEXT NOT NULL,scan_id TEXT NOT NULL,run_id TEXT NOT NULL,
        request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,intent TEXT NOT NULL,
        progress TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,stopped_at TEXT,
        UNIQUE(owner_email,scan_id,request_id))''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS idx_automatic_release_active
       ON automatic_release_authorizations(owner_email,scan_id,run_id)
       WHERE status IN ('active','waiting','processing','blocked')''',
    '''CREATE INDEX IF NOT EXISTS idx_automatic_release_owner
       ON automatic_release_authorizations(owner_email,scan_id,created_at)''',
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _decode(row):
    if row:
        row = dict(row)
        row['intent'] = json.loads(row['intent'])
        row['progress'] = json.loads(row['progress'])
    return row


def get(store, authorization_id, owner, *, lock=False):
    with store._db.cursor() as cur:
        if lock:
            # A no-op write locks the row on Postgres and serializes SQLite writers.
            # Callers retain the lock by surrounding this operation in a transaction.
            store._db.execute(cur, 'UPDATE automatic_release_authorizations SET revision=revision WHERE id=%s AND owner_email=%s', (authorization_id, owner))
        store._db.execute(cur, 'SELECT * FROM automatic_release_authorizations WHERE id=%s AND owner_email=%s', (authorization_id, owner))
        return _decode(store._db.fetchone(cur))


def _schedule(store, row, delay):
    store.enqueue_job('release_continue', {'mode':'automatic', 'authorization_id':row['id'],
        'owner':row['owner_email'], 'revision':row['revision']}, scan_id=row['scan_id'],
        run_after=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat())


def _expire_active_for_run(store, owner, scan, run):
    """Release stale active-index slots during a new explicit authorization."""
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT id,intent FROM automatic_release_authorizations "
            "WHERE owner_email=%s AND scan_id=%s AND run_id=%s "
            "AND status IN ('active','waiting','processing','blocked')", (owner, scan, run))
        rows = store._db.fetchall(cur)
        now = datetime.now(timezone.utc)
        expired = []
        for row in rows:
            try:
                intent = json.loads(row['intent']) if isinstance(row['intent'], str) else row['intent']
                if now >= datetime.fromisoformat(intent['expires_at']):
                    expired.append(row['id'])
            except (KeyError, TypeError, ValueError):
                continue
        for authorization_id in expired:
            store._db.execute(cur, "UPDATE automatic_release_authorizations "
                "SET status='failed',revision=revision+1,updated_at=%s WHERE id=%s "
                "AND status IN ('active','waiting','processing','blocked')",
                (store._now(), authorization_id))


def create(store, owner, scan, run, request_id, intent):
    if any(not isinstance(v,str) or not v.strip() or len(v)>512 for v in (owner,scan,run,request_id)):
        raise ValueError('Bounded owner, scan, run and request identities are required')
    if not isinstance(intent,dict):
        raise ValueError('An exact authorization intent is required')
    encoded=_json(intent)
    fingerprint=store.canonical_request_fingerprint({'run_id':run,'intent':intent})
    identity=store.canonical_request_fingerprint([owner,scan,request_id])
    with store.transaction():
        with store._db.cursor() as cur:
            # Serialize admission for this owner/scan, including concurrent new request IDs.
            store._db.execute(cur, 'UPDATE scan_runs SET owner_email=owner_email WHERE id=%s AND owner_email=%s', (scan,owner))
            if cur.rowcount != 1:
                raise ValueError('Scan not found in this owner scope')
            store._db.execute(cur, 'SELECT execution_id FROM stage_executions WHERE execution_id=%s AND owner_email=%s AND scan_id=%s AND stage=%s', (run,owner,scan,'remediate'))
            if not store._db.fetchone(cur):
                raise ValueError('Remediation run not found in this owner scope')
        _expire_active_for_run(store, owner, scan, run)
        existing=get(store,identity,owner)
        if existing:
            if existing['fingerprint'] != fingerprint or existing['intent'] != intent or existing['run_id'] != run:
                raise ValueError('Authorization request intent is immutable')
            return existing
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT id FROM automatic_release_authorizations WHERE owner_email=%s AND scan_id=%s AND run_id=%s AND status IN ('active','waiting','processing','blocked')", (owner,scan,run))
            if store._db.fetchone(cur):
                raise ValueError('An automatic Release authorization is already active for this run')
            now=store._now()
            store._db.execute(cur, '''INSERT INTO automatic_release_authorizations
                (id,owner_email,scan_id,run_id,request_id,fingerprint,intent,progress,status,revision,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active',0,%s,%s)''',
                (identity,owner,scan,run,request_id,fingerprint,encoded,'{"_tick_revision":0}',now,now))
        row=get(store,identity,owner)
        _schedule(store,row,0)
        return row


def latest(store, scan, owner, run_id=None):
    with store._db.cursor() as cur:
        sql='SELECT * FROM automatic_release_authorizations WHERE scan_id=%s AND owner_email=%s'
        params=(scan,owner)
        if run_id is not None:
            sql+=' AND run_id=%s';params+=(run_id,)
        store._db.execute(cur, sql+' ORDER BY created_at DESC,id DESC LIMIT 1', params)
        return _decode(store._db.fetchone(cur))


def save(store, row, *, status, progress, schedule=False, delay=20):
    if status not in ACTIVE|TERMINAL or not isinstance(progress,dict):
        raise ValueError('Valid authorization status and progress are required')
    if schedule and status not in ACTIVE:
        raise ValueError('Terminal authorizations cannot schedule work')
    if not isinstance(delay,(int,float)) or isinstance(delay,bool) or not 0<=delay<=86400:
        raise ValueError('Invalid continuation delay')
    with store.transaction():
        progress=dict(progress)
        if schedule:
            progress['_tick_revision']=row['revision']+1
        current=get(store,row['id'],row['owner_email'],lock=True)
        if not current or current['revision'] != row['revision']:
            raise ValueError('Authorization changed; refresh its status')
        if current['status'] in TERMINAL:
            raise ValueError('Authorization is terminal and cannot be changed')
        if not schedule and '_tick_revision' in current['progress']:
            progress['_tick_revision']=current['progress']['_tick_revision']
        with store._db.cursor() as cur:
            now=store._now()
            store._db.execute(cur, '''UPDATE automatic_release_authorizations SET progress=%s,status=%s,
                revision=revision+1,updated_at=%s,stopped_at=%s WHERE id=%s AND owner_email=%s AND revision=%s''',
                (_json(progress),status,now,now if status=='stopped' else None,row['id'],row['owner_email'],row['revision']))
            if cur.rowcount != 1:
                raise ValueError('Authorization changed; refresh its status')
        updated=get(store,row['id'],row['owner_email'])
        if schedule:
            _schedule(store,updated,delay)
        return updated


def stop(store, authorization_id, owner):
    with store.transaction():
        row=get(store,authorization_id,owner,lock=True)
        if not row:
            raise ValueError('Authorization not found')
        if row['status'] in TERMINAL:
            return row
        return save(store,row,status='stopped',progress=row['progress'])


def update_file(store, authorization_id, owner, file, update, *, schedule=False, delay=20):
    if not isinstance(file,str) or not file or not isinstance(update,dict):
        raise ValueError('File and progress update are required')
    with store.transaction():
        row=get(store,authorization_id,owner,lock=True)
        if not row:
            raise ValueError('Authorization not found')
        if row['status'] in TERMINAL:
            return row
        progress=dict(row['progress'])
        files=dict(progress.get('files') or {})
        files[file]={**(files.get(file) or {}),**update}
        progress['files']=files
        return save(store,row,status=row['status'],progress=progress,schedule=schedule,delay=delay)


def by_request(store, owner, scan, request_id):
    identity = store.canonical_request_fingerprint([owner, scan, request_id])
    return get(store, identity, owner)


def wake(store, scan):
    """Promote the existing recovery tick; events carry no new release authority.

    A running tick consumes the wake flag when saving its next recovery job.
    Repeated events do not enqueue duplicate continuations or change tick identity.
    """
    with store.transaction():
        with store._db.cursor() as cur:
            store._db.execute(cur, "SELECT id,owner_email FROM automatic_release_authorizations "
                "WHERE scan_id=%s AND status IN ('active','waiting','blocked')", (scan,))
            scopes = store._db.fetchall(cur)
        for scope in scopes:
            row = get(store, scope['id'], scope['owner_email'], lock=True)
            if not row or row['status'] not in ACTIVE:
                continue
            revision = row['progress'].get('_tick_revision', 0)
            with store._db.cursor() as cur:
                store._db.execute(cur, "SELECT id,payload FROM jobs WHERE scan_id=%s "
                    "AND type='release_continue' AND status='queued'", (scan,))
                pending = store._db.fetchall(cur)
                matching = []
                for job in pending:
                    payload = json.loads(job['payload']) if isinstance(job['payload'], str) else job['payload']
                    if (payload.get('mode') == 'automatic' and payload.get('authorization_id') == row['id']
                            and payload.get('owner') == row['owner_email'] and payload.get('revision') == revision):
                        matching.append(job['id'])
                if matching:
                    for job_id in matching:
                        store._db.execute(cur, "UPDATE jobs SET run_after=%s,updated_at=%s "
                            "WHERE id=%s AND status='queued'", (store._now(), store._now(), job_id))
                else:
                    progress = {**row['progress'], '_wake_requested': True}
                    save(store, row, status=row['status'], progress=progress)
