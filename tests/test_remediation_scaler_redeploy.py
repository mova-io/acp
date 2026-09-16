"""Release queue additions must reach the live scaler without resizing the tier."""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('remediation_scaler_deploy', ROOT / 'deploy/public/remediation_scaler.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def template():
    return {
        'revisionSuffix': 'old', 'terminationGracePeriodSeconds': 30,
        'containers': [{'name': 'worker', 'image': 'old:image', 'resources': {'cpu': 2, 'memory': '4Gi'},
                        'env': [{'name': 'DATABASE_URL', 'secretRef': 'database-url'},
                                {'name': 'OTHER', 'value': 'preserve'},
                                {'name': 'ACP_SHUTDOWN_DRAIN_SECONDS', 'value': '20'}]}],
        'volumes': [{'name': 'data', 'storageType': 'EmptyDir'}],
        'scale': {'minReplicas': 5, 'maxReplicas': 10, 'cooldownPeriod': 300, 'pollingInterval': 30,
                  'rules': [{'name': 'other', 'custom': {'type': 'cpu', 'metadata': {'value': '80'}}},
                            {'name': 'remediation-queue', 'custom': {
                                'type': 'postgresql', 'identity': '/identities/queue',
                                'auth': [{'triggerParameter': 'connection', 'secretRef': 'database-url'}],
                                'metadata': {'query': 'OLD', 'targetQueryValue': '7', 'activationTargetQueryValue': '2'}}}]}}


def test_exact_preservation_except_rollout_fields_and_queue_query():
    before = template()
    untouched = deepcopy(before)
    expected = deepcopy(before)
    expected['revisionSuffix'] = ''
    expected['terminationGracePeriodSeconds'] = 600
    expected['containers'][0]['image'] = 'new:image'
    expected['containers'][0]['env'][-1]['value'] = '540'
    expected['scale']['rules'][1]['custom']['metadata']['query'] = helper.remediation_query()
    assert helper.worker_patch(before, 'new:image', 600, 540, helper.remediation_query()) == {
        'properties': {'template': expected}}
    assert before == untouched


def test_staging_bootstrap_queue_rule_keeps_its_name_and_settings():
    # Live staging was provisioned by deploy.sh with jobs-queued, min=max=1.
    # Its valid PostgreSQL rule must receive the lane query without resizing.
    before = template()
    before['scale'].update(minReplicas=1, maxReplicas=1)
    before['scale']['rules'][1]['name'] = 'jobs-queued'
    expected = deepcopy(before['scale'])
    expected['rules'][1]['custom']['metadata']['query'] = helper.remediation_query()
    patch = helper.worker_patch(before, 'new:image', 600, 540, helper.remediation_query())
    assert patch['properties']['template']['scale'] == expected
    assert before['scale']['rules'][1]['custom']['metadata']['query'] == 'OLD'


def test_both_known_queue_rules_are_ambiguous():
    source = template()
    legacy = deepcopy(source['scale']['rules'][1])
    legacy['name'] = 'jobs-queued'
    source['scale']['rules'].append(legacy)
    with pytest.raises(ValueError):
        helper.worker_patch(source, 'new:image', 600, 540, helper.remediation_query())


@pytest.mark.parametrize('mutation', [
    lambda t: t['scale'].update(rules=[]),
    lambda t: t['scale']['rules'].append(deepcopy(t['scale']['rules'][1])),
    lambda t: t['scale']['rules'][1]['custom'].update(type='http'),
    lambda t: t['scale']['rules'][1]['custom']['metadata'].pop('targetQueryValue'),
    lambda t: t['scale']['rules'][1].update(name='unrelated-queue'),
    lambda t: t['containers'].append(deepcopy(t['containers'][0])),
])
def test_ambiguous_or_missing_live_configuration_fails_closed(mutation):
    source = template()
    mutation(source)
    with pytest.raises(ValueError):
        helper.worker_patch(source, 'new:image', 600, 540, helper.remediation_query())


def test_generated_query_counts_every_authoritative_job_and_claimability():
    tree = ast.parse((ROOT / 'api/core.py').read_text())
    lane = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'REMEDIATE_LANE_JOB_TYPES' for t in n.targets))
    query = helper.remediation_query()
    assert 'prepare_release_package' in lane
    for job in lane:
        assert f"'{job}'" in query
    assert "run_after::timestamptz <= now() AND attempts < max_attempts" in query
    assert "status='queued'" in query
    assert "'assess_file'" not in query


def test_future_lane_addition_requires_no_deploy_list_edit(tmp_path):
    (tmp_path / 'api').mkdir()
    (tmp_path / 'api/core.py').write_text("REMEDIATE_LANE_JOB_TYPES = ('prepare_release_package', 'release_continue')\n")
    (tmp_path / 'api/queue_scaler.py').write_text((ROOT / 'api/queue_scaler.py').read_text())
    assert "'release_continue'" in helper.remediation_query(tmp_path)


