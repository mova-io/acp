"""The worker services must be verified by asking THEM, per role — not by asking ACA what it was
told, and not through the shared heartbeat key.

WHAT WENT WRONG, measured on 2026-09-01. Production's app rolled 2026.9.1.12 -> .23 across roughly
eleven deploys. Every one ran `az containerapp update -n acp-worker --image <new>`, confirmed
`properties.template.containers[0].image` matched, and printed `acp-worker ✓`. The live worker tier
reported an image built on 31 August throughout.

TWO GAPS, and this file pins both, plus the trap that turned the first attempt at fixing them into
a check that could never fail.

1. The ✓ read the TEMPLATE — the image ACA was told to run, which equals the image being run only
   if the new revision actually replaces the running replicas. The app never had this problem
   because step 9 verifies it through /healthz. The worker services had no equivalent, so the
   tiers that could drift silently were the ones nothing interrogated.
2. Nothing in this repo asserted the worker services' REVISION MODE. The restore block named $APP
   only, and no script sets their mode either. With no ingress there is no traffic weight to
   strand an old revision at 0% — it just keeps running and keeps claiming jobs.

AND THE TRAP. The obvious check — compare /readyz's `workers.version` against the build — is
WRONG, because that field comes from a single key every worker overwrites; with two services it
reports whichever beat last, so the same deploy would pass or fail at random. The check must read
`workers.roles.<role>.version`. See tests/test_worker_roles_status.py.

Source-level, deliberately: `az` and `curl` cannot run here, and the property under test is which
SURFACE each check interrogates. tests/test_az_subscription_scope.py takes the same approach to
the same file.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REDEPLOY = ROOT / "deploy" / "public" / "redeploy.sh"
SRC = REDEPLOY.read_text()

# Comments stripped for structural assertions: the comments here quote the very strings under
# test (they record the incident), so asserting on prose instead of code is a live risk.
CODE = re.sub(r"^\s*#.*$", "", SRC, flags=re.M)


def _embedded_program() -> str:
    """The python3 -c program step 9b runs, lifted OUT of the script.

    Extracted rather than retyped so these tests exercise the shipped code. A retyped copy is how
    a broken embedded program passes its own test.
    """
    m = re.search(r"python3 -c '\n(.*?)\n' \"\$BUILD_VERSION\" \"\$\{LANE_ROLES\[@\]\}\"", SRC, re.S)
    assert m, "could not find step 9b's embedded program"
    return m.group(1)


def _note_program() -> str:
    """The second, smaller program: the roles that reported but are not ours to deploy."""
    m = re.search(r"_OTHER=\"\$\(printf '%s' \"\$_ROLES_JSON\" \| python3 -c '\n(.*?)\n' ", SRC, re.S)
    assert m, "could not find the not-ours-to-deploy note program"
    return m.group(1)


# The roles this script deploys, read out of the script so the tests move with it.
LANE_ROLES = re.search(r'^LANE_ROLES=\((.*?)\)$', SRC, re.M).group(1).replace('"', '').split()


def _run_extractor(payload: str, want: str = "2026.9.2.1",
                   roles: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _embedded_program(), want,
                           *(LANE_ROLES if roles is None else roles)],
                          input=payload, capture_output=True, text=True)


def _run_note(payload: str, roles: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _note_program(),
                           *(LANE_ROLES if roles is None else roles)],
                          input=payload, capture_output=True, text=True)


def _payload(**roles) -> str:
    """A /readyz body carrying exactly these roles. Values may be a version string (alive) or a
    full dict when the test needs to say something about aliveness."""
    import json
    body = {r: (v if isinstance(v, dict) else {"version": v, "alive": True})
            for r, v in roles.items()}
    return json.dumps({"workers": {"version": "whatever", "roles": body}})


# ── the trap: a check that cannot go red ──────────────────────────────────────────────────
def test_the_embedded_program_actually_compiles():
    """THE bite that mattered. The first version used an f-string containing an escaped quote —

        print(f"{role}={r.get(\\"version\\")}")

    which is a SyntaxError. Python wrote to stderr, the shell swallowed it, `_STALE` came back
    empty, and step 9b printed its ✓ on every input. A check that cannot fail is worse than no
    check, and nothing but running it would have shown that.
    """
    compile(_embedded_program(), "<embedded>", "exec")


def test_the_extractor_reports_a_role_on_the_wrong_build():
    r = _run_extractor(_payload(discovery="2026.8.31.20", assess="2026.9.2.1",
                                remediate="2026.9.2.1"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "discovery=2026.8.31.20"


def test_the_extractor_is_silent_when_every_deployed_role_matches():
    """The ✓ path has to be reachable, or the warning becomes noise everyone learns to skip."""
    r = _run_extractor(_payload(discovery="2026.9.2.1", assess="2026.9.2.1",
                                remediate="2026.9.2.1"))
    assert r.stdout.strip() == "" and r.returncode == 0


def test_the_extractor_reports_every_stale_role_not_just_the_first():
    r = _run_extractor(_payload(discovery="2026.8.31.20", assess="2026.8.31.39",
                                remediate="2026.9.2.1"))
    assert sorted(r.stdout.split()) == ["assess=2026.8.31.39", "discovery=2026.8.31.20"]


def test_a_deployed_role_that_never_reported_is_a_failure_not_a_silence():
    """THE SILENT PASS this rewrite closes. The old program iterated the roles the API returned,
    so a lane worker whose replicas never came up contributed no line at all and read as ✓ — the
    exact failure step 9b exists to catch was the one it could not see."""
    r = _run_extractor(_payload(discovery="2026.9.2.1", assess="2026.9.2.1"))
    assert r.stdout.strip() == "remediate=absent"


def test_a_role_on_the_right_build_that_stopped_beating_is_reported():
    """Right image, no longer alive: one beat then the replica died. Distinct from a wrong
    version, and named differently so the reader does not go looking for an old revision."""
    r = _run_extractor(_payload(discovery="2026.9.2.1", assess="2026.9.2.1",
                                remediate={"version": "2026.9.2.1", "alive": False,
                                           "age_s": 2877.6}))
    assert r.stdout.strip() == "remediate=stale-2877.6s"


def test_the_extractor_reports_an_api_without_the_roles_field():
    """CHANGED DELIBERATELY. This used to read as 'nothing to report' and print ✓ — a third
    can't-go-red hole: a curl that returned an error page, or an API predating the field, was
    indistinguishable from three healthy workers. "We could not ask" is its own answer."""
    r = _run_extractor('{"workers":{"version":"2026.8.31.39"}}')
    assert r.stdout.strip() == "readyz=no-roles-field" and r.returncode == 0


def test_the_extractor_survives_a_roles_error_payload():
    """/readyz reports {"error": ...} when the role read itself failed. The field is a dict, so
    the required roles are simply all missing from it — which is the truth."""
    r = _run_extractor('{"workers":{"roles":{"error":"Boom: x"}}}')
    assert sorted(r.stdout.split()) == ["assess=absent", "discovery=absent", "remediate=absent"]
    assert r.returncode == 0


def test_a_null_version_is_not_treated_as_stale():
    """A heartbeat predating the version field reports null. That is 'unknown', not 'wrong', and
    warning on it would fire against every worker that has not redeployed onto the field yet."""
    r = _run_extractor(_payload(discovery=None, assess="2026.9.2.1", remediate="2026.9.2.1"))
    assert r.stdout.strip() == ""


def test_garbage_input_does_not_crash_the_deploy():
    """curl fails, or returns an error page. `set -euo pipefail` is on."""
    r = _run_extractor("not json at all")
    assert r.returncode == 0 and r.stdout.strip() == "readyz=no-roles-field"


# ── the false alarm nobody could clear ────────────────────────────────────────────────────
def test_a_retired_roles_leftover_heartbeat_does_not_warn():
    """THE REGRESSION FIXTURE, verbatim from production on 2026-09-01.

    `worker_tier_heartbeat:<role>` is a settings row and nothing reaps it when the service that
    wrote it goes away. #1172 retired the generic worker, which ran as ACP_WORKER_ROLE=processing;
    its final beat is still in the table, 48 minutes old and one build behind, and still appears
    in /readyz. Run against this exact payload, the previous program printed

        processing=2026.9.1.29

    and would have on every future deploy, because that row can never catch up. It also burned the
    full 24x5s retry each time, since the condition never clears.
    """
    live = ('{"workers":{"version":"2026.9.1.32","alive":true,"roles":{'
            '"assess":{"alive":true,"version":"2026.9.1.32","age_s":0.2},'
            '"discovery":{"alive":true,"version":"2026.9.1.32","age_s":1.1},'
            '"processing":{"alive":false,"version":"2026.9.1.29","age_s":2877.6},'
            '"remediate":{"alive":true,"version":"2026.9.1.32","age_s":7.7}}}}')
    r = _run_extractor(live, want="2026.9.1.32")
    assert r.stdout.strip() == "", f"a retired role must not warn, got: {r.stdout!r}"


def test_a_role_we_do_not_deploy_is_still_visible_as_a_note():
    """Ignoring it silently would hide a service somebody adds without adding it to LANE_ROLES.
    A note is not a warning: it does not gate the ✓ and it does not tell anyone to act."""
    live = ('{"workers":{"roles":{'
            '"discovery":{"alive":true,"version":"2026.9.1.32"},'
            '"processing":{"alive":false,"version":"2026.9.1.29"}}}}')
    r = _run_note(live)
    assert r.stdout.strip() == "processing=2026.9.1.29 (not beating)"
    assert "discovery" not in r.stdout, "a deployed role is not 'other'"


def test_the_note_program_compiles_too():
    compile(_note_program(), "<note>", "exec")


# ── which surface each check reads ────────────────────────────────────────────────────────
def test_the_worker_check_reads_the_per_role_key_not_the_shared_one():
    """The whole correction. `workers.version` is last-writer-wins across services; comparing it
    against the build passes or fails at random once two services run."""
    assert '["workers"].get("roles")' in SRC, "step 9b must read workers.roles"
    tail = CODE[CODE.index("_ROLES_JSON"):]
    assert not re.search(r'"version":\\"\$BUILD_VERSION', tail), \
        "must not compare the shared workers.version against the build"


def test_the_template_check_still_exists_and_is_a_different_question():
    """Step 8 asks ACA what it was told; step 9b asks the service what it runs. Both belong."""
    assert "properties.template.containers[0].image" in CODE
    assert "_ROLES_JSON" in CODE


def test_a_stale_role_warning_names_the_role_and_the_expected_build():
    """A warning that does not say WHICH role and WHICH versions sends the reader to the console.
    The incident was diagnosable only because both strings were visible together."""
    warn = CODE[CODE.index("_ROLES_JSON"):]
    assert "$BUILD_VERSION" in warn and "_STALE" in warn


def test_the_warning_tells_the_reader_what_to_run_next():
    assert "activeRevisionsMode" in SRC and "revision list" in SRC


def test_the_worker_check_does_not_fail_the_deploy():
    """Deliberate, with the reason written down: the condition is PRE-EXISTING, so dying here
    would red every deploy including the one shipping the cleanup. Making it fatal is the rollout
    owner's call. If this is ever flipped, flip it as a decision rather than by accident."""
    tail = CODE[CODE.index("_ROLES_JSON"):]
    assert "die " not in tail


