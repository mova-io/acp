"""Release capacity is independent, bounded, and opt-in for existing installs."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_dedicated_claims_reserve_delivery_and_reports(monkeypatch, isolated_store):
    import core
    import handlers
    monkeypatch.setenv('ACP_DEDICATED_RELEASE_WORKERS', '1')
    jobs = {kind: isolated_store.enqueue_job(kind, {}) for kind in
            ('remediate_file', 'publish_file', 'publish_release_reports')}
    monkeypatch.setenv('ACP_WORKER_ROLE', 'remediate')
    types = core._worker_job_types(0, 2)
    assert isolated_store.claim_job('remediate', job_types=types)['id'] == jobs['remediate_file']
    assert isolated_store.claim_job('remediate', job_types=types) is None
    monkeypatch.setenv('ACP_WORKER_ROLE', 'release')
    reports = core._worker_job_types(0, 3); delivery = core._worker_job_types(1, 3)
    assert set(reports).isdisjoint(delivery)
    assert set(reports + delivery) == set(core.RELEASE_LANE_JOB_TYPES)
    assert isolated_store.claim_job('release-report', job_types=reports)['id'] == jobs['publish_release_reports']
    assert isolated_store.claim_job('release-delivery', job_types=delivery)['id'] == jobs['publish_file']
    assert core._worker_job_types(2, 3) == delivery
    with pytest.raises(ValueError, match='at least three'):
        core._worker_job_types(0, 1)


@pytest.mark.parametrize('role', ['remediate', 'processing', 'mixed'])
def test_legacy_workers_keep_release_until_cutover(monkeypatch, isolated_store, role):
    import core
    import handlers
    monkeypatch.setenv('ACP_WORKER_ROLE', role)
    monkeypatch.delenv('ACP_DEDICATED_RELEASE_WORKERS', raising=False)
    before = core._worker_job_types(11, 12)
    assert before is None or 'publish_file' in before
    monkeypatch.setenv('ACP_DEDICATED_RELEASE_WORKERS', '1')
    after = core._worker_job_types(11, 12)
    assert set(after).isdisjoint(core.RELEASE_LANE_JOB_TYPES)


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/public' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


def source():
    return dict(name='acp-remediate', location='eastus2', identity={'type': 'SystemAssigned'}, properties={
        'managedEnvironmentId': '/environment',
        'configuration': {'ingress': {'external': True}, 'registries': [], 'secrets': []},
        'template': {'containers': [{'name': 'worker', 'image': 'old', 'command': ['python', 'worker_main.py'],
            'env': [{'name': 'ACP_DEPLOY_ENV', 'value': 'production'}, {'name': 'ACP_WORKER_ROLE', 'value': 'remediate'},
                    {'name': 'DATABASE_URL', 'secretRef': 'database'}]}],
            'scale': {'rules': [{'name': 'remediation-queue', 'custom': {'type': 'postgresql',
                'metadata': {'query': 'old', 'targetQueryValue': '4'},
                'auth': [{'secretRef': 'database-keda', 'triggerParameter': 'connection'}]}}]}}})


def test_private_bounded_release_template_preserves_dependencies():
    worker = module('release_worker'); original = source()
    result = worker.definition(original, [{'name': 'database', 'value': 'private'}], 'acp-release', 'new', 'production')
    props = result['properties']; template = props['template']; container = template['containers'][0]
    assert 'ingress' not in props['configuration']
    assert template['scale']['minReplicas'] == 0 and template['scale']['maxReplicas'] == 1
    assert container['command'] == ['python', 'worker_main.py']
    env = {e['name']: e for e in container['env']}
    assert env['DATABASE_URL']['secretRef'] == 'database'
    assert env['ACP_WORKERS']['value'] == '3'
    assert env['ACP_DB_MAX_CONN']['value'] == '6'
    query = template['scale']['rules'][0]['custom']['metadata']['query']
    assert 'publish_file' in query and 'remediate_file' not in query
    assert 'run_after' in query and 'attempts < max_attempts' in query
    assert original['properties']['template']['containers'][0]['image'] == 'old'


def test_release_environment_and_identity_boundaries():
    worker = module('release_worker')
    for name, environment in [('acp-release', 'staging'), ('acp-release-staging', 'production'), ('acp-remediate', 'production')]:
        with pytest.raises(ValueError):
            worker.definition(source(), [], name, 'new', environment)
    registry_source = source()
    registry_source['properties']['configuration']['registries'] = [{'identity':'system'}]
    with pytest.raises(ValueError, match='registry access'):
        worker.definition(registry_source, [], 'acp-release', 'new', 'production')
    with pytest.raises(ValueError, match='Key Vault'):
        worker.definition(source(), [{'keyVaultUrl':'https://vault', 'identity':'system'}], 'acp-release', 'new', 'production')


def test_remediation_scaler_matches_cutover(monkeypatch, isolated_store):
    import core
    import queue_scaler
    scaler = module('remediation_scaler')
    monkeypatch.setenv('ACP_DEDICATED_RELEASE_WORKERS', '1')
    assert scaler.remediation_query(dedicated=True) == queue_scaler.depth_query(core.remediation_job_types())
    assert 'release' in queue_scaler.lane_job_types()
    monkeypatch.delenv('ACP_DEDICATED_RELEASE_WORKERS')
    assert scaler.remediation_query() == queue_scaler.depth_query(core.remediation_job_types())


def test_release_heartbeat_is_visible(monkeypatch, isolated_store):
    from datetime import datetime, timezone
    import json
    isolated_store.set_setting('worker_tier_heartbeat:release', json.dumps({
        'at': datetime.now(timezone.utc).isoformat(), 'pool_size': 3, 'version': 'release-test'}))
    status = isolated_store.worker_roles_status()
    assert status['release']['alive'] is True
    assert status['release']['pool_size'] == 3


def test_all_release_work_is_claimable_after_cutover(monkeypatch):
    import core
    import handlers
    from worker import HANDLERS
    monkeypatch.setenv('ACP_DEDICATED_RELEASE_WORKERS', '1')
    claimed = set()
    for role in ('discovery', 'assess', 'remediate', 'release', 'processing'):
        monkeypatch.setenv('ACP_WORKER_ROLE', role)
        for index in range(3):
            claimed.update(core._worker_job_types(index, 3))
    assert set(HANDLERS) <= claimed


def test_release_queue_counts_only_claimable_release_work(isolated_store):
    import core
    isolated_store.enqueue_job('publish_file', {})
    isolated_store.enqueue_job('publish_release_reports', {})
    isolated_store.enqueue_job('remediate_file', {})
    delayed = isolated_store.enqueue_job('publish_file', {})
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, 'UPDATE jobs SET run_after=%s WHERE id=%s', ('2999-01-01T00:00:00+00:00', delayed))
    snapshot = isolated_store.worker_lane_queue(core.RELEASE_LANE_JOB_TYPES)
    assert snapshot['claimable'] == 2
    assert snapshot['oldest_created_at']


@pytest.mark.parametrize('grant_fails', [False, True])
def test_provisioning_cannot_start_jobs_before_storage_grant(monkeypatch, capsys, grant_fails):
    import json
    import sys
    worker = module('release_worker')
    original = source(); original['identity']['principalId'] = 'source-principal'
    calls = []
    def fake_az(*args):
        calls.append(args[:3])
        if args[:2] == ('containerapp', 'show'): return original
        if args[:3] == ('containerapp', 'secret', 'list'): return [{'name':'database', 'value':'DO-NOT-PRINT'}]
        if args[:2] == ('containerapp', 'list'): return []
        if args[:3] == ('role', 'assignment', 'list'):
            return [{'roleDefinitionId':'/roles/ba92f5b4-2d11-453d-a403-e96b0029c9fe', 'scope':'/storage', 'condition':'restricted scope', 'conditionVersion':'2.0'}]
        if args[:2] == ('containerapp', 'create'):
            spec = json.loads(Path(args[args.index('--yaml') + 1]).read_text())
            assert spec['properties']['template']['scale']['minReplicas'] == 0
            assert spec['properties']['template']['scale']['rules'] == []
            return {'identity': {'principalId':'new-principal'}, 'id':'/new-app'}
        if args[:3] == ('role', 'assignment', 'create'):
            assert args[args.index('--condition') + 1] == 'restricted scope'
            assert args[args.index('--condition-version') + 1] == '2.0'
            if grant_fails: raise RuntimeError('grant failed')
            return {}
        if args[0] == 'rest':
            assert ('role', 'assignment', 'create') in calls
            patch = json.loads(Path(args[args.index('--body') + 1][1:]).read_text())
            assert patch['properties']['template']['scale']['minReplicas'] == 1
            assert patch['properties']['template']['scale']['rules'][0]['name'] == 'release-queue'
            return {}
        raise AssertionError(args)
    monkeypatch.setattr(worker, 'az', fake_az)
    monkeypatch.setattr(sys, 'argv', ['worker', '--subscription','s','--resource-group','g','--source','acp-remediate',
        '--name','acp-release','--image','verified-image','--environment','production','--apply'])
    if grant_fails:
        with pytest.raises(RuntimeError, match='grant failed'): worker.main()
        assert not any(call[0] == 'rest' for call in calls)
    else:
        worker.main()
    assert 'DO-NOT-PRINT' not in capsys.readouterr().out


def test_rollout_preflight_requires_three_live_release_slots():
    script = (ROOT / 'deploy/public/redeploy.sh').read_text()
    assert 'int(r.get("pool_size") or 0) < 3' in script
    assert 'live three-slot heartbeat' in script


def test_redeploy_normalizes_existing_release_worker_capacity():
    script = (ROOT / 'deploy/public/remediation_scaler.sh').read_text()
    assert '[ -n "${RELEASE_WORKER:-}" ] && [ "$app" = "$RELEASE_WORKER" ]' in script
    assert '[ "${DEPLOY_TARGET_ENV:-production}" != staging ]' in script
    assert 'release_capacity=("ACP_WORKERS=3" "ACP_DB_MAX_CONN=6")' in script
