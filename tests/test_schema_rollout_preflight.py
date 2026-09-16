"""Deployment authority, transport boundaries and crash-and-retry refusal."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/public' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


gate = module('schema_preflight')
update = module('update_api_image')
evidence = module('startup_evidence')


def app(name='api-staging', database='main', secret=False):
    connection = {'name': 'DATABASE_URL', **({'secretRef': 'database'} if secret else
                  {'value': 'postgresql://user:credential@localhost/' + database})}
    main = {'name': name, 'image': 'old', 'env': [{'name': 'ACP_DEPLOY_ENV', 'value': 'staging'}, connection],
            'probes': [{'type': 'Readiness', 'httpGet': {'path': '/probe/readyz', 'port': 8077}, 'failureThreshold': 10}],
            'resources': {'cpu': 1, 'memory': '2Gi'}, 'volumeMounts': [{'volumeName': 'keep', 'mountPath': '/keep'}]}
    sidecar = {'name': 'sidecar', 'image': 'side', 'env': [{'name': 'DATABASE_URL', 'value': 'postgresql://side@localhost/wrong'}]}
    return {'id': '/subscriptions/sub/resourceGroups/group/providers/Microsoft.App/containerApps/' + name,
            'name': name, 'properties': {'latestRevisionName': 'new', 'latestReadyRevisionName': 'new',
                                       'template': {'containers': [sidecar, main], 'scale': {'maxReplicas': 1}}}}


def test_named_role_database_and_secret_authority_with_first_sidecar(monkeypatch):
    row = app(secret=True)
    def azure(subscription, *args):
        return [{'name': 'database', 'value': 'postgresql://user:credential@localhost/main'}] if args[1] == 'secret' else row
    monkeypatch.setattr(gate, 'azure', azure)
    assert gate.target_connections('sub', 'group', ['api-staging'], 'staging')[0][2].endswith('/main')


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong_env'])
def test_ambiguous_or_wrong_environment_role_is_refused(monkeypatch, mutation):
    row = app()
    containers = row['properties']['template']['containers']
    if mutation == 'missing':
        containers.pop()
    elif mutation == 'duplicate':
        containers.append(deepcopy(containers[1]))
    else:
        containers[1]['env'][0]['value'] = 'production'
    monkeypatch.setattr(gate, 'azure', lambda *args: row)
    with pytest.raises(gate.PreflightRefused):
        gate.target_connections('sub', 'group', ['api-staging'], 'staging')


def test_actual_roles_database_mismatch_is_not_hidden_by_equal_sidecars(monkeypatch):
    monkeypatch.setattr(gate, 'azure', lambda subscription, *args: app(args[args.index('-n') + 1],
                                                                    'one' if 'api-staging' in args else 'two'))
    with pytest.raises(gate.PreflightRefused, match='role_database_mismatch'):
        gate.target_connections('sub', 'group', ['api-staging', 'worker-staging'], 'staging')


def test_hostaddr_override_is_part_of_database_identity():
    assert gate.database_identity('host=same hostaddr=127.0.0.1 dbname=db') != gate.database_identity('host=same hostaddr=127.0.0.2 dbname=db')


def test_one_api_revision_adds_startup_and_preserves_readiness_liveness_and_secrets():
    template = app()['properties']['template']
    template['containers'][1]['probes'].append({'type': 'Liveness', 'tcpSocket': {'port': 8077}})
    template['containers'][1]['env'].append({'name': 'SECRET', 'secretRef': 'keep'})
    before = deepcopy(template)
    result = update.update_template(template, 'api-staging', 'new-image', {'ACP_DEPLOY_ENV': 'staging'}, 'green')
    assert template == before
    main = result['containers'][1]
    assert main['image'] == 'new-image' and result['revisionSuffix'] == 'green'
    assert main['probes'][:-1] == before['containers'][1]['probes']
    assert main['probes'][-1] == update.STARTUP_PROBE
    assert {'name': 'SECRET', 'secretRef': 'keep'} in main['env']
    assert main['volumeMounts'] == before['containers'][1]['volumeMounts']
    assert result['containers'][0] == before['containers'][0]


@pytest.mark.parametrize('probe', [
    {'type': 'Startup', 'tcpSocket': {'port': 9999}, 'failureThreshold': 10, 'periodSeconds': 10},
    {'type': 'Startup', 'httpGet': {'port': 8077, 'path': '/probe/readyz'}, 'failureThreshold': 10, 'periodSeconds': 10},
    {'type': 'Startup', 'tcpSocket': {'port': 8077}, 'failureThreshold': 3, 'periodSeconds': 10},
])
def test_wrong_or_short_existing_startup_requires_review(probe):
    template = app()['properties']['template']
    template['containers'][1]['probes'].append(probe)
    with pytest.raises(ValueError):
        update.update_template(template, 'api-staging', 'new', {})


def test_valid_existing_startup_is_preserved():
    template = app()['properties']['template']
    probe = {'type': 'Startup', 'tcpSocket': {'port': 8077}, 'failureThreshold': 10, 'periodSeconds': 20}
    template['containers'][1]['probes'].append(probe)
    assert update.update_template(template, 'api-staging', 'new', {})['containers'][1]['probes'][-1] == probe


@pytest.mark.parametrize('busy', [False, True])
def test_actual_api_writer_private_patch_cleanup_and_redacted_busy_failure(monkeypatch, capsys, busy):
    from types import SimpleNamespace
    row = app()
    paths = []
    def run(command, **kwargs):
        if command[1] == 'containerapp':
            return SimpleNamespace(returncode=0, stdout=json.dumps(row), stderr='')
        path = Path(command[command.index('--body') + 1][1:])
        paths.append(path)
        assert path.stat().st_mode & 0o777 == 0o600
        patch = json.loads(path.read_text())
        assert patch['properties']['template']['containers'][1]['image'] == 'new'
        assert patch['properties']['template']['containers'][1]['probes'][-1] == update.STARTUP_PROBE
        return SimpleNamespace(returncode=int(busy), stdout='{}',
                               stderr='ContainerAppOperationInProgress credential=secret' if busy else '')
    monkeypatch.setattr(update.subprocess, 'run', run)
    assert update.main(['--subscription', 'sub', '--group', 'group', '--app', 'api-staging',
                        '--image', 'new']) == int(busy)
    captured = capsys.readouterr()
    assert 'credential' not in captured.out + captured.err and 'secret' not in captured.out + captured.err
    assert all(not path.exists() for path in paths)
    if busy:
        assert captured.err.strip() == 'ContainerAppOperationInProgress'


def test_configuration_changed_after_migration_refuses_rollout_receipt(monkeypatch, capsys):
    snapshots = iter([[('api-staging', 'revision', 'secret-one')], [('api-staging', 'revision', 'secret-two')]])
    monkeypatch.setattr(gate, 'target_connections', lambda *args: next(snapshots))
    monkeypatch.setattr(gate, 'bounded_prepare', lambda *args: {'ok': True, 'version': 56})
    assert gate.main(['--subscription', 'sub', '--group', 'group', '--environment', 'staging',
                      '--api-path', str(ROOT / 'api'), 'api-staging']) == 1
    output = capsys.readouterr().out
    assert 'target_configuration_changed' in output and 'secret-one' not in output and 'secret-two' not in output


@pytest.mark.parametrize('restart', [0, 1])
def test_healthy_last_process_cannot_hide_startup_restart(restart):
    row = app()
    replicas = [{'properties': {'containers': [{'ready': True, 'started': True, 'restartCount': restart}]}}]
    result = evidence.receipt(row, replicas, [], [], 'old')
    assert result['ok'] is (restart == 0)


def test_deleted_failed_process_is_retained_from_system_evidence_without_credentials():
    row = app()
    replicas = [{'properties': {'containers': [{'ready': True, 'started': True, 'restartCount': 0}]}}]
    system = [{'RevisionName': 'new', 'Reason': 'ContainerTerminated', 'Msg': "exit code '3' credential=secret", 'TimeStamp': 'now'}]
    console = [{'Log': '{"event":"schema.boot","state":"failed","sqlstate":"55P03","message":"credential=secret"}'}]
    result = evidence.receipt(row, replicas, system, console, 'old')
    assert not result['ok'] and result['events'][0]['exit_code'] == 3
    assert 'credential' not in json.dumps(result) and 'secret' not in json.dumps(result)


@pytest.mark.parametrize('blue_green,active_override', [('0', '0'), ('1', '0'), ('0', '1')])
def test_real_shell_failure_gate_stops_both_rollout_paths(tmp_path, blue_green, active_override):
    script = (ROOT / 'deploy/public/redeploy.sh').read_text()
    body = script[script.index('# Schema-changing releases'):]
    fake = tmp_path / 'bin'
    fake.mkdir()
    python = fake / 'python3'
    python.write_text('#!/bin/sh\nexit 41\n')
    python.chmod(0o755)
    sentinel = tmp_path / 'mutation'
    prelude = f'''set -euo pipefail
say() {{ :; }}
die() {{ exit 42; }}
_aca_retry() {{ touch '{sentinel}'; exit 43; }}
az() {{ touch '{sentinel}'; exit 43; }}
SRC_ROOT='{ROOT}'
SUB=sub
RG=group
DEPLOY_TARGET_ENV=staging
APP=api-staging
LANE_WORKERS=(worker-staging)
BG={blue_green}
ALLOW_ACTIVE_JOBS={active_override}
PATH='{fake}':$PATH
'''
    result = subprocess.run(['bash', '-c', prelude + body], capture_output=True, text=True)
    assert result.returncode == 42
    assert not sentinel.exists(), 'failed schema gate reached an app/worker mutation'
