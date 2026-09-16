"""A routine release must not kill document-sized worker jobs after 30 seconds."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_rollouts_allow_the_pool_to_drain():
    script = (ROOT / "deploy/public/redeploy.sh").read_text()
    assert 'WORKER_TERMINATION_GRACE_SECONDS="${ACP_WORKER_TERMINATION_GRACE_SECONDS:-600}"' in script
    assert 'WORKER_DRAIN_SECONDS="${ACP_WORKER_DRAIN_SECONDS:-540}"' in script
    # Blue-green, production concurrent, and staging sequential paths all keep this contract.
    assert script.count('_update_lane_worker "$a"') == 3
    helper = (ROOT / "deploy/public/remediation_scaler.sh").read_text()
    assert '--termination-grace-period "$WORKER_TERMINATION_GRACE_SECONDS"' in helper
    assert '"$WORKER_TERMINATION_GRACE_SECONDS" "$WORKER_DRAIN_SECONDS"' in helper
    assert 'ACP_SHUTDOWN_DRAIN_SECONDS=$WORKER_DRAIN_SECONDS' in helper


def test_application_drain_finishes_before_platform_deadline():
    core = (ROOT / "api/core.py").read_text()
    assert 'ACP_SHUTDOWN_DRAIN_SECONDS", "20"' in core
