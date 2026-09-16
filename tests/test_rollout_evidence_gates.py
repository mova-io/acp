import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/public'))
import queue_activity_gate as queue_gate  # noqa: E402
import revision_replica_gate as revision_gate  # noqa: E402


@pytest.mark.parametrize('payload', [{}, {'queue': {}}, {'queue': {'active': None}},
                                      {'queue': {'active': True}}, {'queue': {'active': -1}}])
def test_missing_or_invalid_queue_evidence_fails_closed(payload, monkeypatch):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    assert queue_gate.main() == 1


def test_numeric_queue_evidence_is_accepted(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"queue":{"active":0}}'))
    assert queue_gate.main() == 0
    assert capsys.readouterr().out.strip() == '0'


def test_failed_readyz_curl_cannot_become_zero_queue_activity(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = re.search(r'_queue_active_after_quiescence\(\) \{.*?\n\}', source, re.S).group(0)
    harness = tmp_path / 'gate.sh'
    harness.write_text('set -euo pipefail\n' + function + '''
FQDN=staging.invalid
SRC_ROOT="$1"
curl() { return 22; }
_queue_active_after_quiescence
''')
    result = subprocess.run(['bash', str(harness), str(ROOT)], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout.strip() == ''


def test_active_job_override_cannot_reach_first_bootstrap_mutation(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = re.search(r'_require_empty_staging_queue\(\) \{.*?\n\}', source, re.S).group(0)
    harness = tmp_path / 'empty.sh'
    harness.write_text('set -euo pipefail\n' + function + '''
ACP_DEPLOY_WITH_ACTIVE_JOBS=1
_queue_active_after_quiescence() { echo 2; }
die() { echo "$*" >&2; return 1; }
_require_empty_staging_queue
touch "$1/mutated"
''')
    result = subprocess.run(['bash', str(harness), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / 'mutated').exists()
    assert 'before any mutation' in result.stderr


def test_schema_preparation_orders_bootstrap_and_recovers_on_preflight_failure(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_prepare_schema_for_rollout() {', 1)[1].split('\n}', 1)[0]
    harness = tmp_path / 'schema-order.sh'
    harness.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
LOG="$2/order-log"
DEPLOY_TARGET_ENV="$1"
STAGING_DB_BOOTSTRAP="${{BOOTSTRAP:-0}}"
say() {{ :; }}
die() {{ exit 23; }}
_require_empty_staging_queue() {{ echo queue >> "$LOG"; }}
_require_single_revision_mode() {{ echo mode >> "$LOG"; }}
_ensure_single_revision_mode() {{ echo ensure-mode >> "$LOG"; }}
_staging_bootstrap_exit() {{ status=$?; trap - EXIT; echo recover >> "$LOG"; exit "$status"; }}
_quiesce_staging_workers() {{ echo quiesce >> "$LOG"; trap _staging_bootstrap_exit EXIT; }}
_schema_preflight() {{ echo schema >> "$LOG"; [ "${{SCHEMA_FAIL:-0}}" != 1 ] || die; }}
_update_api_normal() {{ echo api >> "$LOG"; }}
_prepare_schema_for_rollout() {{{function}
}}
_prepare_schema_for_rollout
_update_api_normal
''')
    harness.chmod(0o755)

    env = {**os.environ, 'BOOTSTRAP': '1'}
    result = subprocess.run([str(harness), 'staging', str(tmp_path)], env=env)
    assert result.returncode == 0
    assert (tmp_path / 'order-log').read_text().splitlines() == [
        'queue', 'mode', 'quiesce', 'schema', 'api', 'recover']

    (tmp_path / 'order-log').unlink()
    result = subprocess.run([str(harness), 'staging', str(tmp_path)],
                            env={**env, 'SCHEMA_FAIL': '1'})
    assert result.returncode == 23
    assert (tmp_path / 'order-log').read_text().splitlines() == [
        'queue', 'mode', 'quiesce', 'schema', 'recover']


@pytest.mark.parametrize('environment', ['staging', 'production'])
def test_nonbootstrap_schema_preflight_precedes_image_update(tmp_path, environment):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_prepare_schema_for_rollout() {', 1)[1].split('\n}', 1)[0]
    harness = tmp_path / 'normal-schema-order.sh'
    harness.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
LOG="$2/order-log"
DEPLOY_TARGET_ENV="$1"
STAGING_DB_BOOTSTRAP=0
_require_empty_staging_queue() {{ echo queue >> "$LOG"; }}
_quiesce_staging_workers() {{ echo quiesce >> "$LOG"; }}
_require_single_revision_mode() {{ echo require-mode >> "$LOG"; }}
_ensure_single_revision_mode() {{ echo mode >> "$LOG"; }}
_schema_preflight() {{ echo schema >> "$LOG"; }}
_update_api_normal() {{ echo api >> "$LOG"; }}
_prepare_schema_for_rollout() {{{function}
}}
_prepare_schema_for_rollout
_update_api_normal
''')
    harness.chmod(0o755)
    result = subprocess.run([str(harness), environment, str(tmp_path)])
    assert result.returncode == 0
    expected = ['schema', 'mode', 'api'] if environment == 'staging' else ['schema', 'api']
    assert (tmp_path / 'order-log').read_text().splitlines() == expected


@pytest.mark.parametrize('api_attempted', [0, 1])
def test_recovery_verifies_untouched_api_without_mutating_it_and_restores_workers(
        tmp_path, api_attempted):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_restore_staging_workers() {', 1)[1].split('\n_staging_bootstrap_exit()', 1)[0]
    harness = tmp_path / 'recover.sh'
    harness.write_text('set -euo pipefail\n_restore_staging_workers() {' + function + '''
STAGING_WORKERS_QUIESCED=1; STAGING_API_TARGET_READY=0
STAGING_API_MUTATION_ATTEMPTED=$3
STAGING_OLD_API_IMAGE=old:image; STAGING_OLD_API_REVISION=oldrev
LANE_WORKERS=(worker); STAGING_OLD_IMAGES=(old-worker:image); STAGING_OLD_MINS=(2)
AZ=(fake); RG=g; APP=api; SUB=s; SRC_ROOT="$1"; API_ENV_VARS=(ACP_DB_MAX_CONN=8)
LOG="$2/recovery-log"
WORKER_TERMINATION_GRACE_SECONDS=600; WORKER_DRAIN_SECONDS=540; RELEASE_WORKER=release
_aca_retry() { "$@"; }
_wait_recovery_cohort() { echo "$*" >> "$LOG"; return 0; }
sleep() { :; }
python3() { echo "$*" >> "$LOG"; }
az() {
  echo "az $*" >> "$LOG"
  case "$*" in
    *latestReadyRevisionName*) echo oldrev ;;
    *latestRevisionName*) echo oldrev ;;
    *"containers[0].image"*) echo old:image ;;
    *"containerapp update"*) : ;;
    *"revision list"*) echo 1 ;;
    *"replica list"*) echo 1 ;;
    *) return 1 ;;
  esac
}
_restore_staging_workers
''')
    result = subprocess.run(['bash', str(harness), str(ROOT), str(tmp_path), str(api_attempted)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    log = (tmp_path / 'recovery-log').read_text()
    assert 'api oldrev old:image 1' in log
    assert 'worker oldrev old-worker:image 2' in log
    assert 'containerapp update' in log and '--image old-worker:image' in log
    assert ('update_api_image.py' in log) is bool(api_attempted)


def test_bootstrap_revision_mode_gate_is_read_only_and_fails_closed(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_require_single_revision_mode() {', 1)[1].split('\n}', 1)[0]
    assert '_aca_retry' not in function and 'set-mode' not in function
    harness = tmp_path / 'mode-gate.sh'
    harness.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
APP=api; LANE_WORKERS=(worker); AZ=(fake); RG=g
die() {{ echo "$*" >&2; exit 19; }}
az() {{ case "$*" in *"-n worker"*) echo Multiple ;; *) echo Single ;; esac; }}
_require_single_revision_mode() {{{function}
}}
_require_single_revision_mode
touch "$1/mutated"
''')
    harness.chmod(0o755)
    result = subprocess.run([str(harness), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 19
    assert not (tmp_path / 'mutated').exists()
    assert 'before any mutation' in result.stderr


def test_late_worker_snapshot_failure_causes_zero_mutation(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_quiesce_staging_workers() {', 1)[1].split('\n}', 1)[0]
    harness = tmp_path / 'snapshot-failure.sh'
    harness.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
_quiesce_staging_workers() {{{function}
}}
WORK="$1"; APP=api; RG=g; AZ=(fake); LANE_WORKERS=(worker1 worker2)
STAGING_OLD_IMAGES=(); STAGING_OLD_MINS=(); STAGING_OLD_REVISIONS=()
STAGING_WORKERS_QUIESCED=0
die() {{ echo "$*" >&2; exit 17; }}
az() {{
  case "$*" in
    *"containerapp update"*|*"revision deactivate"*|*"revision set-mode"*) echo "$*" >> "$WORK/mutations" ;;
  esac
  case "$*" in
    *"-n worker2"*"containers[0].image"*) echo '' ;;
    *"containers[0].image"*) echo old:image ;;
    *latestRevisionName*) echo api-old ;;
    *scale.minReplicas*) echo 1 ;;
    *"revision list"*) echo worker-old ;;
    *) return 1 ;;
  esac
}}
_quiesce_staging_workers
''')
    harness.chmod(0o755)
    result = subprocess.run([str(harness), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 17
    assert 'worker2' in result.stderr
    assert not (tmp_path / 'mutations').exists()


@pytest.mark.parametrize('old_replicas,expected', [(1, 1), (0, 0)])
def test_recovery_gate_requires_zero_prior_replicas_and_sole_ready_image(
        tmp_path, old_replicas, expected):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_wait_recovery_cohort() {', 1)[1].split('\n}', 1)[0]
    harness = tmp_path / 'recovery-gate.sh'
    harness.write_text('set -euo pipefail\n_wait_recovery_cohort() {' + function + f'''\n}}
AZ=(fake); RG=g; SUB=s; SRC_ROOT="$1"; OLD_REPLICAS={old_replicas}
sleep() {{ :; }}
python3() {{
  if [[ "$*" == *revision_replica_gate.py* ]]; then cat >/dev/null; echo "true 1 $OLD_REPLICAS";
  else command python3 "$@"; fi
}}
az() {{
  case "$*" in
    *latestReadyRevisionName*) echo rev373 ;;
    *latestRevisionName*) echo rev373 ;;
    *"containers[0].image"*) echo old:image ;;
    *latestRevisionName*) echo old ;;
    *"revision list"*) echo '[{{"name":"rev372","properties":{{"active":false,"replicas":'$OLD_REPLICAS'}}}},{{"name":"rev373","properties":{{"active":true,"replicas":1}}}}]' ;;
    *"replica list"*) echo 1 ;;
    *) return 1 ;;
  esac
}}
_wait_recovery_cohort worker rev372 old:image 1
''')
    result = subprocess.run(['bash', str(harness), str(ROOT)], capture_output=True, text=True)
    assert (result.returncode == 0) is (expected == 0)


def test_failed_revision_enumeration_cannot_converge_cohort(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = re.search(r'_wait_rollout_cohort\(\) \{.*?\n\}', source, re.S).group(0)
    harness = tmp_path / 'cohort.sh'
    harness.write_text('set -euo pipefail\n' + function + '''
AZ=(fake); RG=g
_verify_startup() { :; }
sleep() { :; }
die() { echo "$*" >&2; return 1; }
az() {
  if [[ "$*" == *"containerapp show"* ]]; then echo latest; return 0; fi
  return 1
}
_wait_rollout_cohort worker prior
''')
    result = subprocess.run(['bash', str(harness)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'did not converge' in result.stderr


def _snapshot(*names):
    return [{'name': name, 'properties': {'active': False, 'replicas': 0}} for name in names]


def test_revision_replica_list_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(revision_gate.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, '', 'unavailable'))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(_snapshot('old', 'config'))))
    assert revision_gate.main(['--subscription', 's', '--group', 'g', '--app', 'a',
                               '--revision', 'old', '--revision', 'config']) == 1


def test_residual_configuration_revision_replica_is_counted(monkeypatch, capsys):
    def run(command, **_kwargs):
        revision = command[command.index('--revision') + 1]
        return subprocess.CompletedProcess(command, 0, '1\n' if revision == 'config' else '0\n', '')

    monkeypatch.setattr(revision_gate.subprocess, 'run', run)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(_snapshot('old', 'config'))))
    assert revision_gate.main(['--subscription', 's', '--group', 'g', '--app', 'a',
                               '--revision', 'old', '--revision', 'config']) == 0
    assert capsys.readouterr().out.strip() == 'true 0 1'


def test_complete_zero_replica_snapshot_converges(monkeypatch, capsys):
    monkeypatch.setattr(revision_gate.subprocess, 'run', lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, '0\n', ''))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(_snapshot('old', 'config'))))
    assert revision_gate.main(['--subscription', 's', '--group', 'g', '--app', 'a',
                               '--revision', 'old', '--revision', 'config']) == 0
    assert capsys.readouterr().out.strip() == 'true 0 0'


def test_hundreds_of_historical_revisions_do_not_expand_arm_calls(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(revision_gate.subprocess, 'run', lambda command, **_kwargs:
                        (calls.append(command) or subprocess.CompletedProcess(command, 0, '0\n', '')))
    rows = _snapshot(*[f'historical-{index}' for index in range(400)], 'old', 'config')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(rows)))
    assert revision_gate.main(['--subscription', 's', '--group', 'g', '--app', 'a',
                               '--revision', 'old', '--revision', 'config']) == 0
    assert len(calls) == 2
    assert capsys.readouterr().out.strip() == 'true 0 0'


def test_older_residual_replica_is_discovered_without_scanning_history(monkeypatch, capsys):
    calls = []
    rows = _snapshot(*[f'historical-{index}' for index in range(400)], 'prior', 'latest')
    rows[37]['properties']['replicas'] = 1
    def run(command, **_kwargs):
        calls.append(command)
        revision = command[command.index('--revision') + 1]
        return subprocess.CompletedProcess(command, 0, '1\n' if revision == 'historical-37' else '0\n', '')
    monkeypatch.setattr(revision_gate.subprocess, 'run', run)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(rows)))
    assert revision_gate.main(['--subscription', 's', '--group', 'g', '--app', 'a',
                               '--revision', 'prior', '--revision', 'latest',
                               '--exclude-revision', 'latest']) == 0
    assert len(calls) == 2  # prior plus the one observed historical residual; latest excluded
    assert capsys.readouterr().out.strip() == 'true 0 1'


