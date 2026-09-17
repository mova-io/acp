"""Production deploys are MANUAL by default; the automatic trigger is opt-in and off.

`deploy.yml` keeps its `workflow_run` trigger but gates the JOB on the repository variable
`PRODUCTION_AUTO_DEPLOY_ENABLED`, which is unset. The workflow itself stays ENABLED, because a
disabled workflow cannot be dispatched either — switching the automatic trigger off in settings
also removes the manual button.

These tests EVALUATE the gate rather than grep for it. A `"PRODUCTION_AUTO_DEPLOY_ENABLED" in
job["if"]` assertion would pass for `!= '1'`, for a clause ORed where it should be ANDed, and for
one that also disarmed the manual dispatch. So the `if` is rendered against concrete
event x variable contexts and the run/skip decision is compared.

`_evaluate` models one property that matters here and is easy to get backwards: **GitHub compares
strings case-insensitively**. That is why the gate is `'1'` — a digit has no case to argue about,
where a `'true'` gate would also accept `'TRUE'` and `'True'`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"
VAR = "PRODUCTION_AUTO_DEPLOY_ENABLED"


def _evaluate(expression: str, context: dict[str, object]):
    """Render a GitHub expression body against a concrete context.

    Narrow by design: `==`, `&&`, `||`, parens, single-quoted strings and context paths, which is
    everything the three-clause condition under test uses. Unknown syntax raises rather than being
    guessed at.

    Two GitHub behaviours are modelled deliberately. An unknown path is `''` — `vars.X` for a
    variable nobody created, and `github.event.workflow_run.conclusion` on a dispatch, where the
    whole `workflow_run` object is absent. And `==` between two strings ignores case.
    """
    def token(match: re.Match[str]) -> str:
        text = match.group()
        if text in ("&&", "||"):
            return {"&&": " and ", "||": " or "}[text]
        if text == "==":
            return " == "
        if text in "()":
            return text
        if text.startswith("'"):
            return f"_ci({text})"
        return f"_ci({context.get(text, '')!r})"

    python = re.sub(r"'[^']*'|&&|\|\||==|[()]|[A-Za-z_][\w.]*", token, expression)
    return eval(python, {"__builtins__": {}}, {"_ci": _CaseInsensitive})  # noqa: S307


class _CaseInsensitive:
    """A value whose `==` ignores case for strings, as GitHub's does, and is falsy when empty."""

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        a, b = self.value, getattr(other, "value", other)
        if isinstance(a, str) and isinstance(b, str):
            return a.casefold() == b.casefold()
        return a == b

    def __bool__(self):
        return bool(self.value)


def _context(*, event: str, conclusion: str = "", opt_in: str | None = None) -> dict[str, object]:
    context: dict[str, object] = {
        "github.event_name": event,
        "github.event.workflow_run.conclusion": conclusion,
    }
    # `opt_in=None` means the variable does not exist -- left OUT of the mapping, so the absent
    # case exercises the `''` default rather than a value the test handed it.
    if opt_in is not None:
        context[f"vars.{VAR}"] = opt_in
    return context


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(DEPLOY.read_text())


@pytest.fixture(scope="module")
def triggers(workflow: dict) -> dict:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1 truthiness).
    return workflow[True] if True in workflow else workflow["on"]


@pytest.fixture(scope="module")
def job(workflow: dict) -> dict:
    return workflow["jobs"]["deploy"]


@pytest.fixture(scope="module")
def steps(job: dict) -> dict:
    return {step.get("name") or step.get("uses"): step for step in job["steps"]}


@pytest.fixture(scope="module")
def fires(job: dict):
    def _fires(**kwargs) -> bool:
        return bool(_evaluate(job["if"], _context(**kwargs)))

    return _fires


def test_the_evaluator_matches_githubs_case_insensitive_equality():
    """Guards the guard: if this were case-SENSITIVE, the table below would misreport `'TRUE'`.

    GitHub's expression `==` ignores case when both operands are strings, so a gate written
    against `'true'` would accept `'TRUE'` and `'True'` too. The gate under test is `'1'`
    precisely so that question never arises -- but the evaluator has to have the real behaviour,
    or a passing test here would be describing a GitHub that does not exist.
    """
    assert _evaluate("a == 'true'", {"a": "TRUE"})
    assert _evaluate("a == 'true'", {"a": "True"})
    assert not _evaluate("a == '1'", {"a": "true"})
    assert not _evaluate("a == '1'", {"a": ""})


