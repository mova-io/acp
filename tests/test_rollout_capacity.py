import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy" / "public"))
from rollout_capacity import plan  # noqa: E402


TARGET = {"api": 8, "discovery": 5, "assess": 5, "remediate": 5, "release": 6}
CURRENT = {"api": 16, "discovery": 18, "assess": 18, "remediate": 18, "release": 6}


def test_staging_target_fits_exactly_with_external_headroom():
    result = plan(maximum=50, reserved=10, external=3,
                  current=TARGET, target=TARGET)
    assert result == {
        "usable": 40, "target_steady": 29, "largest_cohort": 8,
        "external": 3, "sequential_peak": 40,
        "current_rolling_peak": 40, "bootstrap_required": False,
    }


def test_current_fleet_cannot_be_misrepresented_as_target_during_migration():
    result = plan(maximum=50, reserved=10, external=3,
                  current=CURRENT, target=TARGET)
    assert result["current_rolling_peak"] == 87
    assert result["bootstrap_required"] is True


def test_simultaneous_current_rollout_reproduces_the_failure_envelope():
    assert sum(CURRENT.values()) == 76
    assert sum(CURRENT.values()) * 2 > 50 - 10


def test_target_larger_than_the_boundary_fails_closed():
    too_large = dict(TARGET, api=9)
    with pytest.raises(ValueError, match="needs 42 ordinary"):
        plan(maximum=50, reserved=10, external=3,
             current=TARGET, target=too_large)


@pytest.mark.parametrize("maximum,reserved", [(10, 10), (10, 11)])
def test_no_ordinary_server_capacity_is_invalid(maximum, reserved):
    with pytest.raises(ValueError, match="invalid server capacity"):
        plan(maximum=maximum, reserved=reserved, external=0,
             current={"api": 1}, target={"api": 1})


def test_cli_emits_a_machine_readable_bootstrap_decision():
    result = subprocess.run([
        sys.executable, str(ROOT / "deploy/public/rollout_capacity.py"),
        "--maximum", "50", "--reserved", "10", "--external", "3",
        "--current", json.dumps(CURRENT), "--target", json.dumps(TARGET),
    ], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["bootstrap_required"] is True
