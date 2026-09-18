"""End-to-end test: trigger a scan on the live ACP service, verify assessment
correctness and per-rule tracing completeness.

Usage:
  # Local corpus (default):
  python scripts/e2e_test.py

  # Google Drive (server-side ADC, requires ACP_DEMO_DRIVE_KEY deployed):
  python scripts/e2e_test.py --source drive --folder <drive-folder-id> \
      --demo-key <ACP_DEMO_DRIVE_KEY value>

  # Google Drive with a per-user GIS token:
  python scripts/e2e_test.py --source drive --folder <drive-folder-id> \
      --gis-token <access-token>

  # SharePoint / OneDrive (per-user MS Graph token — see below):
  python scripts/e2e_test.py --source sharepoint --site <graph-site-id> \
      --sp-token "$SP_TOKEN"

THE SHAREPOINT TOKEN HAS TO COME FROM A PERSON. SP_SCOPES are DELEGATED
(User.Read, Files.Read.All, Sites.Read.All), so there is no client-credentials path and no
server-side bypass — ACP_DEMO_DRIVE_KEY is Drive-only. Get one from the browser session after
Connect Microsoft, or from `az account get-access-token --resource https://graph.microsoft.com`
IF that principal carries the delegated scopes; an app-only token usually does not, and this
script says so rather than surfacing a bare 401.

WHY SHAREPOINT DOES NOT USE THE EXPECTED TABLE BELOW. That table is ground truth for the
hand-authored local corpus: named files with known issue floors and score ceilings. A real
SharePoint estate has no such ground truth, and pointing this at one would print 18 "expected
file not scanned" warnings and assert nothing about what IS there. So --source sharepoint runs
an ESTATE-shaped check instead: counts by extension, listed-vs-assessed, truncation, and which
provider actually served each AI call. See `estate_assertions()`.

Exit code 0 = all assertions pass, 1 = failures found.
"""
from __future__ import annotations
import argparse, base64, json, os, sys, time
import urllib.request, urllib.error

BASE_URL  = "https://acp-app.greenwater-4bf2c997.eastus2.azurecontainerapps.io"
SOURCE    = "local"
FOLDER    = None
DEMO_KEY  = None
GIS_TOKEN = None
POLL_INTERVAL = 4    # seconds between job status polls
POLL_TIMEOUT  = 360  # max seconds to wait for scan completion (Drive is slower)

ANSI = {
    "red":    "\033[91m",
    "green":  "\033[92m",
    "yellow": "\033[93m",
    "cyan":   "\033[96m",
    "bold":   "\033[1m",
    "reset":  "\033[0m",
}

def c(color: str, s: str) -> str:
    return f"{ANSI[color]}{s}{ANSI['reset']}"

SP_TOKEN = ""  # set via --sp-token or ACP_SP_TOKEN

def _drive_headers() -> dict:
    """Auth headers for the scan request, per source.

    SharePoint's token rides `x-sp-token` (api/routes/scans.py:47) and is per-USER: the API
    refuses `source=sharepoint` outright without one, because SP_SCOPES are delegated and there
    is no server-side equivalent of the Drive demo key.
    """
    h = {}
    if SOURCE == "sharepoint":
        if SP_TOKEN:
            h["x-sp-token"] = SP_TOKEN
        return h
    if GIS_TOKEN:
        h["x-drive-token"] = GIS_TOKEN
    elif DEMO_KEY:
        h["x-demo-key"] = DEMO_KEY
    return h

E2E_KEY = ""  # set via --e2e-key or ACP_E2E_KEY env var

def _auth_headers() -> dict:
    """Authorization header for API calls."""
    if GIS_TOKEN:
        return {"Authorization": f"Bearer {GIS_TOKEN}"}
    if E2E_KEY:
        return {"x-e2e-key": E2E_KEY}
    return {}