# ── revision mode, for every app ──────────────────────────────────────────────────────────
def test_single_revision_mode_is_restored_for_every_app_this_script_deploys():
    """The asymmetry that let the worker services drift: this named $APP only.

    ASSERTED AS A PROPERTY, NOT A LITERAL, and that is the point of the rewrite. The previous
    version of this test pinned the exact string `for a in "$APP" "$WORKER" "$DISCOVERY_WORKER"`.
    When #1172 retired the generic worker and deleted $WORKER on 2026-09-01, the loop kept naming
    it, `set -u` killed every deploy at that line, and this test went on passing because the
    broken literal was what it was asserting. Deriving the list from LANE_WORKERS means the loop
    cannot fall behind the set of apps the script actually updates.
    """
    loop = re.search(r'for a in "\$APP" "\$\{LANE_WORKERS\[@\]\}"; do\s*\n\s*MODE=(.*?)\ndone',
                     CODE, re.S)
    assert loop, "revision mode must be restored for $APP and every lane worker in one loop"
    body = loop.group(1)
    assert "revision set-mode" in body and "--mode single" in body
    assert '-n "$a"' in body, "set-mode must target the loop variable, not a hardcoded app"


def test_no_expansion_of_a_variable_the_script_never_defines():
    """THE GUARD THAT WOULD HAVE CAUGHT IT, and the reason the suite did not.

    `bash -n` parses; it cannot see an unbound variable, so test_the_script_still_parses stayed
    green while `set -u` aborted the real deploy at line 433 with

        deploy/public/redeploy.sh: line 433: WORKER: unbound variable

    after the images were updated and before anything was verified. Every other guard in this file
    asserts a specific string, which by construction cannot notice a variable that stopped
    existing somewhere else. This one is general: every $NAME the script expands must be assigned
    in the script, defaulted with ${NAME:-...}, or a shell builtin.
    """
    body = re.sub(r"^\s*#.*$", "", SRC, flags=re.M)  # comments quote the broken line on purpose
    assigned: set[str] = set()
    for pat in (r'^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)\+?=',
                r'^\s*for\s+([A-Za-z_]\w*)\s+in\b',
                r'\bread\s+(?:-\w+\s+)*((?:[A-Za-z_]\w*\s+)*[A-Za-z_]\w*)',
                r'^\s*([A-Za-z_]\w*)\(\)\s*\{'):
        for hit in re.findall(pat, body, re.M):
            assigned |= set(hit.split())
    shell = {"HOME", "PATH", "PWD", "IFS", "RANDOM", "BASH", "BASHPID", "BASH_SOURCE", "LINENO",
             "FUNCNAME", "OLDPWD", "SECONDS", "UID", "EUID", "HOSTNAME", "SHELL", "TMPDIR",
             "USER", "PS1"}
    undefined = set()
    for m in re.finditer(r'\$\{([A-Za-z_]\w*)([^}]*)\}|\$([A-Za-z_]\w*)', body):
        name = m.group(1) or m.group(3)
        defaulted = bool(m.group(2)) and m.group(2)[0] in ":-+="
        if name not in assigned and name not in shell and not defaulted:
            undefined.add(name)
    assert not undefined, f"expanded but never defined (set -u will kill the deploy): {undefined}"


