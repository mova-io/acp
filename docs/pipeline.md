# How a change reaches production, and how the matrix keeps up

> **Production worker topology, 2026-09-01:** releases update `acp-app`, `acp-discovery`,
> `acp-assess`, and `acp-remediate` to one image and verify each role's heartbeat. References
> below to a generic production `acp-worker` document the earlier topology or staging. The
> generic production service has been retired.

Two chains start from one event — a PR merged to `acp/main` — and then never touch again.

**Chain A** keeps the public WCAG matrix in step with the code. It is automatic and finishes in
minutes.

**Chain B** puts the code in production. It is `deploy.yml`, and it is **manual by default**:
a person starts it at Actions → deploy → Run workflow, and the `production` GitHub Environment
still gates it, so a reviewer approves before it reaches the container. The automatic
`workflow_run`-on-green-`main` trigger #221/#222 added is still there, but its job is skipped
unless the repository variable `PRODUCTION_AUTO_DEPLOY_ENABLED` is `1`, and it is unset.

That asymmetry used to be the single most important thing on this page: **the matrix could be
perfectly accurate about code that was not deployed.** For most of 2026-07-29 it was, and
automating the *build* alone did not change it — a pipeline nobody triggers ships exactly as much
as no pipeline. The gap closed when the trigger became automatic, not when the workflow file
landed.

Why `workflow_run` and not `push: [main]`: a merge fires CI and the deploy at the same instant,
and `redeploy.sh`'s own gate refuses to ship a commit whose CI has not passed — so a `push`
trigger checks for a CI result that does not exist yet and dies at "no CI run found". #221 did
exactly that on the very merge that enabled it; #222 moved the trigger to the CI workflow's
*completion*, so the gate always sees a finished run.

```
                        ┌──────────────────────────┐
                        │   PR merged to acp/main  │
                        └────────────┬─────────────┘
                                     │
              ┌──────────────────────┴──────────────────────┐
              │                                             │
   ═══════ CHAIN A: matrix sync ═══════        ═══════ CHAIN B: app deploy ═══════
              │  (automatic)                                │  (deploy.yml — MANUAL by default)
              ▼                                             ▼
   matrix-progress-log.yml                        1. pin  PIN=dispatch input, else main's tip
   scans the pushed commits:                         └─ gate: CI on PIN must be green
     (a) Matrix-Note: trailers → count            2. clone to /tmp/acp-deploy, checkout PIN
     (b) capability sources touched?                 └─ the shared checkout is written by many
              │                                         sessions; a build there is unreproducible
     repository_dispatch ──────────┐              3. dotnet build AcpScan.Cli -c Release
     (MATRIX_DISPATCH_TOKEN)       │                 └─ .NET Office analyser → bin/Release/net10.0
              │                    │              4. vendor worker-python  (41 modules)
              ▼                    ▼                 └─ GUARD: abort if < 41
   progress-log.yml         grid-drift.yml            └─ source is NOT in this repo
    gen_progress_log.py      gen_matrix_coverage.py  5. az acr build -r mdkaccessibilityacr
    gen_matrix_coverage.py   check_grid_drift.py        -t acp-app:<sha>-<ts>
    apply_progress_log.py                           6. HEALTH CHECK BEFORE  ← baseline
    apply_maturity.py       drift? → --apply on     7. az containerapp update --image
    bump_build.py             automation/grid-drift        acp-app   ─┐ image only:
              │                    │                       acp-worker ┘ 27 env + 9 secrets survive
              ▼                    ▼                          └─ BOTH, or fixes ship nowhere
   COMMIT → main            OPEN A PR (human)      8. verify /healthz + /readyz
              │                    │
              └────────┬───────────┘
                       ▼
              Netlify auto-deploy
              wcag-matrix.mova-io.app
```

---

## Chain A — matrix sync

### The sender

`.github/workflows/matrix-progress-log.yml`, on every push to `main`. It looks for two
independent signals and fires a `repository_dispatch` for each:

| signal | how it is detected | event |
|---|---|---|
| a commit opted into the public changelog | a `Matrix-Note:` trailer (see `scripts/gen_progress_log.py`) | `acp-progress-log` |
| a capability CEILING may have moved | the push touched a path in `gen_matrix_coverage.py --sources` | `acp-capability-change` |

The second is deliberately **not** opt-in. A ceiling can move without anyone writing a trailer,
and a cell claiming more than the code supports is a correctness bug rather than a changelog
omission.

The dispatch carries **no entry data** — it is a bare "something landed, go look". The generator
therefore lives in exactly one place, there is no 64 KB payload ceiling to design around, and a
dropped dispatch is self-healing because the far side regenerates from full history.

Requires the `MATRIX_DISPATCH_TOKEN` secret: `GITHUB_TOKEN` cannot dispatch across repositories.
Without it the job logs a notice and exits 0 rather than failing a push.

### The receivers

Both clone acp and run **its** generators — the matrix never reimplements them.

**`progress-log.yml`** → `gen_progress_log.py` + `gen_matrix_coverage.py`, spliced by
`apply_progress_log.py` and `apply_maturity.py`, then `bump_build.py`. It **commits straight to
`main`**.

**`grid-drift.yml`** → `check_grid_drift.py` against the freshly derived coverage. On drift it
applies to a branch and **opens a PR**.

### Why one commits and the other asks

| | Progress Log | Grid tiers |
|---|---|---|
| lands as | auto-commit | a PR a human reviews |
| because | a changelog entry is a **fact** — this commit shipped | a tier is a **judgement**, and code cannot make it |

This is not caution for its own sake. On 2026-07-29 the drift guard produced **14** proposals
and **2 were wrong** — one because acp contradicted itself, one because a missing remediation
lane made the tool fall back to an assessment-side value. A bulk auto-apply would have silently
corrupted both cells, and one of them was hiding an engine bug that a human reading the proposal
went on to find.

---

## Chain B — app deploy

**There is a pipeline now: `.github/workflows/deploy.yml`.** It runs `redeploy.sh` on a runner
rather than reimplementing it, so the guards below are the same guards — Actions → deploy → Run
workflow, optionally with a pin and a blue-green toggle.

Two things had to be true first, and only one of them was obvious. The build context had to be
complete from a checkout: the Dockerfile used to copy the PDF engine from a gitignored staging
directory a script filled from *outside* the repo, so a runner could never assemble it. ADR 0029
vendored the engine, which is what unblocked this. And `redeploy.sh` had to resolve `dotnet` from
PATH — `actions/setup-dotnet` never creates `~/.dotnet/dotnet`, so the old hard-coded default
would have failed on the one host CD runs on.

It is **manual by default.** `workflow_dispatch` is always allowed and is how production ships —
Actions → deploy → Run workflow, optionally with a pin, blue-green, or the emergency checkboxes.
`workflow_run` on CI completing on `main` is still declared, but its job is **skipped** unless the
repository variable `PRODUCTION_AUTO_DEPLOY_ENABLED` is `1`. Unset, the default, means no merge
deploys on its own.

The gate is a variable rather than Settings → Actions → deploy → Disable workflow because **a
disabled workflow cannot be dispatched either**: switching the automatic trigger off there also
removes the manual button, and re-enabling it to ship one thing re-arms the automatic trigger in
the same click. So the workflow stays **enabled** and the variable stays **unset**.
`'1'` rather than a `true`/`false` spelling because GitHub's `==` compares strings
case-insensitively — a digit has no near-miss that silently arms production.
`tests/test_deploy_manual_only_default.py` evaluates the job's `if` against each event × variable
combination rather than grepping it.

When the automatic path *is* armed the `if` additionally requires the triggering CI to have
concluded `success`, so a red or cancelled `main` is skipped: opting in re-arms the trigger, it
does not lower the bar that trigger clears.