def _get(path: str) -> dict | list:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def _post(path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body else b""
    h = {"Content-Type": "application/json", **_auth_headers(), **(headers or {})}
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def _put(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method="PUT",
                                  headers={"Content-Type": "application/json", **_auth_headers()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── Expected outcomes per file (ground truth) ────────────────────────────────
# compliant: True = file should pass conformance, False = should fail, None = flexible
# min_issues: minimum issues expected (scanner detection floor)
# max_score: score ceiling (files with more issues score lower)
# max_issues: upper bound (sanity check for files that should be mostly clean)
#
# Note: PDFs generated via reportlab canvas lack proper PDF/UA tags, so the
# scanner always detects 2-4 structural issues (missing tags, untagged images etc.)
# regardless of content quality. The meaningful check for PDFs is relative:
# critical PDF has ≥ same issues as moderate PDF, and both fail conformance.
# DOCX/PPTX scoring is more discriminating because the .NET engine parses structure.

EXPECTED: dict[str, dict] = {
    # CRITICAL tier — multiple failures across Level A criteria
    "pdf-critical-untagged-no-lang.pdf":  {"min_issues": 2,  "max_score": 80},
    "docx-critical-no-headings.docx":     {"min_issues": 1,  "max_score": 70},
    "pptx-critical-no-titles.pptx":       {"min_issues": 2,  "max_score": 55},

    # SERIOUS tier — specific high-impact failures
    "pdf-serious-contrast.pdf":           {"min_issues": 1,  "max_score": 80},
    "docx-serious-ambiguous-links.docx":  {"min_issues": 1,  "max_score": 100},

    # MODERATE tier — partial failures, may pass rubric threshold
    "pdf-moderate-mixed.pdf":             {"min_issues": 0,  "max_score": 100},
    "docx-moderate-skipped-headings.docx":{"min_issues": 0,  "max_score": 100},
    "pptx-moderate-title-only.pptx":      {"min_issues": 0,  "max_score": 100},

    # New synthetic edge-case files
    "pptx-alt-missing-image.pptx":        {"min_issues": 1, "max_score": 90},
    "docx-heading-structure.docx":        {"min_issues": 1, "max_score": 90},
    "xlsx-chart-no-alt.xlsx":             {"min_issues": 1, "max_score": 90},
    "html-partial-violations.html":       {"min_issues": 2, "max_score": 95, "max_issues": 3},
    "docx-all-violations.docx":           {"min_issues": 3, "max_score": 70},

    # BORDERLINE / CLEAN
    "pdf-borderline-contrast.pdf":        {"min_issues": 0},
    "pdf-clean-accessible.pdf":           {"min_issues": 0},
    "docx-clean-accessible.docx":         {"min_issues": 0, "max_issues": 0},   # DOCX is fully clean
    "pptx-clean-accessible.pptx":         {"min_issues": 0},
}

# Ordering assertions: within each format, critical must score ≤ clean
ORDERING_CHECKS = [
    # (lower_score_file, higher_score_file, label)
    ("pptx-critical-no-titles.pptx",  "pptx-clean-accessible.pptx",  "PPTX critical < clean"),
    ("docx-critical-no-headings.docx","docx-clean-accessible.docx",   "DOCX critical < clean"),
]

def estate_assertions(scan: dict, scan_id: str, expect_per_type: int,
                      failures: list, warnings: list) -> None:
    """What is checkable on an estate nobody hand-authored ground truth for.

    The EXPECTED table above cannot help here, so this asserts the four things that actually go
    wrong on a real source — each chosen because its failure mode is a NUMBER THAT LOOKS FINE:

      1. counts by extension     one bucket coming back empty is invisible in a total. "300
                                 files" reads as success whether or not any xlsx were listed.
      2. listed vs assessed      ADR 0020 defers analysis: Discover lists and persists inventory
                                 and STOPS; Assess opens files. So listed > assessed is correct
                                 by design, and printing both stops it reading as a loss.
      3. truncation              a capped run reports a clean, smaller number. This is the one
                                 failure that cannot be spotted from the result alone.
      4. provider and zone       from the per-call ledger. Configuration says what SHOULD run;
                                 ai_calls says what did. The gap between them is the silent
                                 GPU→CPU fallback this whole preflight/provenance effort exists
                                 to make visible.
    """
    files = scan.get("files", [])
    scope = scan.get("scope") or {}
    inv   = scope.get("inventory") or {}

    # 1. by extension
    by_ext: dict[str, int] = {}
    for f in files:
        ext = (f.get("file", "").rsplit(".", 1) + [""])[1].lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
    print(f"  assessed {len(files)} file(s) across {len(by_ext)} type(s)")
    for ext, n in sorted(by_ext.items(), key=lambda kv: -kv[1]):
        print(f"    .{ext:<6} {n:>4}")
    if expect_per_type:
        for ext in ("docx", "xlsx", "pptx", "pdf"):
            n = by_ext.get(ext, 0)
            if n == 0:
                failures.append(f"No .{ext} files in the scan — the type is missing entirely, "
                                f"which a total would have hidden")
                print(c("red", f"  ✗ .{ext}: none found"))
            elif n < expect_per_type:
                warnings.append(f".{ext}: {n} < expected {expect_per_type}")
                print(c("yellow", f"  ? .{ext}: {n} (expected ~{expect_per_type})"))

    # 2. listed vs assessed — the deferred-analysis gap, stated rather than implied
    listed = inv.get("discovered") or scope.get("kept")
    if listed is not None:
        print(f"  discovered {listed} · assessed {len(files)}")
        if len(files) > (listed or 0):
            failures.append(f"assessed ({len(files)}) exceeds discovered ({listed}) — the "
                            f"denominators disagree, so one of them is wrong")

    # 3. truncation — the number that looks clean because it was cut
    if inv.get("truncated") or scope.get("truncated"):
        failures.append("The listing was TRUNCATED: the counts above describe a capped subset, "
                        "not the estate. Raise ACP_FANOUT_MAX_FILES and re-run before reading "
                        "any of them as coverage.")
        print(c("red", "  ✗ listing truncated — counts describe a capped subset"))
    else:
        print(c("green", "  ✓ listing not truncated"))

    # 4. what actually served the AI calls
    try:
        calls = _get(f"/scans/{scan_id}/ai_calls")
    except Exception as e:
        warnings.append(f"could not read the ai_calls ledger: {e}")
        return
    if not calls:
        print(c("yellow", "  ? no AI calls recorded — expected if no document held an "
                          "undescribed image (only 1.1.1 remediation uses vision)"))
        return
    seen: dict[str, int] = {}
    for cl in calls:
        k = f"{cl.get('provider','?')}/{cl.get('processing_zone') or cl.get('zone','?')}"
        seen[k] = seen.get(k, 0) + 1
    for k, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<28} {n:>4} call(s)")
    if any(k.startswith("ollama/") for k in seen) and len(seen) > 1:
        warnings.append(f"Mixed providers served this scan ({', '.join(seen)}) — some calls fell "
                        f"back to the local floor. Configuration alone would not have shown this.")


RULE_CATALOG_IDS = {
    "1.1.1","1.3.1","1.4.1","1.4.3","1.4.4","1.4.10","1.4.11","1.4.12",
    "2.1.1","2.4.2","2.4.3","2.4.4","2.4.6","2.4.7","3.1.1","3.1.4","4.1.2",
}

def main():
    global BASE_URL, SOURCE, FOLDER, DEMO_KEY, GIS_TOKEN
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url",   default=BASE_URL)
    parser.add_argument("--source",     default="local",
                        choices=["local", "drive", "sharepoint"])
    parser.add_argument("--folder",     default=None,    help="Drive folder ID to scan")
    # The API reuses `folder` to carry the SharePoint SITE id (scanner.py:1140), so --site is an
    # alias rather than a second parameter — one knob on the wire, the readable name here.
    parser.add_argument("--site",       default=None,
                        help="SharePoint site id (omit for OneDrive). Sent as `folder`.")
    parser.add_argument("--sp-token",   default=os.environ.get("ACP_SP_TOKEN", ""),
                        help="Per-user MS Graph token (delegated). Or set ACP_SP_TOKEN.")
    parser.add_argument("--expect-per-type", type=int, default=0,
                        help="Expected count per document type, e.g. 50. 0 disables the check.")
    parser.add_argument("--demo-key",   default=None,    help="ACP_DEMO_DRIVE_KEY value")
    parser.add_argument("--gis-token",  default=None,    help="Per-user GIS access token")
    parser.add_argument("--langfuse-url", default=os.environ.get("LANGFUSE_HOST", ""),
                        help="Langfuse base URL (or set LANGFUSE_HOST env var)")
    parser.add_argument("--langfuse-pk",  default=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                        help="Langfuse public key (or set LANGFUSE_PUBLIC_KEY)")
    parser.add_argument("--langfuse-sk",  default=os.environ.get("LANGFUSE_SECRET_KEY", ""),
                        help="Langfuse secret key (or set LANGFUSE_SECRET_KEY)")
    parser.add_argument("--e2e-key",      default=os.environ.get("ACP_E2E_KEY", ""),
                        help="X-E2E-Key bypass token (or set ACP_E2E_KEY env var)")
    args = parser.parse_args()
    global E2E_KEY, SP_TOKEN
    BASE_URL  = args.base_url.rstrip("/")
    SOURCE    = args.source
    FOLDER    = args.site if args.source == "sharepoint" else args.folder
    DEMO_KEY  = args.demo_key
    GIS_TOKEN = args.gis_token
    E2E_KEY   = args.e2e_key
    SP_TOKEN  = args.sp_token

    # Fail here, not at the API's 401. The message an operator needs is not "unauthorized" — it
    # is WHY no automated credential will do, which is a design fact about delegated scopes and
    # not something they can fix by trying a different key.
    if SOURCE == "sharepoint" and not SP_TOKEN:
        print(c("red", "\n  ✗ --source sharepoint needs --sp-token (or ACP_SP_TOKEN).\n"))
        print("    SP_SCOPES are DELEGATED — User.Read, Files.Read.All, Sites.Read.All — so a\n"
              "    person signs in and there is no client-credentials path. ACP_DEMO_DRIVE_KEY\n"
              "    is Drive-only and does not apply.\n\n"
              "    Get a token from the browser session after Connect Microsoft, or:\n"
              "      az account get-access-token --resource https://graph.microsoft.com \\\n"
              "        --query accessToken -o tsv\n"
              "    …but only if that principal carries the delegated scopes. An app-only token\n"
              "    authenticates and then returns an empty estate, which reads as 'the site is\n"
              "    empty' rather than as the wrong credential.")
        sys.exit(1)

    failures: list[str] = []
    warnings: list[str] = []

    # ── 1. Health check ──────────────────────────────────────────────────────
    print(c("bold", "\n── 1/6  Health check ───────────────────────────────────────"))
    try:
        h = _get("/healthz")
        assert h.get("ok"), f"healthz returned ok=false: {h}"
        print(c("green", f"  ✓ Service healthy — rubric_hash={h.get('rubric_hash')}"))
    except Exception as e:
        failures.append(f"Health check failed: {e}")
        print(c("red", f"  ✗ {e}"))
        sys.exit(1)  # can't continue without a live service

    # ── 2. Trigger scan (async) ───────────────────────────────────────────────
    src_label = SOURCE + (f"  folder={FOLDER}" if FOLDER else "")
    print(c("bold", f"\n── 2/6  Trigger {src_label} scan ─────────────────────────"))
    qs = f"source={SOURCE}"
    if FOLDER:
        qs += f"&folder={FOLDER}"
    job = _post(f"/scans?{qs}", headers=_drive_headers())
    job_id = job.get("job_id")
    if not job_id:
        failures.append(f"No job_id in POST /scans response: {job}")
        print(c("red", f"  ✗ {failures[-1]}"))
        sys.exit(1)
    print(f"  job_id = {job_id}")

    # ── 3. Poll until done ───────────────────────────────────────────────────
    print(c("bold", "\n── 3/6  Waiting for scan to complete ───────────────────────"))
    elapsed = 0
    scan_id = None
    while elapsed < POLL_TIMEOUT:
        j = _get(f"/scans/jobs/{job_id}")
        phase = j.get("phase","?")
        done  = j.get("done", False)
        print(f"  [{elapsed:>3}s]  phase={phase}  files_done={j.get('files_done',0)}/{j.get('files_found',0)}  current={j.get('current','-')}", end="\r", flush=True)
        if done:
            scan_id = j.get("scan_id")
            error   = j.get("error")
            print()
            if error:
                failures.append(f"Scan job failed: {error}")
                print(c("red", f"  ✗ Scan job error: {error}"))
                sys.exit(1)
            print(c("green", f"  ✓ Scan complete — scan_id={scan_id}  ({elapsed}s)"))
            break
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    else:
        failures.append(f"Scan timed out after {POLL_TIMEOUT}s")
        print(c("red", f"\n  ✗ {failures[-1]}"))
        sys.exit(1)

    # ── 4. Assessment verification ───────────────────────────────────────────
    print(c("bold", "\n── 4/6  Assessment correctness ─────────────────────────────"))
    scan = _get(f"/scans/{scan_id}")
    file_map = {f["file"]: f for f in scan.get("files", [])}
    scanned_names = set(file_map.keys())

    print(f"  {len(scanned_names)} files in scan result")

    # An estate has no per-file ground truth, so the EXPECTED table is not merely unhelpful here
    # — it is actively misleading: every one of its 18 named local-corpus files would report as
    # "not scanned", burying whatever the estate check has to say under warnings about files
    # that were never supposed to be there. Emptying both tables makes the two loops below
    # no-ops, which keeps sections 5 and 6 (rule traces, HITL round-trip) running unchanged —
    # they are source-agnostic and worth having on every source.
    expected, ordering = EXPECTED, ORDERING_CHECKS
    if SOURCE == "sharepoint":
        estate_assertions(scan, scan_id, args.expect_per_type, failures, warnings)
        expected, ordering = {}, []

    for fname, exp in expected.items():
        if fname not in scanned_names:
            warnings.append(f"Expected file not scanned: {fname}")
            print(c("yellow", f"  ? {fname}  — not found in scan (check corpus deployment)"))
            continue
        f = file_map[fname]
        ok = True
        notes = []
        n_issues = len(f.get("issues", []))
        score    = f.get("score") or 0

        if "min_issues" in exp and n_issues < exp["min_issues"]:
            ok = False
            notes.append(f"expected ≥{exp['min_issues']} issues, got {n_issues}")

        if "max_issues" in exp and n_issues > exp["max_issues"]:
            ok = False
            notes.append(f"expected ≤{exp['max_issues']} issues, got {n_issues}")

        if "max_score" in exp and score > exp["max_score"]:
            ok = False
            notes.append(f"expected score≤{exp['max_score']}, got {score}")

        if ok:
            print(c("green", f"  ✓ {fname:<46}  score={score:>4}  issues={n_issues}"))
        else:
            msg = f"Assessment mismatch for {fname}: {'; '.join(notes)}"
            failures.append(msg)
            print(c("red",   f"  ✗ {fname:<46}  score={score:>4}  issues={n_issues}  ← {'; '.join(notes)}"))

    # Ordering checks: lower-quality files must score ≤ higher-quality ones
    print()
    for lo_file, hi_file, label in ordering:
        lo = file_map.get(lo_file)
        hi = file_map.get(hi_file)
        if not lo or not hi:
            warnings.append(f"Ordering check skipped (file missing): {label}")
            continue
        lo_score = lo.get("score") or 0
        hi_score = hi.get("score") or 0
        if lo_score <= hi_score:
            print(c("green", f"  ✓ Ordering: {label}  ({lo_score} ≤ {hi_score})"))
        else:
            msg = f"Ordering violation: {label} — {lo_file}={lo_score} > {hi_file}={hi_score}"
            failures.append(msg)
            print(c("red", f"  ✗ {msg}"))

    # ── 5. Rule trace verification ───────────────────────────────────────────
    print(c("bold", "\n── 5/6  Rule trace completeness ────────────────────────────"))
    traces = _get(f"/scans/{scan_id}/traces")
    trace_by_file: dict[str, dict[str, dict]] = {}
    for t in traces:
        trace_by_file.setdefault(t["file"], {})[t["rule_id"]] = t

    trace_pass = trace_warn = trace_fail = 0
    for fname in scanned_names:
        file_traces = trace_by_file.get(fname, {})
        missing_rules = RULE_CATALOG_IDS - set(file_traces.keys())
        extra_rules   = set(file_traces.keys()) - RULE_CATALOG_IDS

        if missing_rules:
            trace_fail += 1
            msg = f"Missing trace rules for {fname}: {sorted(missing_rules)}"
            failures.append(msg)
            print(c("red", f"  ✗ {fname}  — missing {len(missing_rules)} rule traces"))
        elif extra_rules:
            trace_warn += 1
            warnings.append(f"Extra trace rules for {fname}: {sorted(extra_rules)}")
            print(c("yellow", f"  ? {fname}  — {len(file_traces)} traces (extra: {sorted(extra_rules)})"))
        else:
            trace_pass += 1
            outcomes = {t["outcome"] for t in file_traces.values()}
            print(c("green", f"  ✓ {fname:<42}  {len(file_traces)} rules traced  outcomes={sorted(outcomes)}"))

    # Per-file trace spot-check on critical file
    spot = "pdf-critical-untagged-no-lang.pdf"
    if spot in trace_by_file:
        t = trace_by_file[spot]
        print(f"\n  Spot-check traces for {spot}:")
        for rid in sorted(t.keys()):
            row = t[rid]
            ico = "✓" if row["outcome"] == "PASS" else "✕" if row["outcome"] == "FAIL" else "–"
            print(f"    {ico} {rid:<8}  {row['outcome']:<5}  findings={row['finding_count']}  fix_mode={row['fix_mode']}")
    else:
        warnings.append(f"No traces for spot-check file: {spot}")

    # ── 5b. Verify trace counts add up ──────────────────────────────────────
    expected_traces = len(scanned_names) * len(RULE_CATALOG_IDS)
    actual_traces   = len(traces)
    if actual_traces >= expected_traces * 0.9:  # allow 10% tolerance for edge files
        print(c("green", f"\n  ✓ Total traces: {actual_traces}  (expected ~{expected_traces} for {len(scanned_names)} files × {len(RULE_CATALOG_IDS)} rules)"))
    else:
        msg = f"Trace count low: got {actual_traces}, expected ~{expected_traces}"
        failures.append(msg)
        print(c("red", f"\n  ✗ {msg}"))

    # ── 5c. Langfuse trace verification ─────────────────────────────────────
    print(c("bold", "\n── 5c. Langfuse trace check ────────────────────────────────"))
    lf_url = args.langfuse_url.rstrip("/") if args.langfuse_url else ""
    lf_pk  = args.langfuse_pk
    lf_sk  = args.langfuse_sk
    if lf_url and lf_pk and lf_sk:
        lf_auth = base64.b64encode(f"{lf_pk}:{lf_sk}".encode()).decode()
        try:
            lf_req = urllib.request.Request(
                f"{lf_url}/api/public/traces?page=1&limit=20",
                headers={"Authorization": f"Basic {lf_auth}"},
            )
            with urllib.request.urlopen(lf_req, timeout=15) as r:
                lf_data = json.loads(r.read())
            recent = lf_data.get("data", [])
            matched = [t for t in recent if t.get("id") == scan_id]
            if matched:
                t0 = matched[0]
                print(c("green", f"  ✓ Langfuse trace found: id={t0['id']}  name={t0.get('name')}  observations={t0.get('observationCount', 0)}"))
            elif recent:
                print(c("yellow", f"  ? Langfuse reachable ({len(recent)} recent traces) — scan_id={scan_id} may still be flushing"))
                warnings.append(f"Langfuse trace for scan_id={scan_id} not yet visible (flush lag)")
            else:
                warnings.append("Langfuse returned 0 traces — check LANGFUSE_SECRET_KEY in the container")
                print(c("yellow", "  ? Langfuse has 0 recent traces"))
        except Exception as e:
            warnings.append(f"Langfuse check failed: {e}")
            print(c("yellow", f"  ? Langfuse check skipped: {e}"))
    else:
        print("  – skipped (set LANGFUSE_HOST + LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY env vars to enable)")

    # ── 6. HITL auto-queue round-trip ────────────────────────────────────────
    print(c("bold", "\n── 6/6  HITL queue round-trip ──────────────────────────────"))
    try:
        hitl = _post(f"/hitl/queue/{scan_id}/auto")
        queued_n = hitl.get("queued", 0)
        items = hitl.get("items", [])
        print(f"  POST /hitl/queue/{scan_id}/auto → {queued_n} items queued")
        if queued_n > 0:
            # Spot: approve first item
            first = items[0]
            # An approval names the version it approves (the row as GET /hitl/queue lists it);
            # without it the server answers 409 viewed_version_required and records nothing.
            listed = next(x for x in _get(f"/hitl/queue?scan_id={scan_id}") if x["id"] == first["id"])
            updated = _put(f"/hitl/queue/{first['id']}",
                           {"status": "approved", "reviewer_note": "E2E auto-approval test",
                            "approval_scope": "single",
                            "expected_version": listed.get("decision_version") or 0,
                            "expected_source_revision": listed.get("source_revision"),
                            "expected_proposal_snapshot_ids": listed.get("proposal_snapshot_ids") or [],
                            "expected_corrected_sha256": listed.get("corrected_artifact")})
            assert updated["status"] == "approved", f"Expected approved, got {updated['status']}"
            print(c("green", f"  ✓ Approved item {first['id']} ({first['rule_id']} in {first['file']})"))

            # Read back list
            queue = _get("/hitl/queue")
            approved = [x for x in queue if x["status"] == "approved"]
            assert any(x["id"] == first["id"] for x in approved), "Approved item missing from queue listing"
            print(c("green", f"  ✓ GET /hitl/queue — {len(queue)} total, {len(approved)} approved"))
        else:
            warnings.append("No ai-assisted FAILs queued — either no issues or all passed")
            print(c("yellow", "  ? 0 HITL items queued (ai-assisted rules all PASS or not triggered)"))

        # Idempotency: re-queue should add 0 new items
        hitl2 = _post(f"/hitl/queue/{scan_id}/auto")
        assert hitl2.get("queued", -1) == 0, f"Second queue call added {hitl2.get('queued')} items (should be 0)"
        print(c("green", "  ✓ Idempotency: second auto-queue call added 0 items"))

    except Exception as e:
        failures.append(f"HITL round-trip failed: {e}")
        print(c("red", f"  ✗ {e}"))

    # ── Summary ──────────────────────────────────────────────────────────────
    print(c("bold", "\n══ Summary ═════════════════════════════════════════════════"))
    print(f"  Files scanned:   {len(scanned_names)}")
    print(f"  Trace checks:    {trace_pass} pass  {trace_warn} warn  {trace_fail} fail")
    print(f"  Total traces:    {actual_traces}")
    if warnings:
        print(c("yellow", f"  Warnings ({len(warnings)}):"))
        for w in warnings:
            print(c("yellow", f"    · {w}"))
    if failures:
        print(c("red", f"  FAILURES ({len(failures)}):"))
        for f_ in failures:
            print(c("red", f"    ✗ {f_}"))
        print(c("red", "\n  E2E TEST FAILED"))
        sys.exit(1)
    else:
        print(c("green", f"\n  ✓ ALL CHECKS PASSED"))
        print(f"    Scan URL: {BASE_URL}/scans/{scan_id}")
        print(f"    Traces:   {BASE_URL}/scans/{scan_id}/traces")
        sys.exit(0)


if __name__ == "__main__":
    main()