def test_lane_roles_is_paired_with_lane_workers():
    """Step 9b asks by ROLE, step 8 updates by SERVICE NAME, and the two lists are positional
    pairs. Different lengths means a service is deployed and never verified, or a role is demanded
    of a service nobody ships."""
    workers = re.search(r'^LANE_WORKERS=\((.*?)\)$', SRC, re.M).group(1).split()
    assert len(workers) == len(LANE_ROLES), f"{workers} vs {LANE_ROLES}"
    assert LANE_ROLES == ["discovery", "assess", "remediate"]


def test_the_discovery_role_matches_the_dockerfile_that_sets_it():
    """The one pairing this repo can actually verify — acp-assess and acp-remediate take
    ACP_WORKER_ROLE from container-app env vars set outside it, which the script says so."""
    dockerfile = (ROOT / "deploy" / "discovery" / "Dockerfile").read_text()
    assert "ACP_WORKER_ROLE=discovery" in dockerfile
    assert LANE_ROLES[0] == "discovery", "LANE_ROLES[0] pairs with $DISCOVERY_WORKER"


def test_the_mode_restore_defaults_to_single_when_the_query_fails():
    """`|| echo Single` stops a failed read reading as 'Multiple' and triggering a set-mode
    against an app whose state is unknown."""
    assert re.search(r'activeRevisionsMode -o tsv 2>/dev/null \|\| echo Single', CODE)