def test_delayed_configuration_revision_is_observed_and_deactivated(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_quiesce_staging_workers() {', 1)[1].split('\n}', 1)[0]
    real_python = sys.executable
    harness = tmp_path / 'quiesce.sh'
    harness.write_text(f'''set -euo pipefail
_quiesce_staging_workers() {{{function}
}}
REAL_PYTHON={real_python!r}
WORK="$1"; SRC_ROOT="$2"; SUB=s; RG=g; FQDN=f; APP=api; AZ=(fake)
LANE_WORKERS=(worker); STAGING_OLD_IMAGES=(); STAGING_OLD_MINS=()
CURRENT_API_POOL=16; STAGING_DB_EXTERNAL_CONNECTIONS=3; WORKER_TERMINATION_GRACE_SECONDS=600
STAGING_WORKERS_QUIESCED=0; ACTIVE_JOBS=0
_aca_retry() {{ "$@"; }}
_staging_bootstrap_exit() {{ :; }}
die() {{ echo "$*" >&2; return 1; }}
sleep() {{ :; }}
curl() {{ echo '{{"queue":{{"active":0}}}}'; }}
_queue_active_after_quiescence() {{ echo 0; }}
python3() {{
  if [[ "$*" == *revision_replica_gate.py* ]]; then
    cat >/dev/null
    n=$(cat "$WORK/gate" 2>/dev/null || echo 0); echo $((n+1)) > "$WORK/gate"
    if [ "$n" = 0 ]; then echo 'true 1 0'; else echo 'true 0 0'; fi
  elif [[ "$*" == *db_session_gate.py* ]]; then :
  elif [[ "$*" == *queue_activity_gate.py* ]]; then cat >/dev/null; echo 0
  else "$REAL_PYTHON" "$@"; fi
}}
az() {{
  case "$*" in
    *"containers[0].image"*) echo old:image ;;
    *"[?properties.active=="*"| [0]"*) echo old ;;
    *"containerapp update"*) : ;;
    *"provisioningState"*)
      n=$(cat "$WORK/show" 2>/dev/null || echo 0); echo $((n+1)) > "$WORK/show"
      if [ "$n" = 0 ]; then echo '{{"provisioning":"Succeeded","min_replicas":1,"revision":"old"}}'
      else echo '{{"provisioning":"Succeeded","min_replicas":0,"revision":"config"}}'; fi ;;
    *latestRevisionName*) echo old ;;
    *"scale.minReplicas"*) echo 1 ;;
    *"revision deactivate"*) echo "$*" >> "$WORK/deactivated" ;;
    *"[?properties.active=="*"].name"*) printf 'old\\nconfig\\n' ;;
    *"revision list"*"-o json"*)
      n=$(cat "$WORK/list" 2>/dev/null || echo 0); echo $((n+1)) > "$WORK/list"
      if [ "$n" = 0 ]; then echo '[{{"name":"old","properties":{{"active":false}}}},{{"name":"config","properties":{{"active":true}}}}]'
      else echo '[{{"name":"old","properties":{{"active":false}}}},{{"name":"config","properties":{{"active":false}}}}]'; fi ;;
    *) echo "unexpected az: $*" >&2; return 1 ;;
  esac
}}
_quiesce_staging_workers
STAGING_WORKERS_QUIESCED=0; trap - EXIT
grep -- '--revision config' "$WORK/deactivated"
''')
    result = subprocess.run(['bash', str(harness), str(tmp_path), str(ROOT)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