# -- the opt-in, in every state it can be in ------------------------------------------------

@pytest.mark.parametrize(
    "opt_in",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("0", id="zero"),
        pytest.param("false", id="false"),
        pytest.param("true", id="true-is-not-the-opt-in-value"),
        pytest.param("TRUE", id="TRUE-is-not-either"),
        pytest.param("yes", id="yes"),
    ],
)
def test_a_green_merge_does_not_deploy_without_the_opt_in(fires, opt_in):
    """Absent is the default; anything that is not `'1'` means the same thing.

    `'true'`/`'TRUE'` are in here as a pair on purpose. Because GitHub's `==` ignores case, a gate
    spelled `'true'` could not distinguish them -- the two cases would have to behave identically.
    Against a `'1'` gate both are simply off, which is the unambiguous behaviour this change wants.
    """
    assert not fires(event="workflow_run", conclusion="success", opt_in=opt_in)


def test_a_green_merge_deploys_once_the_opt_in_is_set(fires):
    assert fires(event="workflow_run", conclusion="success", opt_in="1")


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", ""])
def test_the_opt_in_does_not_bypass_the_ci_conclusion_gate(fires, conclusion):
    """Opting in re-arms the automatic trigger; it does not lower the bar that trigger clears.

    `workflow_run` fires on `completed`, so a red or CANCELLED CI run reaches this `if` exactly
    like a green one. Cancelled matters on its own -- see `test_ci_concurrency.py`.
    """
    assert not fires(event="workflow_run", conclusion=conclusion, opt_in="1")


# -- the manual path, which must survive all of the above ------------------------------------

@pytest.mark.parametrize(
    "opt_in",
    [pytest.param(None, id="absent"), pytest.param("0", id="zero"), pytest.param("1", id="one")],
)
def test_a_manual_dispatch_always_runs(fires, opt_in):
    """The variable governs the AUTOMATIC trigger only.

    This is the assertion the change exists for. Gating the manual dispatch on the same variable
    would reproduce the disabled-workflow problem it replaces: no automatic deploys, and no way to
    ship either.
    """
    assert fires(event="workflow_dispatch", opt_in=opt_in)


def test_a_manual_dispatch_does_not_read_the_triggering_conclusion(fires):
    """`workflow_run` is absent from a dispatch payload, so its conclusion renders as ''.

    An `if` that ANDed the conclusion across both events would treat that empty string as falsy
    and skip every manual run -- silently, as a skipped job rather than an error.
    """
    assert fires(event="workflow_dispatch", conclusion="", opt_in=None)


def test_both_triggers_are_still_declared(triggers):
    """Arming automatic deploys must be a VARIABLE change, not a workflow edit."""
    assert set(triggers) == {"workflow_run", "workflow_dispatch"}
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    # Keeps a PR branch's own CI completion from triggering a production deploy.
    assert triggers["workflow_run"]["branches"] == ["main"]


def test_the_gate_is_what_stops_the_automatic_deploy(job):
    """A bite check on this file's CLAIM, not on the workflow.

    Every test above would also pass if the automatic branch were unreachable for some unrelated
    reason -- the opt-in could be a no-op and the absent case would still read as "does not
    deploy". So neutralise only the clause naming the variable and confirm the absent case flips
    to RUNNING, while the CI conclusion goes on doing its own separate job.
    """
    without_the_gate = job["if"].replace(f"vars.{VAR} == '1'", "'yes' == 'yes'")
    assert VAR not in without_the_gate

    assert _evaluate(without_the_gate, _context(event="workflow_run", conclusion="success"))
    assert not _evaluate(without_the_gate, _context(event="workflow_run", conclusion="failure"))


# -- the guards a "make it manual" change must not have taken with it ------------------------

def test_the_production_environment_gate_survives(job):
    """Required reviewers live on the environment, not on the trigger."""
    assert job["environment"]["name"] == "production"
    assert job["environment"]["url"] == "https://${{ vars.ACP_FQDN }}"