What SHIPS is a separate question from what fires. Since #238 the checkout `ref` and `ACP_PIN` are
a dispatch's resolved pin, or `main` — main's live tip **at checkout time, after any approval
wait** — not a sha frozen when the run was created. `redeploy.sh`'s own CI gate then refuses
whatever that resolves to unless CI is green on it. The `production` GitHub Environment is what
keeps this safe: required reviewers live there, not in the trigger.

**One-time setup, all of it outside this repo:**

| what | where |
|---|---|
| Azure OIDC federated credential for this repo | an App Registration → Federated credentials |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | repo → Settings → Secrets |
| `ACP_FQDN` (the app hostname, no scheme) | repo → Settings → Variables |
| `production` environment + required reviewers | repo → Settings → Environments |
| the workflow left **enabled** (a disabled one cannot be dispatched) | repo → Actions → deploy |

`PRODUCTION_AUTO_DEPLOY_ENABLED` (repo → Settings → Variables) is optional and deliberately
absent. Set it to `1` to let a green merge deploy again; delete it to return to manual-only. It is
a repository variable rather than an environment one because an environment-scoped variable is not
resolvable in a job `if`.

The service principal needs Contributor on the `mdk-accessibility` resource group and AcrPush on
the registry — no more. OIDC federation is used deliberately over a stored client secret: a
long-lived deploy credential in a repo secret is the thing you least want to rotate under pressure.

Until those exist the workflow fails at `azure/login`, which is the correct failure — it is the
only step that cannot be verified from here.

### Chain B — staging (unattended)

`deploy.yml` is manually *triggered* and human-*approved*: production only moves when someone
dispatches it and clears the `production` gate, so its CalVer legitimately sits behind `main` while an approval waits.
`.github/workflows/deploy-staging.yml` is the complement — it ships the **same green commit** to a
staging container app with **no reviewer gate**, so `main`'s tip is live *somewhere* within
minutes of every merge. It reuses `redeploy.sh` unchanged; the only differences from prod are the
four it sets — no approval, staging app/worker names, staging FQDN, and a dormancy guard.

It is **dormant until provisioned.** The job is gated on `vars.STAGING_FQDN != ''`, so this file
is inert on merge until the staging target exists, then turns on the moment that variable is set.
`redeploy.sh` *updates* apps and health-checks **both** `acp-app-staging` and `acp-worker-staging`
as `Running` before it will deploy — it does not create them. One-time setup, all outside this
repo:

| what | where |
|---|---|
| `acp-app-staging` + `acp-worker-staging` container apps, each with its **own** config and a **separate** `DATABASE_URL` | Azure (`deploy.sh` stands them up) |
| `staging` environment with **no** required reviewers | repo → Settings → Environments |
| `STAGING_FQDN` (staging hostname, no scheme) — also the switch that un-dormants the workflow | repo → Settings → Variables |
| optional `STAGING_RG` / `STAGING_APP` / `STAGING_WORKER` if provisioned under other names | repo → Settings → Variables |

Give staging a **separate database**: both environments pull the same registry image, so a staging
app pointed at the production `DATABASE_URL` would write to production data. The Azure secrets and
OIDC federation are shared with prod — the service principal's Contributor on `mdk-accessibility`
already covers the staging apps in that same group, so no new credential is needed.

### Chain B — staging Azure validation (read-only, plus a separate gated scale test)

Two more `workflow_dispatch`-only workflows sit beside `deploy-staging.yml`, for the same reason
this whole page exists to be read rather than trusted: they exist because the sandbox that
authors changes here cannot reach Azure's management endpoints at all (a hard organization
egress policy, not a credentials problem), so verifying that ACP's own `/control/workers/*`
answers actually match Azure has to happen on a runner instead. Neither runs automatically, and
neither adds a secret — both reuse the OIDC credentials and `ACP_E2E_KEY` already described
above.