def test_the_blue_green_path_is_untouched_by_the_restore():
    """Blue-green deliberately LEAVES the app in Multiple mode, keeping blue at 0% for rollback,
    and exits before this block. Restoring Single there would deactivate the rollback target."""
    assert CODE.index("ROLLBACK") < CODE.index('for a in "$APP" "${LANE_WORKERS[@]}"; do\n  MODE=')


def test_blue_green_domain_follows_the_api_apps_exact_environment():
    """Resource groups can contain both staging/GPU and production ACA environments. A list's
    first item is unrelated to the API app and can send the green smoke test across environments."""
    assert "properties.managedEnvironmentId" in CODE
    assert 'containerapp env show "${AZ[@]}" --ids "$environment_id"' in CODE
    assert "containerapp env list" not in CODE
    assert CODE.count('ENV_DOMAIN="$(app_environment_domain)"') == 2


def test_green_revision_suffix_is_bound_to_the_exact_image():
    """A failed zero-traffic green can retain its immutable revision. A retry must reuse the
    suffix only for the exact same image and create a distinct suffix for a newly tagged build."""
    assert "green_revision_suffix()" in CODE
    assert CODE.count('SUFFIX="$(green_revision_suffix)"') == 2

    helper = re.search(r"green_revision_suffix\(\) \{.*?^\}", CODE, re.MULTILINE | re.DOTALL).group()
    def suffix(image):
        script = f"set -euo pipefail\ndie() {{ exit 97; }}\nIMG={image!r}\n{helper}\ngreen_revision_suffix\n"
        return subprocess.run(["bash"], input=script, text=True, capture_output=True, check=True).stdout

    first = suffix("registry/acp-app:c03ffc8-1789567001")
    assert first == suffix("registry/acp-app:c03ffc8-1789567001")
    assert first != suffix("registry/acp-app:c03ffc8-1789567002")
    assert re.fullmatch(r"[a-z][a-z0-9-]{0,63}", first)