def test_deploys_still_queue_rather_than_race_or_cancel(workflow):
    """Two deploys at once race the CalVer ordinal; cancelling one mid-traffic-shift is worse."""
    assert workflow["concurrency"]["group"] == "deploy-production"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_manual_inputs_that_carry_the_remaining_guards_survive(triggers):
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"pin", "blue_green", "deploy_with_active_jobs", "skip_ci_gate"}
    # Each is a guard that stays OFF unless the operator asks for it on the dispatch form.
    for name in ("blue_green", "deploy_with_active_jobs", "skip_ci_gate"):
        assert inputs[name]["type"] == "boolean"
        assert inputs[name]["default"] is False


def test_the_manual_pinned_flow_resolves_before_it_checks_out(steps):
    """The pin is resolved ONCE, and both consumers read the resolved value.

    `actions/checkout` takes `ref` verbatim, so an abbreviated sha dies in checkout with a git
    error naming the ref and nothing about why. A manual dispatch is now the only way this
    workflow ships anything, so that path carries all of the traffic rather than some of it.
    """
    resolve = steps["Resolve the pinned ref"]
    assert resolve["id"] == "pin"
    assert resolve["if"] == "inputs.pin != ''"
    assert "^[0-9a-f]{40}$" in resolve["run"]

    checkout = steps["actions/checkout@v4"]
    assert checkout["with"]["ref"] == "${{ steps.pin.outputs.sha || 'main' }}"
    # redeploy.sh clones locally from this checkout and pins a sha inside it.
    assert checkout["with"]["fetch-depth"] == 0

    # The same resolved value, so the checkout and the script cannot pin different commits.
    assert steps["Deploy"]["env"]["ACP_PIN"] == "${{ steps.pin.outputs.sha }}"


@pytest.mark.parametrize(
    "pin,expected",
    [
        pytest.param("", "main", id="unpinned-dispatch-ships-current-main"),
        pytest.param("e2ac2ab1" * 5, "e2ac2ab1" * 5, id="pinned-dispatch-ships-the-pin"),
    ],
)
def test_the_checkout_ref_renders_to_a_ref_checkout_accepts(steps, pin, expected):
    """The `||` fallback, evaluated rather than eyeballed.

    Unpinned, the resolve step is skipped and its output is empty, so this falls through to
    `main` -- main's tip at checkout time, i.e. after any approval wait.
    """
    template = steps["actions/checkout@v4"]["with"]["ref"]
    body = re.fullmatch(r"\$\{\{(.+)\}\}", template).group(1)
    # `||` returns an operand in GitHub exactly as in Python, so the substitution is direct.
    assert _evaluate(body, {"steps.pin.outputs.sha": pin}).value == expected


def test_the_deploy_step_still_runs_the_script_rather_than_reimplementing_it(steps):
    deploy = steps["Deploy"]
    assert deploy["run"].strip() == "bash deploy/public/redeploy.sh"

    env = deploy["env"]
    assert env["ACP_DEPLOY_TARGET_ENV"] == "production"
    # The script's CI gate can only CHECK rather than skip if it has a token to read runs with.
    assert env["GH_TOKEN"] == "${{ github.token }}"
    # Off unless the dispatch form asked for it; `inputs` is empty on an automatic run.
    assert env["ACP_SKIP_CI_GATE"] == "${{ inputs.skip_ci_gate && '1' || '' }}"
    assert env["ACP_DEPLOY_WITH_ACTIVE_JOBS"] == "${{ inputs.deploy_with_active_jobs && '1' || '0' }}"
    assert env["ACP_BLUE_GREEN"] == "${{ inputs.blue_green && '1' || '0' }}"
    # Where the schema preflight and startup gates write their sanitized receipt.
    assert env["ACP_STARTUP_EVIDENCE_PATH"] == "${{ runner.temp }}/acp-startup-evidence.json"


def test_readiness_is_asserted_independently_of_the_script(steps):
    """The script asserts the deploy it just performed; this step is the disinterested check."""
    confirm = steps["Confirm the live app reports the new build"]["run"]
    for route in ("/healthz", "/readyz", "/probe/readyz"):
        assert route in confirm
    assert '"version_stamped":true' in confirm
    # /readyz and the route ACA gates ingress on, both asserted ready.
    assert confirm.count('"ready":true') == 2


def test_the_permissions_the_gates_depend_on_survive(workflow):
    permissions = workflow["permissions"]
    assert permissions["id-token"] == "write"   # OIDC federation, no stored client secret
    assert permissions["actions"] == "read"     # redeploy.sh's CI gate reads the CI conclusion
    assert permissions["contents"] == "read"