**`.github/workflows/validate-staging-azure.yml`** — read-only. Calls Azure directly (via
`azure.mgmt.appcontainers`/`azure.mgmt.monitor`, the same SDK clients `api/routes/control.py`
uses) and ACP's own `GET /control/workers/replicas` / `/capacity` / `/revisions` on staging (via
`x-e2e-key`, same mechanism `smoke-staging.yml` uses), then compares them field by field —
replica min/max, running replicas, active revision, revision health/provisioning state, traffic
split, CPU, memory. Every call is a `get`/`list`/`show`; nothing here can mutate either side. The
comparison logic lives in `scripts/validate_staging_azure.py` (unit-tested in
`tests/test_validate_staging_azure.py`), not embedded in the workflow YAML. Before writing
anything, the script redacts subscription/tenant/client ids and hostnames to stable placeholders
(`<subscription>`, `<hostname>`, …) — see `redact()` in that script. The report uploads as a
`retention-days: 7` artifact, not the default 90. Gated on `vars.STAGING_FQDN != ''` like its
siblings, and on the same worker-app-name safety check described next.

**`.github/workflows/validate-staging-scale-test.yml`** — the one workflow in this pair that
writes. Fully separate from the read-only workflow above; it does **not** run automatically after
it, and requires its own explicit `confirm_scale_test` dispatch input (default `false`) before the
job does anything at all — mirroring `deploy-staging.yml`'s `skip_ci_gate` input pattern. When
confirmed, it records `acp-worker-staging`'s current minimum-replica floor, raises it by exactly
1, polls Azure and ACP's own `GET /jobs` (`worker_tier_pool_size`, added in #1035) for when each
side notices, and reports (best-effort, without manufacturing one) whether the new capacity
claimed a queued job. **The original floor is restored in a step that runs even if an earlier
step failed** (`if: always()`), and that restoration is itself verified and logged — never assumed.

**Safety guarantees common to both:** a hard-fail check — before any Azure call, in either
workflow — that the resolved worker app name ends in `-staging` (`assert_staging_target()` in
`scripts/validate_staging_azure.py`, reused by both workflows via `--check-staging-only` so the
check can't drift between them). Both are `workflow_dispatch`-only with no automatic trigger.
Both upload short-lived, redacted artifacts only. Neither introduces a new secret or a new Azure
role grant — they reuse `deploy-staging.yml`'s existing Contributor-on-`mdk-accessibility`
service principal, read-only in the first workflow's case.

### Chain B — staging diagnostic logging, and read-only deadlock evidence retrieval

Two more `workflow_dispatch`-only workflows, added for the 30 Aug 2026 production incident
(`psycopg2.pool.PoolError: connection pool exhausted` recurring across 5 production revisions).
The incident review found two gaps neither `validate-staging-azure.yml` nor
`validate-staging-scale-test.yml` touches: staging retained **no logs at all** ("no destination;
no environment diagnostic settings"), so staging's historical error rate is unknown, not zero —
and production's own root cause (`Store -> init_schema -> cur.execute(stmt)`, 16 API + 38 worker
terminal `DeadlockDetected` lines) never had its exact conflicting SQL/relation names retrieved.

**`.github/workflows/configure-staging-diagnostics.yml`** — applies (not just proposes) real
Azure diagnostic settings on `acp-app-staging`, `acp-worker-staging`, and staging's PostgreSQL
flexible server, routing all three to a **dedicated staging** Log Analytics workspace
(`acp-staging-logs` by default) with 30-day retention on every log category
`az monitor diagnostic-settings categories list` reports for that resource — none hardcoded, same
"ask Azure, don't guess" discipline `validate_staging_azure.py` already follows for field names.
Deliberately a **separate** workspace from production's
(`3e4c5202-f541-41ea-ab71-a677d91cf38e`), for the same reason staging gets a separate
`DATABASE_URL` above: staging activity — including deadlocks this pair of workflows exists to
provoke and observe — must not mix into production's own retained signal or query results.
Idempotent (checks for an existing diagnostic setting by name before creating), and applies the
same staging-name hard-fail check as its siblings before any Azure call — the Postgres server name
isn't derivable from anything in this repo (`redeploy.sh`/`deploy.sh` only know the two Container
Apps by name; Postgres is reached purely via an opaque `DATABASE_URL`), so it's a required
`workflow_dispatch` input, checked for a `-staging` suffix the same way. **RBAC note:** it runs
under the existing broad Contributor grant on `mdk-accessibility` — narrower `Monitoring
Contributor` + `Log Analytics Contributor` would be better practice; narrowing the identity itself
is a separate, human-executed step, not something this workflow attempts.