def test_staging_remediation_patch_sets_reviewed_pool_and_preserves_scale(tmp_path):
    live = {'id': '/subscriptions/test/resourceGroups/test/providers/Microsoft.App/containerApps/acp-remediate',
            'properties': {'template': template()}}
    source = tmp_path / 'live.json'
    output = tmp_path / 'patch.json'
    source.write_text(json.dumps(live))
    result = subprocess.run([
        'python3', str(ROOT / 'deploy/public/remediation_scaler.py'), str(source), str(output),
        'new:image', '600', '540'], cwd=ROOT, env={**__import__('os').environ,
                                                   'ACP_DEPLOY_TARGET_ENV': 'staging'},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    patch = json.loads(output.read_text())['properties']['template']
    env = {entry['name']: entry.get('value') for entry in patch['containers'][0]['env']}
    assert env['ACP_WORKERS'] == '2'
    assert env['ACP_DB_MAX_CONN'] == '5'
    assert patch['scale']['minReplicas'] == 5
    assert patch['scale']['rules'][0] == template()['scale']['rules'][0]


def test_both_paths_use_same_update_after_active_job_guard():
    script = (ROOT / 'deploy/public/redeploy.sh').read_text()
    # Blue-green, production concurrent, and staging sequential paths share one updater.
    assert script.count('_update_lane_worker "$a"') == 3
    gate = script.index('if [ "$ACTIVE_JOBS" != 0 ]') if 'if [ "$ACTIVE_JOBS" != 0 ]' in script else script.index('queued/running')
    assert script.index('_prepare_remediation_worker_patch') > gate
    assert script.index('_prepare_remediation_worker_patch') < script.index('say "deploying green')
    assert script.count('_verify_remediation_scaler') == 2
    assert all(pos > gate for pos in [script.index('_update_lane_worker "$a"'), script.rindex('_update_lane_worker "$a"')])


@pytest.mark.parametrize('queue_name', ['remediation-queue', 'jobs-queued'])
@pytest.mark.parametrize('corrupt_verification', [False, True])
def test_guarded_worker_helper_sends_one_patch_preserving_other_rules(tmp_path, corrupt_verification, queue_name):
    live = {'id': '/subscriptions/test/resourceGroups/test/providers/Microsoft.App/containerApps/acp-remediate',
            'properties': {'template': template()}}
    live['properties']['template']['scale']['rules'][1]['name'] = queue_name
    fixture = tmp_path / 'live.json'
    fixture.write_text(json.dumps(live))
    output = tmp_path / 'patch.json'
    (tmp_path / 'corrupt').write_text('yes' if corrupt_verification else 'no')
    harness = r'''
set -euo pipefail
source deploy/public/remediation_scaler.sh
SRC_ROOT="$PWD"
AZ=(--only-show-errors); RG=test; REMEDIATE_WORKER=acp-remediate; IMG=new:image
WORKER_TERMINATION_GRACE_SECONDS=600; WORKER_DRAIN_SECONDS=540; WORK="$1"
_aca_retry() { "$@"; }
az() {
  if [ "$1 $2" = 'containerapp show' ]; then
    if [[ "$*" == *'--query properties.template.scale'* ]]; then
      python3 - "$WORK" <<'VERIFY_FIXTURE'
import json, sys
from pathlib import Path
work = Path(sys.argv[1])
scale = json.loads((work / 'patch.json').read_text())['properties']['template']['scale']
if (work / 'corrupt').read_text() == 'yes':
    scale['minReplicas'] = 99
print(json.dumps(scale))
VERIFY_FIXTURE
    else cat "$WORK/live.json"; fi
    return
  fi
  if [ "$1 $2 $3" = 'rest --method patch' ]; then
    while [ "$#" -gt 0 ]; do
      if [ "$1" = '--body' ]; then shift; cp "${1#@}" "$WORK/patch.json"; return; fi
      shift
    done
  fi
  echo 'unexpected mutation' >&2; return 1
}
_prepare_remediation_worker_patch
_update_lane_worker acp-remediate
_verify_remediation_scaler
'''
    result = subprocess.run(['bash', '-c', harness, 'harness', str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == (1 if corrupt_verification else 0), result.stderr
    if corrupt_verification:
        assert 'live scale differs' in result.stderr
        return
    patch = json.loads(output.read_text())
    expected = helper.worker_patch(live['properties']['template'], 'new:image', 600, 540, helper.remediation_query())
    expected['properties']['template']['containers'][0]['env'].append({'name': 'ACP_DEDICATED_RELEASE_WORKERS', 'value': '0'})
    assert patch == expected
    assert list(tmp_path.glob('remediation-*')) == []