def test_the_script_still_parses():
    assert subprocess.run(["bash", "-n", str(REDEPLOY)]).returncode == 0


# ── one clean poll is not an answer ───────────────────────────────────────────────────────
#
# WHAT WENT WRONG, measured against production on 2026-09-01 (deploy of 2026.9.1.36, run
# 33568634537). Step 9b printed its tick at 23:03:55.5585 while discovery's row read:
#
#     23:03:39.570  beat, version 2026.9.1.30
#    ~23:03:55.5    step 9b polls, sees .36, BREAKS, prints the tick
#     23:03:55.581  beat, version 2026.9.1.30
#     23:03:56.35   the workflow's own /readyz: discovery .30, age 0.5s
#
# Discovery beats every 15s, and .30 was written at :39.57 and :55.58 — one replica's cadence.
# A .36 write between them is a second writer: two replicas, one per image, both writing
# worker_tier_heartbeat:discovery. The role-scoped key is last-writer-wins across REPLICAS, not
# only across services, so a single poll is a sample and breaking on the first clean one makes a
# check that converges on green instead of one that can go red.
#
# These tests drive the REAL loop over a sequence of payloads (see redeploy_step9b_harness.sh);
# the per-payload tests above cannot express a value that changes between polls.
HARNESS = ROOT / "tests" / "redeploy_step9b_harness.sh"