**`.github/workflows/staging-deadlock-diagnostics.yml`** — READ-ONLY, zero configuration changes.
Retrieves deadlock/lock-wait evidence from **both** sides via KQL: production's existing workspace
(read-only — explicitly authorized for reading, never writing) using the same
`ContainerAppConsoleLogs_CL` table style the incident review's own evidence bundle used, and
staging's Postgres logs via the workspace the workflow above populates. KQL rather than a direct
`pg_stat_activity`/`pg_locks` connection to staging **on purpose**: a live connection needs a new
Postgres credential secret, and this pair of workflows extends `validate-staging-azure.yml`'s
"zero new secrets" pattern exactly — reusing only the existing OIDC identity. Checks whether the
staging workspace exists yet and reports that plainly rather than failing confusingly deep inside
a query if `configure-staging-diagnostics.yml` hasn't run. **Says explicitly, in its own report
and step summary, that this is diagnostic evidence only** — the single-run migration fix to
`api/store.py`'s schema-init path is separate, later work, not claimed here.

Both share the common safety guarantees the pair above does: `workflow_dispatch`-only, gated on
`vars.STAGING_FQDN != ''`, redaction (`scripts/validate_staging_azure.py`'s `redact()` /
`--redact-file`) before anything is written to disk, and short-lived (`retention-days: 7`)
artifacts.

### Deploying locally

Every image in the registry before this was built from a laptop (`runType: QuickRun`,
`sourceTrigger: null`), and the script is still the way to do it by hand. It behaves identically
to the workflow, and its CI gate degrades to a loud "NOT CHECKED" rather than a silent pass when
`gh` is unavailable:

```bash
./deploy/public/redeploy.sh          # ACP_DRY_RUN=1 to stop before touching Azure
```

It performs exactly the sequence below, with every guard, and adds two things the hand-run
version could not:

**Dependency bases, so a source change stops paying for LibreOffice.** Measured 2026-07-29:
six ACR builds, every one 2m46s–3m14s. Almost none of that was the app — it was the apt layer
(LibreOffice writer/calc/impress plus a *downloaded* .NET 10 runtime), `pip install`, and
`npm install`, re-running every time. `az acr build` has **no `--cache-from`** (checked on az
2.86.0), so a QuickRun cannot reuse layers from a previous build.

Those layers now live in `acp-base-api` / `acp-base-web`, tagged with a hash of *only* their
inputs — `api/requirements.txt`, `frontend/package-lock.json`, and the two `Dockerfile.base-*`
files. Source is deliberately excluded from the hash: if a source edit moved it, the base would
rebuild every time and the split would achieve nothing. The bases rebuild only when that hash
moves, and `redeploy.sh` decides that by asking the registry, so nobody has to remember.

`deploy/public/Dockerfile` still defaults `BASE_WEB`/`BASE_API` to the upstream images, so a
first deploy — or anyone without the bases — builds from scratch exactly as before.

**Both apps updated concurrently.** `--no-wait` on each, then poll both. Sequential updates left
a window where `acp-app` and `acp-worker` ran *different images*, which the rule below forbids.

It also refuses to finish unless `/healthz` reports the new CalVer and `version_stamped: true` —
an image built without the build args runs perfectly well while every surface reports `dev`.

<details><summary>The eight steps it runs</summary>

