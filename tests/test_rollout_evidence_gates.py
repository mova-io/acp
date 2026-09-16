import io
import json
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


def test_api_convergence_failure_restores_and_verifies_old_api(tmp_path):
    source = (ROOT / 'deploy/public/redeploy.sh').read_text()
    function = source.split('_restore_staging_workers() {', 1)[1].split('\n_staging_bootstrap_exit()', 1)[0]
    harness = tmp_path / 'recover.sh'
    harness.write_text('set -euo pipefail\n_restore_staging_workers() {' + function + '''
STAGING_WORKERS_QUIESCED=1; STAGING_API_TARGET_READY=0
STAGING_OLD_API_IMAGE=old:image; LANE_WORKERS=(); STAGING_OLD_IMAGES=(); STAGING_OLD_MINS=()
AZ=(fake); RG=g; APP=api; SUB=s; SRC_ROOT="$1"; API_ENV_VARS=(ACP_DB_MAX_CONN=8)
LOG="$2/recovery-log"
WORKER_TERMINATION_GRACE_SECONDS=600; WORKER_DRAIN_SECONDS=540; RELEASE_WORKER=release
_aca_retry() { "$@"; }
_wait_recovery_cohort() { echo "$*" >> "$LOG"; return 0; }
sleep() { :; }
python3() { echo "$*" >> "$LOG"; }
az() {
  case "$*" in
    *latestReadyRevisionName*) echo oldrev ;;
    *latestRevisionName*) echo oldrev ;;
    *"containers[0].image"*) echo old:image ;;
    *"revision list"*) echo 1 ;;
    *"replica list"*) echo 1 ;;
    *) return 1 ;;
  esac
}
_restore_staging_workers
grep -- '--image old:image' "$2/recovery-log"
''')
    result = subprocess.run(['bash', str(harness), str(ROOT), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'api oldrev old:image 1' in (tmp_path / 'recovery-log').read_text()


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