_CLEAN = {"discovery": "2026.9.1.36", "assess": "2026.9.1.36", "remediate": "2026.9.1.36"}
_DISCOVERY_OLD = {**_CLEAN, "discovery": "2026.9.1.30"}


def _payload_file(tmp_path, name: str, versions: dict) -> str:
    import json
    body = {r: {"alive": True, "version": v, "age_s": 1.0} for r, v in versions.items()}
    body["processing"] = {"alive": False, "version": "2026.9.1.29", "age_s": 2877.6}
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"workers": {"version": "whatever", "roles": body}}))
    return str(p)


def _drive(tmp_path, sequence, want="2026.9.1.36", az_revs=None, script=None, cycle=False):
    """Run the shipped loop over `sequence` (a list of version dicts).

    The last payload repeats for further polls; `cycle=True` wraps around instead, which is how a
    run that never settles but ends on a clean poll is expressed.
    """
    files = [_payload_file(tmp_path, f"p{i}", v) for i, v in enumerate(sequence)]
    env = dict(os.environ,
               COUNTER_FILE=str(tmp_path / "counter"),
               CLOCK_FILE=str(tmp_path / "clock"))
    if cycle:
        env["CYCLE"] = "1"
    if az_revs is not None:
        env["AZ_REVS"] = az_revs
    r = subprocess.run(["bash", str(HARNESS), str(script or REDEPLOY), want, *files],
                       capture_output=True, text=True, env=env)
    polls = int((tmp_path / "counter").read_text())
    return r, polls


def test_a_role_that_flaps_between_polls_does_not_pass(tmp_path):
    """THE REGRESSION. Alternating old/new — production's actual behaviour during that deploy —
    must not pass on the poll that happens to look clean."""
    r, polls = _drive(tmp_path, [_DISCOVERY_OLD, _CLEAN, _DISCOVERY_OLD])
    out = r.stdout + r.stderr
    assert "✓" not in out, f"a flapping role passed after {polls} polls:\n{out}"
    assert "discovery=2026.9.1.30" in out
    assert polls > 2, "must keep polling rather than breaking on the first clean sample"


def test_the_settled_case_still_passes(tmp_path):
    """The tick has to stay reachable, or the warning becomes noise everyone learns to skip."""
    r, polls = _drive(tmp_path, [_CLEAN])
    assert "✓" in r.stdout, r.stdout + r.stderr
    assert polls >= 4, "a pass must span more than one 15s heartbeat interval, not one poll"


def test_a_late_settle_passes_once_it_holds(tmp_path):
    """Dirty while the rollout is in progress, then clean: that is a pass, just a later one."""
    r, _ = _drive(tmp_path, [_DISCOVERY_OLD, _DISCOVERY_OLD, _DISCOVERY_OLD, _CLEAN])
    assert "✓" in r.stdout, r.stdout + r.stderr


def test_a_run_that_merely_ENDS_clean_is_not_a_pass(tmp_path):
    """`_STALE` holds only the LAST poll's result, so on exhaustion a run that flapped for two
    minutes and ended on a clean poll leaves it EMPTY — and reading the verdict from it would
    print the tick. That is the same defect one level up from the one being fixed.

    Driven, not asserted structurally: 24 polls over an alternating cycle end on a clean one, so
    this run reaches the end of the loop with `_STALE` empty and `_SUSTAINED` still 0.
    """
    r, polls = _drive(tmp_path, [_DISCOVERY_OLD, _CLEAN], cycle=True)
    out = r.stdout + r.stderr
    assert polls == 24, f"the loop should have exhausted, took {polls} polls"
    assert "✓" not in out, f"a never-clean run that ended clean passed:\n{out}"
    assert "never-clean-for-20s" in out, out