```bash
PIN=$(git rev-parse origin/main)                 # 1. pin, and check CI is green on it
git clone --local . /tmp/acp-deploy              # 2. isolated tree
cd /tmp/acp-deploy && git checkout "$PIN"

dotnet build spike/dotnet/AcpScan.Cli -c Release # 3. Office analyser
cp -R "$ACP_PDF_ENGINE_SRC" deploy/public/vendor/worker-python
[ "$(find deploy/public/vendor/worker-python -name '*.py' | wc -l)" -ge 41 ] || exit 1   # 4.

az acr build -r mdkaccessibilityacr \            # 5. remote build, no local docker
  -t "acp-app:${PIN:0:7}-$(date +%s)" -f deploy/public/Dockerfile \
  --build-arg BUILD_VERSION=... --build-arg BUILD_TIME=... .

curl -s "$APP/healthz"                           # 6. baseline BEFORE deploying

az containerapp update -n acp-app    -g mdk-accessibility --image "$IMG"   # 7. image only
az containerapp update -n acp-worker -g mdk-accessibility --image "$IMG"

curl -s "$APP/healthz"; curl -s "$APP/readyz"    # 8. verify
```

</details>

### Why each guard exists

Steps 1, 2, 4 and 6 are scar tissue. Every one was added because it failed on 2026-07-29:

| step | what it prevents |
|---|---|
| **1** pin + CI gate | a build from a commit rewritten mid-run — produced an image from a sha that no longer existed, matching how `2553e6d` came to be in production while absent from git history |
| **2** isolated clone | `az acr build` uses the working directory as context, so a shared checkout bakes other sessions' half-finished work into the image. 17 files were uncommitted at the time |
| **4** module guard | an expired ACR token made the vendoring step a silent no-op — **0 modules** — which still builds, shipping an empty PDF engine |
| **6** health before | when the app returned 404 after a deploy there was no way to tell whether it had been broken by it. Both apps turned out to have been `Stopped` beforehand |

### Two things that must stay true

**Both apps move together.** `acp-app` and `acp-worker` run the *same image* and differ only by
entrypoint. Scanning happens in the worker, so updating only the app deploys the fixes nowhere
useful.

**Image-only updates.** `az containerapp update --image` leaves 27 environment variables and 9
secrets intact. Running `deploy.sh` instead would re-derive them from the environment — minting
a **new access code**, blanking `DATABASE_URL` to fall back to SQLite, and dropping most of the
rest. It is the right script for a first deploy and the wrong one for a redeploy.

---

## Self-healing

- **Monday 06:17 UTC cron** on `grid-drift.yml`
- **`workflow_dispatch`** on both receivers — re-syncs from scratch, no range state is kept
- The matrix regenerates from **full history** and de-duplicates by commit hash, so a dropped or
  missed dispatch is caught by the next one

## The guards that keep it honest

| where | guard | fails when |
|---|---|---|
| acp CI | `gen_progress_log.py --check` | rule code changed with no `Matrix-Note:` trailer |
| acp CI | assessment-lane gate | a `human` lane could return a certified PASS |
| acp CI | applier/detector parity | a fix is credited on a criterion no detector emits for that format |
| matrix CI | `check_log_covers_grid.py` | a cell **or ceiling** moved with no new Progress Log entry |
| matrix CI | `check_boot_order.py` | a function running at load reads a `const` declared below it |
| matrix CI | JS parse + CSS balance | the page would render broken |
| matrix | timestamps derived from the commit | — they cannot be typed, so they cannot be invented |

## Known weaknesses

- **No alerting when a deploy fails or is left unapproved.** Chain B now links a merge to the
  container automatically, but nothing pages when the `production` approval sits unactioned, when
  `azure/login` fails, or when a revision comes up healthy yet serves no traffic — production can
  fall behind `main` silently, just later in the chain than before.
- **`worker-python` is vendored, not versioned.** It is not in this repo; step 4 copies it out of
  a running image. If that image is pruned, the chain breaks until the source is located.
- **The dispatch only fires on push to `main`.** Work on a branch is invisible to the matrix
  until it merges.
- **`acp-ollama` is a single always-on replica** at 4 CPU / 8 GiB, with a cold model load of tens
  of seconds.
