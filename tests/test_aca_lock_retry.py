"""A busy ACA must be waited out, not treated as a failed deploy.

WHAT WENT WRONG, measured 2026-09-02. Azure serialises modifications per container app, and
refuses a second write while one is provisioning:

    (ContainerAppOperationInProgress) Cannot modify a container app 'acp-discovery' because
    there is an active provisioning operation in progress. OperationId: '...'

`redeploy.sh` runs under `set -euo pipefail` and updated the app and the three lane workers in a
loop with no retry, so that refusal killed the job MID-LOOP. It happened twice in six minutes
(runs 33579832625 and 33580168055, deploying ca4d6e5d and 0f096be0): acp-app's update was
accepted, acp-discovery's was refused, and the deploy died between them.

THE RESULT IS THE PART WORTH KNOWING. Production ran the API on 2026.9.1.48 and all three workers
on 2026.9.1.41 for over half an hour, and NOTHING SAID SO. `/healthz` 200. `/readyz` 200,
`capacity_state: ready`, `degraded: []`, every worker heartbeating within seconds. The OCR fix
that had just merged was live in the API and absent from the workers that actually run scans. The
mixed-version state redeploy.sh's own header calls out as the thing to prevent was reached by the
guard against it aborting.

WHAT THIS FILE PINS, and why each is a loop property rather than a spelling:

  * a lock is retried and the deploy proceeds — the fix;
  * a REAL error is not retried, at all, on the first attempt. A retry loop that cannot tell a
    lock from a bad image turns a five-second failure into a three-minute one and then reports
    the timeout instead of the cause;
  * the wait is BOUNDED — "retry until it works" on a genuinely stuck app is a hung job;
  * every mutating az call in redeploy.sh goes through it, so the next lock does not simply move
    to whichever call was left bare.

The retry existed before this, in _readiness_probe_retry, matching only "conflicting concurrent
write" — so the readiness probe survived a busy ACA and the image update beside it did not. There
is now one matcher, and this file covers both spellings.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "deploy" / "public" / "readiness_probe.sh"
REDEPLOY = ROOT / "deploy" / "public" / "redeploy.sh"
HARNESS = ROOT / "tests" / "aca_retry_harness.sh"

LOCK = ("(ContainerAppOperationInProgress) Cannot modify a container app 'acp-discovery' "
        "because there is an active provisioning operation in progress. OperationId: 'abc'")
OLD_LOCK = "Operation failed: conflicting concurrent write on the container app"
REAL_ERROR = "(ImageNotFound) The image 'acp:doesnotexist' could not be pulled"


def _run(fail_count: int, error_text: str, attempts: int = 4) -> tuple[int, int]:
    """Returns (calls, exit_code) from one harness run."""
    proc = subprocess.run(
        ["bash", str(HARNESS), str(PROBE), str(fail_count), error_text],
        capture_output=True, text=True, timeout=120,
        env={"PATH": "/usr/bin:/bin", "ACP_ACA_RETRY_ATTEMPTS": str(attempts),
             "ACP_ACA_RETRY_SLEEP": "0"},
    )
    m = re.search(r"calls=(\d+) exit=(\d+)", proc.stdout)
    assert m, f"harness produced no verdict.\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
    return int(m.group(1)), int(m.group(2))


# ── the premise ───────────────────────────────────────────────────────────────

def test_the_harness_can_observe_both_outcomes():
    """If the stub could not fail, or could not succeed, every test below would be vacuous."""
    assert _run(0, LOCK) == (1, 0), "a command that never fails should run once and succeed"
    calls, rc = _run(99, REAL_ERROR)
    assert rc != 0 and calls >= 1, "a command that always fails should be observable as failing"


# ── the fix ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,label", [(LOCK, "ContainerAppOperationInProgress"),
                                        (OLD_LOCK, "conflicting concurrent write")])
def test_a_lock_is_waited_out_and_the_deploy_proceeds(text, label):
    calls, rc = _run(2, text)
    assert rc == 0, f"{label} was treated as a failed deploy"
    assert calls == 3, f"expected 2 refusals then a success, got {calls} attempts"


def test_a_real_error_is_not_retried_even_once():
    """The property that keeps the loop honest. A bad image, an expired credential or a typo'd
    app name must fail on the FIRST attempt: retrying them costs the full wait and then reports
    the timeout instead of the cause."""
    calls, rc = _run(99, REAL_ERROR)
    assert rc != 0, "a real error was swallowed"
    assert calls == 1, f"a non-lock error was retried {calls} times"


def test_the_wait_is_bounded():
    """"Retry until it works" on a genuinely stuck app is a hung deploy, not a resilient one."""
    calls, rc = _run(99, LOCK, attempts=4)
    assert rc != 0, "an ACA that never frees up must eventually fail the deploy"
    assert calls == 4, f"expected exactly the 4 configured attempts, got {calls}"


def test_the_attempt_count_is_configurable_and_actually_read():
    """Otherwise the bound above is a coincidence of the default rather than a setting."""
    assert _run(99, LOCK, attempts=2)[0] == 2
    assert _run(99, LOCK, attempts=7)[0] == 7


# ── every mutating call goes through it ───────────────────────────────────────

def _executable_code(src: str) -> str:
    """redeploy.sh with comments AND here-doc bodies removed.

    Both are prose that happens to contain the strings under test. The here-doc matters here
    specifically: the blue-green path PRINTS a rollback recipe for a human to paste into their own
    terminal —

        az containerapp update -g $RG -n $DISCOVERY_WORKER --image $BLUE_IMG

    — and those three lines are correctly bare, because `_aca_retry` is a function of this script
    and does not exist in the operator's shell. Wrapping them would hand someone a recipe that
    fails with "command not found" at the moment they most need it to work. The first version of
    this test flagged all three, which is the test being wrong rather than the script.
    """
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
    return re.sub(r"<<-?'?(\w+)'?\n.*?^\1$", "", src, flags=re.S | re.M)


_CODE = _executable_code(REDEPLOY.read_text())


def test_no_bare_mutating_containerapp_call_remains():
    """A retry on five of six call sites leaves the sixth to fail the same way, and the next
    incident reads identically to this one."""
    bare = [ln.strip() for ln in _CODE.splitlines()
            if re.search(r"^\s*az containerapp (update|revision set-mode)\b", ln)]
    assert not bare, "these mutate a container app without the lock retry:\n  " + "\n  ".join(bare)


def test_the_step_8_updates_are_the_ones_wrapped():
    """Named specifically, because step 8 is where it actually died and a future refactor that
    moves the loop must not quietly drop the wrapper."""
    m = re.search(r'say "updating \$APP.*?\ndone', _CODE, re.S)
    assert m, "could not find step 8's concurrent update block"
    block = m.group(0)
    assert block.count('_aca_retry python3 "$SRC_ROOT/deploy/public/update_api_image.py"') == 1, block
    assert '_update_lane_worker "$a"' in block
    helper = (ROOT / "deploy/public" / "remediation_scaler.sh").read_text()
    assert '_aca_retry az containerapp update' in helper
    assert '_aca_retry az rest --method patch' in helper


def test_the_readiness_probe_still_has_its_own_name():
    """deploy.sh (the first-deploy path) calls _readiness_probe_retry. Renaming it out from under
    that file would break the other deploy path in a way no test here would notice."""
    probe_src = PROBE.read_text()
    assert "_readiness_probe_retry()" in probe_src
    assert "_readiness_probe_retry" in (ROOT / "deploy" / "public" / "readiness_probe.sh").read_text()


def test_both_deploy_scripts_still_parse():
    """`bash -n` cannot catch an unbound variable, but it does catch the unbalanced quote or
    missing `fi` that an edit to a 700-line shell script most easily introduces."""
    for script in (PROBE, REDEPLOY, ROOT / "deploy" / "public" / "deploy.sh"):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"{script.name}: {r.stderr}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