def test_the_clean_span_is_derived_from_the_heartbeat_interval(tmp_path):
    """20s is not a magic number: an old replica beating every 15s must have written at least
    once inside the window for the streak to be able to see it. If worker_main's interval moves,
    this must move with it."""
    src = REDEPLOY.read_text()
    assert "_HEARTBEAT_S=15" in src
    assert "_MIN_CLEAN_SPAN=$(( _HEARTBEAT_S + 5 ))" in src
    worker_main = (ROOT / "api" / "worker_main.py").read_text()
    assert "now - last_beat >= 15" in worker_main, \
        "worker_main's heartbeat interval changed; _HEARTBEAT_S must follow it"


def test_the_pre_fix_script_passes_the_flap_that_the_fix_rejects(tmp_path):
    """THE BITE, shipped. Runs the SAME sequence against the version of this block that was on
    main before the fix, reconstructed by putting the break back. If this ever stops passing on
    the old shape, the sequence no longer reproduces the defect and the test above proves less
    than it claims."""
    old = REDEPLOY.read_text().replace(
        """  if [ -n "$_STALE" ]; then
    _STREAK_START=""            # any dirty poll starts the span over
  else
    [ -n "$_STREAK_START" ] || _STREAK_START="$SECONDS"
    if [ $(( SECONDS - _STREAK_START )) -ge "$_MIN_CLEAN_SPAN" ]; then
      _SUSTAINED=1
      break
    fi
  fi""",
        """  [ -z "$_STALE" ] && { _SUSTAINED=1; break; }""")
    assert "_MIN_CLEAN_SPAN\"" not in old, "the break-on-first-clean shape was not restored"
    scratch = tmp_path / "prefix.sh"
    scratch.write_text(old)
    r, polls = _drive(tmp_path, [_DISCOVERY_OLD, _CLEAN, _DISCOVERY_OLD], script=scratch)
    assert "✓" in r.stdout, "the old shape should pass this flap — that was the bug"
    assert polls == 2, f"it should break on the second (lucky) poll, took {polls}"


# ── the revision count: an answer, where the heartbeat is only a sample ───────────────────
def test_two_active_revisions_fail_even_when_every_heartbeat_looks_clean(tmp_path):
    """The heartbeat cannot answer 'is there exactly one running image' — both replicas write the
    same key. ACA can, and step 8 has already set these apps to Single mode, under which a second
    active revision means that did not take."""
    r, _ = _drive(tmp_path, [_CLEAN], az_revs="2")
    out = r.stdout + r.stderr
    assert "✓" not in out
    assert "acp-discovery=2-active-revisions" in out


def test_an_unreadable_revision_count_is_not_read_as_one(tmp_path):
    """A failed az call must not pass. This is the `|| true` trap: an empty result is 'we could
    not ask', which is a third answer."""
    for bad in ("", "ERROR: not found"):
        r, _ = _drive(tmp_path, [_CLEAN], az_revs=bad)
        out = r.stdout + r.stderr
        assert "✓" not in out, f"az returning {bad!r} passed"
        assert "acp-discovery=unreadable" in out


def test_the_revision_check_covers_every_lane_worker(tmp_path):
    r, _ = _drive(tmp_path, [_CLEAN], az_revs="3")
    out = r.stdout + r.stderr
    for app in ("acp-discovery", "acp-assess", "acp-remediate"):
        assert f"{app}=3-active-revisions" in out


def test_the_harness_actually_advances_its_sequence(tmp_path):
    """A HARNESS BITE. `curl` runs inside $(...), a subshell, so a shell-variable counter never
    reaches the parent and every poll gets payload[0]. The first version of this harness did
    exactly that: it fed both the fixed and the broken script the same payload five times and
    reported a bite it had not performed. If the counter regresses, these sequence tests silently
    stop testing sequences."""
    r, polls = _drive(tmp_path, [_DISCOVERY_OLD, _CLEAN, _DISCOVERY_OLD])
    assert polls >= 3, f"the sequence never advanced past {polls} payload(s)"
    assert "discovery=2026.9.1.30" in (r.stdout + r.stderr)
