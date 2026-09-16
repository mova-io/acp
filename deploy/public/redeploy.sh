#!/usr/bin/env bash
# Redeploy acp to Azure — the FAST path, and the only correct one for an app already running.
#
#   deploy.sh   = FIRST deploy. Re-derives all environment: mints a new access code, blanks
#                 DATABASE_URL back to SQLite, drops most of 27 env vars + 9 secrets.
#   redeploy.sh = THIS. Image only. Every env var and secret survives untouched.
#
# Running deploy.sh against a live app is the mistake this script exists to make unnecessary.
#
# WHAT MAKES IT FAST (measured 2026-07-29: 6/6 ACR builds took 2m46s-3m14s, ~3 min each,
# of which the app's own source is seconds):
#
#   1. The expensive layers are pinned base images keyed on a DEPENDENCY hash — the apt block
#      (LibreOffice + a downloaded .NET 10 runtime), pip install, and npm install. `az acr build`
#      has no --cache-from (checked, az 2.86.0), so a QuickRun cannot reuse layers and was
#      re-running all of that for a one-line CSS change. Bases rebuild only when the hash moves.
#   2. acp-app and all three stage-owned workers are updated CONCURRENTLY (--no-wait, then poll).
#      Sequential updates left a window where stages ran DIFFERENT images; they must move together
#      (same image, different roles — Discovery, Assess, and Remediate own disjoint queues).
#
# Guards, all of which are scar tissue from 2026-07-29 (see docs/pipeline.md):
#   - pin to a sha and refuse to build from a dirty or shared tree
#   - vendored worker-python module-count guard (an expired token made it a silent 0-module no-op)
#   - health check BEFORE, so "it was already broken" is distinguishable from "I broke it"
#   - the build args that stamp the CalVer are non-optional; an unstamped image is rejected
#     rather than quietly serving version "dev"
set -euo pipefail

RG="${ACP_RG:-mdk-accessibility}"
ACR="${ACP_ACR:-mdkaccessibilityacr}"
APP="${ACP_APP:-acp-app}"
DISCOVERY_WORKER="${ACP_DISCOVERY_WORKER:-acp-discovery}"
ASSESS_WORKER="${ACP_ASSESS_WORKER:-acp-assess}"
REMEDIATE_WORKER="${ACP_REMEDIATE_WORKER:-acp-remediate}"
RELEASE_WORKER="${ACP_RELEASE_WORKER:-}"
GPU_APP="${ACP_GPU_APP:-acp-ollama}"
LANE_WORKERS=("$DISCOVERY_WORKER" "$ASSESS_WORKER" "$REMEDIATE_WORKER")
DEPLOY_TARGET_ENV="${ACP_DEPLOY_TARGET_ENV:-production}"
# The ACP_WORKER_ROLE each lane worker runs as, POSITIONALLY paired with LANE_WORKERS above —
# LANE_WORKERS[i] reports its heartbeat under LANE_ROLES[i]. Step 9b needs the role, not the
# service name: the services have no ingress, so the only way to ask one what it is running is
# through the role-scoped key it writes (worker_tier_heartbeat:<role>).
#
# Only acp-discovery's role is set in this repo (deploy/discovery/Dockerfile). The other two get
# ACP_WORKER_ROLE from container-app env vars set outside it, so this list is a convention the
# repo cannot verify end to end — which is exactly why step 9b checks that each named role
# actually reported, instead of trusting whichever roles happen to appear.
LANE_ROLES=("discovery" "assess" "remediate")
DEDICATED_RELEASE=0
if [ -n "$RELEASE_WORKER" ]; then
  LANE_WORKERS+=("$RELEASE_WORKER")
  LANE_ROLES+=("release")
  DEDICATED_RELEASE=1
fi
export ACP_DEDICATED_RELEASE_WORKERS="$DEDICATED_RELEASE"
BUILD_TZ="${BUILD_TZ:-America/Los_Angeles}"
MIN_MODULES=41                  # engine/pdf-analyser is tracked; this guards against truncation
DRY="${ACP_DRY_RUN:-0}"
BG="${ACP_BLUE_GREEN:-0}"       # 1 => green provisions at 0% traffic, is tested, then promoted
ALLOW_ACTIVE_JOBS="${ACP_DEPLOY_WITH_ACTIVE_JOBS:-0}"
STAGING_DEPLOY_MODE="${ACP_STAGING_DEPLOY_MODE:-}"
STAGING_GITHUB_OUTPUT="${GITHUB_OUTPUT:-}"
# Worker jobs are document-sized and production PDFs commonly take 5–7 minutes. ACA's 30-second
# default killed them during ordinary releases; the durable queue then kept their claims until
# lease recovery, making active Remediation appear hung. Worker code drains for 540s, leaving a
# minute for process shutdown before this platform deadline.
WORKER_TERMINATION_GRACE_SECONDS="${ACP_WORKER_TERMINATION_GRACE_SECONDS:-600}"
WORKER_DRAIN_SECONDS="${ACP_WORKER_DRAIN_SECONDS:-540}"
CAPACITY_APPLY_REQUESTED="${ACP_CAPACITY_APPLY_ENABLED:-0}"
ACA_VCPU_QUOTA="${ACP_ACA_VCPU_QUOTA:-}"
PG_MAX_CONNECTIONS="${ACP_PG_MAX_CONNECTIONS:-}"
PG_RESERVED_CONNECTIONS="${ACP_PG_RESERVED_CONNECTIONS:-}"

# Keep the gate installer shared with the first-deploy path. This script is what deploy.yml
# actually executes for production releases.
source "$(cd "$(dirname "$0")" && pwd)/readiness_probe.sh"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# A resource group can contain several Container Apps environments. The revision hostname must
# use THIS app's environment; taking env-list[0] sent a production green smoke test to staging's
# domain and left a healthy green revision unpromoted. Follow the app's exact resource ID so list
# ordering can never cross that boundary.
app_environment_domain() {
  local environment_id domain
  environment_id="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" \
    --query properties.managedEnvironmentId -o tsv)"
  [ -n "$environment_id" ] || die "cannot resolve $APP's Container Apps environment"
  domain="$(az containerapp env show "${AZ[@]}" --ids "$environment_id" \
    --query properties.defaultDomain -o tsv)"
  [ -n "$domain" ] || die "cannot resolve $APP's Container Apps environment domain"
  printf '%s' "$domain"
}

# CalVer can repeat, and even the same commit can be retried after leaving a 0%-traffic green.
# ACA revision names are immutable, so bind the suffix to the immutable image tag this run built:
# the same image maps to the same revision, while every newly tagged build maps to a new one.
green_revision_suffix() {
  local image_tag="${IMG##*:}"
  [[ "$image_tag" =~ ^[a-z0-9][a-z0-9.-]{0,62}$ ]] \
    || die "image tag cannot form a safe Container Apps revision suffix"
  printf 'g%s' "${image_tag//./-}"
}

# Names are an isolation boundary. Staging once passed retired ACP_WORKER while this script read
# the three lane variables, so all three silently fell back to PRODUCTION names. Refuse that
# class of mistake before subscription reads, builds, queue probes, or Container App mutations.
case "$DEPLOY_TARGET_ENV" in
  staging)
    for a in "$APP" "${LANE_WORKERS[@]}"; do
      case "$a" in *-staging) ;; *) die "staging target '$a' must end in -staging; refusing cross-environment deployment" ;; esac
    done ;;
  production)
    for a in "$APP" "${LANE_WORKERS[@]}"; do
      case "$a" in *-staging) die "production target '$a' points at staging; refusing cross-environment deployment" ;; esac
    done ;;
  *) die "ACP_DEPLOY_TARGET_ENV must be 'production' or 'staging', got '$DEPLOY_TARGET_ENV'" ;;
esac
[ "$(printf '%s\n' "$APP" "${LANE_WORKERS[@]}" | sort -u | wc -l | tr -d ' ')" = "$(( ${#LANE_WORKERS[@]} + 1 ))" ] \
  || die "app and all worker targets must have distinct names"

# Capacity application is an API control-plane capability, so these settings are stamped only
# onto the API revision. Either environment may opt in, but only with its exact role-worker names
# and measured resource ceilings. The API independently rechecks its runtime environment and a
# hard production allowlist before constructing an Azure writer.
CAPACITY_APPLY_ENABLED=0
case "$CAPACITY_APPLY_REQUESTED" in 0|1) ;; *) die "ACP_CAPACITY_APPLY_ENABLED must be 0 or 1" ;; esac
if [ "$CAPACITY_APPLY_REQUESTED" = 1 ]; then
  # Consumption-environment core quota is not exposed for every subscription. Preserve the
  # API's explicit "not checked" warning when Azure returns no applicable quota; never invent
  # a ceiling merely to enable the control.
  if [ -n "$ACA_VCPU_QUOTA" ]; then
    [[ "$ACA_VCPU_QUOTA" =~ ^[0-9]+([.][0-9]+)?$ ]] \
      || die "ACP_ACA_VCPU_QUOTA must be a positive number when supplied"
    awk -v n="$ACA_VCPU_QUOTA" 'BEGIN { exit !(n > 0) }' \
      || die "ACP_ACA_VCPU_QUOTA must be greater than zero when supplied"
  fi
  [[ "$PG_MAX_CONNECTIONS" =~ ^[0-9]+$ ]] \
    || die "ACP_PG_MAX_CONNECTIONS must be a positive integer when capacity apply is enabled"
  [[ "$PG_RESERVED_CONNECTIONS" =~ ^[0-9]+$ ]] \
    || die "ACP_PG_RESERVED_CONNECTIONS must be a non-negative integer when capacity apply is enabled"
  [ "$PG_MAX_CONNECTIONS" -gt 0 ] \
    || die "ACP_PG_MAX_CONNECTIONS must be greater than zero when staging capacity apply is enabled"
  [ "$PG_RESERVED_CONNECTIONS" -lt "$PG_MAX_CONNECTIONS" ] \
    || die "ACP_PG_RESERVED_CONNECTIONS must be smaller than ACP_PG_MAX_CONNECTIONS"
  CAPACITY_APPLY_ENABLED=1
fi
API_ENV_VARS=(
  "ACP_DEPLOY_ENV=$DEPLOY_TARGET_ENV"
  "ACP_CAPACITY_APPLY_ENABLED=$CAPACITY_APPLY_ENABLED"
  "WORKER_APP_NAMES=$DISCOVERY_WORKER,$ASSESS_WORKER,$REMEDIATE_WORKER${RELEASE_WORKER:+,$RELEASE_WORKER}"
  "ACP_DEDICATED_RELEASE_WORKERS=$DEDICATED_RELEASE"
  "CAPACITY_APPLY_APP_NAMES=$APP,$DISCOVERY_WORKER,$ASSESS_WORKER,$REMEDIATE_WORKER,$GPU_APP"
)
if [ "$CAPACITY_APPLY_ENABLED" = 1 ]; then
  API_ENV_VARS+=(
    "ACP_PG_MAX_CONNECTIONS=$PG_MAX_CONNECTIONS"
    "ACP_PG_RESERVED_CONNECTIONS=$PG_RESERVED_CONNECTIONS"
  )
  [ -z "$ACA_VCPU_QUOTA" ] || API_ENV_VARS+=("ACP_ACA_VCPU_QUOTA=$ACA_VCPU_QUOTA")
fi

# ── subscription: resolved per-call, never via `az account set` ────────────────────────────
# `az account set` writes a global choice another concurrent process can change mid-deploy.
SUB="$(az account show ${ACP_SUBSCRIPTION:+--subscription "$ACP_SUBSCRIPTION"} --query id -o tsv 2>/dev/null || true)"
[ -n "$SUB" ] || die "no active Azure subscription — run 'az login', or set ACP_SUBSCRIPTION"
AZ=(--subscription "$SUB")

# ── 1. pin ─────────────────────────────────────────────────────────────────────────────────
SRC_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SRC_ROOT"
git fetch -q origin
# RESOLVE whatever was asked for into a full 40-char sha. ACP_PIN used to be taken verbatim, and
# every consumer below assumes a full one: `gh run list --commit` matches ONLY the full sha, so a
# short pin sailed past this line and died at the CI gate with "no CI run found for 1af3be9 — it
# may still be queued". That names the wrong cause. The commit was real, CI was green on it, and
# the suggested remedy (ACP_SKIP_CI_GATE=1) fixes the symptom by disabling the check — which is
# how a normalisation bug turns into a habit of deploying ungated. Observed twice on 2026-07-30.
#
# Also accepts a branch or tag now, since resolving is resolving; `^{commit}` peels an annotated
# tag rather than pinning the tag object, which is not a thing you can check out into a build.
PIN="$(git rev-parse --verify --quiet "${ACP_PIN:-origin/main}^{commit}")" \
  || die "cannot resolve ${ACP_PIN:+ACP_PIN=}${ACP_PIN:-origin/main} to a commit — check the ref exists locally (a fetch may be needed) and is unambiguous"
say "pinning ${PIN:0:7}"

# Serialized automatic staging events can arrive out of order. Compare the actual
# resolved ACP_PIN, never workflow metadata, against the currently serving build.
if [ "$DEPLOY_TARGET_ENV" = staging ] && [ -n "${STAGING_DEPLOY_MODE:-}" ]; then
  GUARD_FQDN="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
  [ -n "$GUARD_FQDN" ] || die "staging target has no ingress hostname"
  GUARD_HEALTH="$(curl -fsS --max-time 20 "https://$GUARD_FQDN/healthz" || echo '{}')"
  GUARD_ARGS=(--repo "$SRC_ROOT" --pin "$PIN" --mode "$STAGING_DEPLOY_MODE")
  if [ "${ACP_STAGING_ALLOW_ROLLBACK:-0}" = 1 ]; then
    [ -n "${ACP_PIN:-}" ] || die "manual rollback/recovery requires an explicit pin"
    [ "${ACP_SKIP_CI_GATE:-0}" != 1 ] || die "manual rollback/recovery cannot bypass image CI"
    GUARD_ARGS+=(--rollback)
  fi
  GUARD_DECISION="$(python3 "$SRC_ROOT/deploy/public/staging_pin_guard.py" "${GUARD_ARGS[@]}" <<<"$GUARD_HEALTH")" \
    || die "staging monotonic pin guard refused deployment"
  if [ "$GUARD_DECISION" = skip ]; then
    say "Skipping stale automatic staging pin ${PIN:0:7}; live build is newer"
    [ -z "${STAGING_GITHUB_OUTPUT:-}" ] || echo 'staging_deployed=false' >> "$STAGING_GITHUB_OUTPUT"
    exit 0
  fi
  if [ -n "${STAGING_GITHUB_OUTPUT:-}" ]; then
    echo 'staging_deployed=true' >> "$STAGING_GITHUB_OUTPUT"
    echo "staging_pin=$PIN" >> "$STAGING_GITHUB_OUTPUT"
  fi
fi

# The CI gate, ENFORCED rather than described. This step's own comment has always said "check CI
# is green on it", and nothing ever checked — a human was expected to remember. Deploying an
# unbuilt commit is exactly the mistake an automated pipeline makes faster than a person.
#
# Skipped, loudly, when gh is unavailable or unauthenticated: a local operator without gh should
# still be able to ship, and a gate that silently passes is worse than one that says it was not
# run. ACP_SKIP_CI_GATE=1 is the deliberate override for a commit CI cannot see (a local-only pin).
if [ "${ACP_CI_PROVIDER:-github}" = azure ]; then
  python3 "$SRC_ROOT/scripts/check_azure_ci.py" "$PIN" \
    || die "Azure CI did not authorize this exact deployment"
elif [ "${ACP_SKIP_CI_GATE:-0}" = 1 ]; then
  echo "  ⚠ CI gate SKIPPED by ACP_SKIP_CI_GATE=1"
elif ! command -v gh >/dev/null 2>&1; then
  echo "  ⚠ CI gate NOT CHECKED — gh is not installed on this host"
elif ! gh auth status >/dev/null 2>&1; then
  echo "  ⚠ CI gate NOT CHECKED — gh is not authenticated"
else
  # Two-phase CI gate.
  #
  # The workflow_run trigger fires the instant CI completes on the triggering commit, but the
  # deploy checks out CURRENT main — which may be a newer commit whose CI hasn't started yet.
  # A single gh run list query can return empty either because the API hasn't indexed the run
  # yet (takes a few seconds) OR because CI for this commit hasn't been queued at all yet.
  #
  # Phase 1 — wait for any run to appear (5 × 15 s = 75 s, covers API indexing lag).
  # Phase 2 — if the run is in_progress/queued, wait for it to finish (20 × 60 s = 20 min).
  _ci_json="[]"
  _ci_count=0
  for _ci_attempt in 1 2 3 4 5; do
    _ci_json="$(gh run list --commit "$PIN" --workflow CI --limit 1 --json status,conclusion 2>/dev/null || echo '[]')"
    _ci_count="$(printf '%s' "${_ci_json:-[]}" | jq 'length' 2>/dev/null || echo 0)"
    [ "${_ci_count:-0}" -gt 0 ] && break
    [ "$_ci_attempt" -lt 5 ] && { echo "  CI run not yet indexed (attempt $_ci_attempt/5) — retrying in 15s"; sleep 15; }
  done

  if [ "${_ci_count:-0}" -eq 0 ]; then
    die "no CI run found for ${PIN:0:7} after 5 attempts — it may not be on a branch CI builds. Set ACP_SKIP_CI_GATE=1 to deploy without the gate."
  fi

  # Phase 2: run is indexed — if it's still in_progress, wait for it to complete.
  _ci_status="$(printf '%s' "$_ci_json" | jq -r '.[0].status // ""')"
  case "$_ci_status" in
    in_progress|queued|waiting|pending)
      echo "  CI is ${_ci_status} on ${PIN:0:7} — waiting up to 20 min for it to finish…"
      for _ci_wait in $(seq 1 20); do
        sleep 60
        _ci_json="$(gh run list --commit "$PIN" --workflow CI --limit 1 --json status,conclusion 2>/dev/null || echo '[]')"
        _ci_status="$(printf '%s' "$_ci_json" | jq -r '.[0].status // ""')"
        case "$_ci_status" in
          in_progress|queued|waiting|pending) echo "  CI still ${_ci_status} (${_ci_wait}/20 min)…" ;;
          *) break ;;
        esac
      done
      ;;
  esac

  CI_CONC="$(printf '%s' "$_ci_json" | jq -r '.[0].conclusion // ""')"
  case "$CI_CONC" in
    success) echo "  ✓ CI is green on ${PIN:0:7}" ;;
    "")      die "CI on ${PIN:0:7} is still in progress after 20 min — timed out waiting. Re-run the deploy when CI finishes, or set ACP_SKIP_CI_GATE=1." ;;
    *)       die "CI on ${PIN:0:7} concluded '$CI_CONC', not success — refusing to deploy it." ;;
  esac
fi

# Suffixes catch the common mistake; the live environment stamp catches a deliberately or
# accidentally misleading name. Every target must already exist for redeploy, and all four must
# agree after the requested commit passes CI but before even the image build starts. This order
# preserves pin diagnostics: an invalid ref must be reported as invalid, not disguised as an
# unrelated live-target error. First-time staging creation belongs to staging_up.sh.
for a in "$APP" "${LANE_WORKERS[@]}"; do
  ACTUAL_DEPLOY_ENV="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$a" \
    --query "properties.template.containers[0].env[?name=='ACP_DEPLOY_ENV'].value | [0]" \
    -o tsv 2>/dev/null || true)"
  [ "$ACTUAL_DEPLOY_ENV" = "$DEPLOY_TARGET_ENV" ] \
    || die "$a is missing or stamped '$ACTUAL_DEPLOY_ENV', expected '$DEPLOY_TARGET_ENV'; refusing cross-environment deployment"
done

# ── 2. isolated clone ──────────────────────────────────────────────────────────────────────
# `az acr build` uploads the working directory as build context. This repo is worked by many
# concurrent sessions; on 2026-07-29 seventeen files were uncommitted and would have been baked
# into the image. Never build from the shared checkout.
WORK="$(mktemp -d -t acp-deploy-XXXX)"
trap 'rm -rf "$WORK"' EXIT
git clone -q --local "$SRC_ROOT" "$WORK/acp"
cd "$WORK/acp"
git checkout -q "$PIN"
source "$SRC_ROOT/deploy/public/remediation_scaler.sh"

# ── 3. compiled engines ────────────────────────────────────────────────────────────────────
say "building the .NET Office analyser"
# Resolve dotnet the same way api/scanner.py and tests/engines.py do: explicit override, then
# PATH, then the dev-machine install location. PATH is what makes this work on a CI runner —
# actions/setup-dotnet puts the muxer on PATH and never creates ~/.dotnet/dotnet, so the old
# hard-coded default would have failed on the very host CD runs on.
DOTNET="${ACP_DOTNET_MUXER:-$(command -v dotnet || echo "$HOME/.dotnet/dotnet")}"
[ -x "$DOTNET" ] || die "no dotnet muxer: tried ACP_DOTNET_MUXER, PATH, and ~/.dotnet/dotnet"
"$DOTNET" build spike/dotnet/AcpScan.Cli -c Release -v quiet --nologo \
  || die "dotnet build failed"
[ -f spike/dotnet/AcpScan.Cli/bin/Release/net10.0/AcpScan.Cli.dll ] \
  || die "AcpScan.Cli.dll missing after a 'successful' build"

say "checking the vendored Python PDF analyser"
# There is no vendoring STEP any more. The engine is tracked at engine/pdf-analyser (ADR 0029),
# so the isolated clone from step 2 already carries it and the build context is complete by
# construction — which is what makes a CI runner able to build this image at all.
#
# The count guard stays, now against the tracked tree. It earned its place: an expired token once
# made the old copy-from-outside step a silent no-op, and 0 modules still BUILDS, shipping an
# image that looks fine and cannot assess a single PDF. A guard that can only fire on a deletion
# is cheap enough to keep.
N_MOD="$(find engine/pdf-analyser -name '*.py' | wc -l | tr -d ' ')"
[ "$N_MOD" -ge "$MIN_MODULES" ] || die "engine/pdf-analyser has only $N_MOD modules, expected >= $MIN_MODULES — the vendored engine looks truncated, refusing to build"
echo "  $N_MOD modules (tracked in-repo)"

# ── 4. CalVer ──────────────────────────────────────────────────────────────────────────────
# YYYY.M.D.N in Pacific, N = the count of revisions already created today + 1. Baked into the
# image as ACP_BUILD_VERSION (Dockerfile ARG -> ENV), which is why an image-only update still
# moves the version the UI and /healthz report.
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
read -r BUILD_DATE DAY_START_UTC DAY_SECS <<<"$(python3 - "$BUILD_TIME" "$BUILD_TZ" <<'PY'
import sys, datetime, zoneinfo
t = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
tz = zoneinfo.ZoneInfo(sys.argv[2]); loc = t.astimezone(tz)
start = loc.replace(hour=0, minute=0, second=0, microsecond=0)
print(f"{loc.year}.{loc.month}.{loc.day}",
      start.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
      int((loc - start).total_seconds()))
PY
)"
# `--all` is REQUIRED: the app runs in Single revision mode, where `revision list` returns only
# the ACTIVE revision, so the count would always be 1 and every deploy would stamp .1.
REV_TIMES="$(az containerapp revision list "${AZ[@]}" -n "$APP" -g "$RG" --all \
              --query "[].properties.createdTime" -o tsv 2>/dev/null || true)"
# Compare only the first 19 chars, exactly as deploy.sh does. createdTime is "…T07:00:00+00:00"
# while the cutoff is "…T07:00:00Z"; a full-string compare differs at char 20, where '+' sorts
# BELOW 'Z', so a revision created precisely at Pacific midnight would be dropped. Both scripts
# must agree — two ordinals for one day is worse than either scheme alone.
SEQ="$(printf '%s\n' "$REV_TIMES" \
       | awk -v cutoff="${DAY_START_UTC:0:19}" 'length($0) >= 19 && substr($0,1,19) >= cutoff' \
       | wc -l | tr -d ' ')"
BUILD_VERSION="${BUILD_DATE}.$(( SEQ + 1 ))"
# Fallback: seconds since Pacific midnight — monotonic within the day and independent of Azure,
# so a throttled revision query never stamps a duplicate ordinal.
[ -n "$REV_TIMES" ] || BUILD_VERSION="${BUILD_DATE}.${DAY_SECS}"
say "CalVer $BUILD_VERSION  (built $BUILD_TIME)"

# The COMMIT this image is built from. `$PIN` rather than `git rev-parse HEAD`: it is the sha
# step 2 resolved and checked out, so it states the intent rather than re-deriving it from a cwd
# — and it is already a full 40-char sha, resolved precisely so a pin could not be taken verbatim
# and mean something else later.
#
# WHY IT IS WORTH A BUILD ARG. `version` answers "which build" and `built_at` answers "when";
# neither answers "which COMMIT", which is the question every deploy verification actually asks.
# Establishing that a merge reached production took a CalVer stamp, two workflow-run timestamps
# and a cancelled run to disambiguate on 2026-09-06 — and the answer was still an inference. One
# sha makes it a read.
BUILD_SHA="$PIN"

# ── 5. bases, rebuilt only when their inputs change ────────────────────────────────────────
# The hash covers exactly what the base images are built FROM. Source files are deliberately
# absent: if a source change moved this hash, the cache would never hit and the whole exercise
# would be pointless. Dockerfile.base-* are included so editing the apt block busts the base.
_hash() { cat "$@" | shasum -a 256 | cut -c1-12; }
# frontend/, matching Dockerfile and Dockerfile.base-web. This hash is what makes the base
# image rebuild when the dependencies change — point it at the wrong lockfile and a stale base
# keeps satisfying the app image's `[ -d node_modules ]` guard, which skips npm install and
# builds the SPA against another tree's dependencies without erroring.
WEB_HASH="$(_hash frontend/package-lock.json deploy/public/Dockerfile.base-web)"
API_HASH="$(_hash api/requirements.txt deploy/public/Dockerfile.base-api)"
BASE_WEB="$ACR.azurecr.io/acp-base-web:$WEB_HASH"
BASE_API="$ACR.azurecr.io/acp-base-api:$API_HASH"

have_tag() { az acr repository show-tags "${AZ[@]}" -n "$ACR" --repository "$1" -o tsv 2>/dev/null | grep -qx "$2"; }

build_base() {  # repo tag dockerfile
  if have_tag "$1" "$2"; then echo "  ✓ $1:$2 already in the registry — skipping"; return; fi
  say "base $1:$2 is new — building it (this is the slow one, and it is why the app build is not)"
  [ "$DRY" = 1 ] || az acr build "${AZ[@]}" -r "$ACR" -t "$1:$2" -f "$3" . >/dev/null
}
build_base acp-base-web "$WEB_HASH" deploy/public/Dockerfile.base-web
build_base acp-base-api "$API_HASH" deploy/public/Dockerfile.base-api

# ── 6. app image ───────────────────────────────────────────────────────────────────────────
IMG="$ACR.azurecr.io/acp-app:${PIN:0:7}-$(date +%s)"
say "building $IMG"
if [ "$DRY" = 1 ]; then
  echo "  DRY RUN — would build FROM $BASE_WEB / $BASE_API"
else
  az acr build "${AZ[@]}" -r "$ACR" -t "${IMG#*/}" -f deploy/public/Dockerfile \
    --build-arg "BASE_WEB=$BASE_WEB" --build-arg "BASE_API=$BASE_API" \
    --build-arg "BUILD_VERSION=$BUILD_VERSION" --build-arg "BUILD_TIME=$BUILD_TIME" \
    --build-arg "BUILD_SHA=$BUILD_SHA" . >/dev/null
fi

# ── 7. health BEFORE ───────────────────────────────────────────────────────────────────────
FQDN="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
BEFORE="$(curl -s --max-time 20 "https://$FQDN/healthz" || echo '{}')"
say "before: $BEFORE"
# The app and every stage worker must already be running, or a stalled queue afterwards is
# unattributable. The retired generic acp-worker is deliberately absent from this list.
for a in "$APP" "${LANE_WORKERS[@]}"; do
  st="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$a" --query properties.runningStatus -o tsv)"
  [ "$st" = "Running" ] || die "$a is '$st' before we started — fix that first, do not deploy onto it"
done

if [ "$DRY" = 1 ]; then
  # A dry run that skipped the blue-green block would leave the riskiest path — the one that
  # moves production traffic — as the only part nobody ever rehearses. So walk it here using
  # READS ONLY, and print the revisions it would actually touch, resolved live.
  if [ "$BG" = 1 ]; then
    ENV_DOMAIN="$(app_environment_domain)"
    MODE="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.configuration.activeRevisionsMode -o tsv)"
    BLUE="$(az containerapp ingress traffic show "${AZ[@]}" -g "$RG" -n "$APP" \
              --query "[?weight>\`0\`] | [0].revisionName" -o tsv 2>/dev/null || true)"
    [ -n "$BLUE" ] || BLUE="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.latestRevisionName -o tsv)"
    SUFFIX="$(green_revision_suffix)"
    say "DRY RUN — blue-green plan"
    echo "  revision mode  : $MODE$([ "$MODE" != Multiple ] && echo '  -> would switch to Multiple')"
    echo "  blue (keeps traffic until promotion): $BLUE"
    echo "  green (would provision at 0%)      : $APP--$SUFFIX"
    echo "  green smoke-test url               : https://$APP--$SUFFIX.$ENV_DOMAIN/healthz"
    echo "  worker cutover                     : ${LANE_WORKERS[*]} -> $IMG  (NOT blue-green)"
  fi
  say "DRY RUN — stopping before anything is changed"
  exit 0
fi

# Worker revisions pull from one shared durable queue. Replacing all lanes while work is active
# is therefore not blue-green: it interrupts handlers and leaves lease recovery to repair the
# run later. Refuse routine releases before any Container App mutation. The explicit override is
# for an emergency security/correctness release whose operator accepts that interruption risk.
READY_BEFORE="$(curl -s --max-time 20 "https://$FQDN/readyz" || echo '{}')"
if [ "$DEDICATED_RELEASE" = 1 ]; then
  python3 -c 'import json,sys
r=json.load(sys.stdin).get("workers", {}).get("roles", {}).get("release", {})
if not r.get("alive") or int(r.get("pool_size") or 0) < 3:
 sys.exit("Release worker must report a live three-slot heartbeat before dedicated routing is enabled")' <<<"$READY_BEFORE" \
    || die "Release capacity is not ready; existing routing has not been changed"
fi
read -r QUEUE_AVAILABLE ACTIVE_JOBS REDIS_REPORTED REDIS_CONFIGURED REDIS_REACHABLE REDIS_TOPOLOGY <<EOF
$(python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); q=d.get("queue", {}); deps=d.get("dependencies", {}); r=deps.get("redis", {})
 print(str(bool(q.get("available"))).lower(), q.get("active", ""),
       str("redis" in deps).lower(),
       str(bool(r.get("configured"))).lower(), str(bool(r.get("reachable"))).lower(),
       r.get("topology", "unknown"))
except Exception:
 print("false", "", "false", "false", "false", "unknown")' <<<"$READY_BEFORE")
EOF
if [ "$REDIS_REPORTED" != true ]; then
  # The one release before #1576 cannot report either Redis health or aggregate queue activity.
  # Admit ONLY that old->new transition: /healthz must identify a real commit at or before the
  # parent of the gate commit, while the requested target must contain the gate. A later revision
  # that drops the fields is a regression, not "legacy", and stays blocked.
  LEGACY_GATE_COMMIT="e3689c2ce4d801e0ea08482bbc72ba9076ad4f18"
  HEALTH_BEFORE="$(curl -s --max-time 20 "https://$FQDN/healthz" || echo '{}')"
  LIVE_SHA="$(python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("commit") or "").strip())
except Exception: print("")' <<<"$HEALTH_BEFORE")"
  [[ "$LIVE_SHA" =~ ^[0-9a-f]{40}$ ]] \
    || die "current revision lacks verifiable commit provenance; refusing legacy worker bootstrap"
  git merge-base --is-ancestor "$LIVE_SHA" "${LEGACY_GATE_COMMIT}^" \
    || die "current revision is not eligible for the one-release legacy bootstrap; missing Redis readiness is a deployment blocker"
  git merge-base --is-ancestor "$LEGACY_GATE_COMMIT" "$PIN" \
    || die "target does not contain the Redis readiness safeguard; refusing legacy worker bootstrap"

  # Execute the independent probe in the CURRENT app revision, where secret-backed REDIS_URL and
  # DATABASE_URL already resolve to the same shared services used by the workers. The source is
  # transmitted because, by definition, this old image does not contain the new probe yet.
  # Azure carries --command in its websocket handshake. The raw base64 source made that request
  # too large: the handshake failed before its "connected" banner, while the same identity and a
  # tiny command succeeded. gzip keeps this below the live-tested limit without changing a byte
  # of the program Python executes after decompression. mtime=0 makes the payload reproducible.
  LEGACY_PROBE_B64="$(python3 -c 'import base64,gzip,pathlib; print(base64.b64encode(gzip.compress(pathlib.Path("deploy/public/legacy_bootstrap_probe.py").read_bytes(), mtime=0)).decode())')"
  # ACA splits --command on spaces itself; quoting the -c program is passed to Python literally.
  # Keep the program space-free so it remains one argument after Azure's parser handles it.
  LEGACY_PROBE_COMMAND="python -c exec(__import__('gzip').decompress(__import__('base64').b64decode('$LEGACY_PROBE_B64')))"
  LEGACY_PROBE_EXIT=0
  LEGACY_PROBE_RAW="$(_aca_exec_tty az containerapp exec "${AZ[@]}" -g "$RG" -n "$APP" \
    --command "$LEGACY_PROBE_COMMAND" 2>&1)" || LEGACY_PROBE_EXIT=$?
  # Azure CLI sometimes returns nonzero while closing its interactive websocket AFTER the remote
  # program printed its result. Parse the result first: exactly one strict marker proves the
  # Redis and PostgreSQL checks ran; an exit code only describes terminal teardown. Missing,
  # duplicated, malformed, or negative marker data remains a hard failure and raw Azure output
  # is never printed because its connection banner carries resource identifiers.
  read -r BOOTSTRAP_REDIS BOOTSTRAP_QUEUED BOOTSTRAP_RETRYING BOOTSTRAP_RUNNING <<EOF
$(python3 scripts/parse_legacy_bootstrap.py <<<"$LEGACY_PROBE_RAW")
EOF
  [ "$BOOTSTRAP_REDIS" = true ] \
    || die "legacy bootstrap returned no valid Redis and queue verification; refusing worker cutover"
  if [ "$LEGACY_PROBE_EXIT" != 0 ]; then
    echo "  ⚠ Azure exec transport exited $LEGACY_PROBE_EXIT after the verified probe result; continuing from the strict in-container marker"
  fi
  REDIS_CONFIGURED=true
  REDIS_REACHABLE=true
  QUEUE_AVAILABLE=true
  ACTIVE_JOBS=$((BOOTSTRAP_QUEUED + BOOTSTRAP_RETRYING + BOOTSTRAP_RUNNING))
  [ "$ACTIVE_JOBS" = 0 ] \
    || die "legacy bootstrap found $BOOTSTRAP_QUEUED queued, $BOOTSTRAP_RETRYING retrying, and $BOOTSTRAP_RUNNING running job(s); its one-time cutover requires a globally empty queue"
  echo "  ✓ legacy bootstrap independently verified shared Redis and global queue state"
else
  [ "$REDIS_CONFIGURED" = true ] || die "Redis is not configured; split worker services cannot preserve live scan state"
  [ "$REDIS_REACHABLE" = true ] || die "Redis is unavailable before deployment; refusing to replace workers while shared live state is unhealthy"
  if [ "$REDIS_TOPOLOGY" = self_hosted ]; then
    echo "  ⚠ Redis is reachable but self-hosted; migrate REDIS_URL to Azure Managed Redis to remove the single-replica failure domain"
  elif [ "$REDIS_TOPOLOGY" = managed ]; then
    echo "  ✓ managed Redis is reachable"
  fi
fi
if [ "$ALLOW_ACTIVE_JOBS" != 1 ]; then
  [ "$QUEUE_AVAILABLE" = true ] || die "cannot establish durable queue activity from /readyz; refusing worker cutover (set ACP_DEPLOY_WITH_ACTIVE_JOBS=1 only for an emergency)"
  [ "${ACTIVE_JOBS:-0}" = 0 ] || die "$ACTIVE_JOBS queued/running job(s) are active; wait for queues to drain or explicitly set ACP_DEPLOY_WITH_ACTIVE_JOBS=1"
else
  say "WARNING: ACP_DEPLOY_WITH_ACTIVE_JOBS=1 — worker cutover may interrupt ${ACTIVE_JOBS:-unknown} active job(s)"
fi

# Schema-changing releases elect one bounded migrator before any app/worker image
# mutation. A held reader or active queue refuses this attempt; no customer work
# is cancelled, and the existing deployment continues on its prior images.
say "preparing the exact pinned schema before worker cutover"
python3 "$SRC_ROOT/deploy/public/schema_preflight.py" \
  --subscription "$SUB" --group "$RG" --environment "$DEPLOY_TARGET_ENV" \
  --api-path "$PWD/api" "$APP" "${LANE_WORKERS[@]}" \
  || die "schema preflight failed; no API or worker image update started"

STARTUP_EVIDENCE_PATH="${ACP_STARTUP_EVIDENCE_PATH:-$(mktemp -t acp-startup-evidence-XXXX)}"
_verify_startup() {
  python3 "$SRC_ROOT/deploy/public/startup_evidence.py" \
    --subscription "$SUB" --group "$RG" --image "$IMG" \
    --output "$STARTUP_EVIDENCE_PATH" "$@" \
    || die "new revision startup failed or restarted; inspect the sanitized startup receipt"
}

# ── 8-BG. blue-green (ACP_BLUE_GREEN=1) ────────────────────────────────────────────────────
#
# WHAT IS AND IS NOT PROTECTED — read this before trusting the word "blue-green".
#
# ACA splits traffic by INGRESS WEIGHT. acp-app has external ingress, so it gets the real thing:
# green provisions at 0%, is smoke-tested on its own FQDN while production still serves blue, and
# takes traffic in one weight change that reverses just as fast.
#
# Stage workers have NO INGRESS (properties.configuration.ingress is null). They take work by pulling
# from a shared job queue, so a second worker on a different image would pull from that SAME
# queue — blue and green racing over live production jobs, picked at random. There is no weight
# to set and nothing to split. The worker therefore CUTS OVER at promotion, and that step is not
# protected. Saying so is the point; a deploy script that implied otherwise would be worse than
# one that never claimed blue-green at all.
#
# Consequence while green sits at 0%: the system is MIXED — green app on the new image, worker
# still on the old one. Read-only checks are fine. Anything that enqueues a job whose contract
# changed is NOT: green would enqueue work the old worker cannot correctly process. That is why
# the smoke tests below are read-only, and it is a rule rather than an oversight.
#
# Making the worker genuinely blue-green needs queue partitioning — green consumes its own queue,
# promotion swaps which queue the app enqueues to. That is an application change (worker_main.py
# plus the job table), not a deploy-script change, and it is deliberately out of scope here.
# Validate and prepare before the first service mutation in either rollout path.
_prepare_remediation_worker_patch

if [ "$BG" = 1 ]; then
  ENV_DOMAIN="$(app_environment_domain)"

  # Multiple-revision mode, idempotently. Switching Single -> Multiple leaves the running revision
  # on 100%, so it is safe against a live app, and it is a no-op once set.
  MODE="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.configuration.activeRevisionsMode -o tsv)"
  if [ "$MODE" != "Multiple" ]; then
    say "switching $APP to multiple-revision mode (currently $MODE)"
    _aca_retry az containerapp revision set-mode "${AZ[@]}" -g "$RG" -n "$APP" --mode multiple -o none
  fi

  # BLUE = whatever holds traffic RIGHT NOW, captured before anything changes. After the update
  # the "latest" revision is green, so blue is no longer reachable by that route.
  BLUE="$(az containerapp ingress traffic show "${AZ[@]}" -g "$RG" -n "$APP" \
            --query "[?weight>\`0\`] | [0].revisionName" -o tsv 2>/dev/null || true)"
  [ -n "$BLUE" ] || BLUE="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.latestRevisionName -o tsv)"
  BLUE_IMG="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$APP" --query properties.template.containers[0].image -o tsv)"
  say "blue = $BLUE"

  # Green at 0%. In Multiple mode weights are explicit, so a new revision takes no traffic until
  # told to — there is no "hold it back" flag to forget.
  SUFFIX="$(green_revision_suffix)"
  GREEN="$APP--$SUFFIX"
  say "deploying green ($GREEN) at 0% traffic"
  _aca_retry python3 "$SRC_ROOT/deploy/public/update_api_image.py" \
    --subscription "$SUB" --group "$RG" --app "$APP" --image "$IMG" \
    --revision-suffix "$SUFFIX" --env "${API_ENV_VARS[@]}"

  GREEN_FQDN="$GREEN.$ENV_DOMAIN"
  printf '  waiting for green '
  for _ in $(seq 1 60); do
    case "$(curl -s --max-time 10 "https://$GREEN_FQDN/healthz" || true)" in
      *'"ok":true'*) printf ' ✓\n'; break ;;
    esac
    printf '.'; sleep 5
  done

  # Smoke-test green on its OWN url while production still serves blue. Read-only (see header).
  # Every failure below leaves blue on 100% and exits — a green that cannot prove itself must not
  # take traffic, and the message says so rather than leaving the operator to infer it.
  say "smoke-testing green at $GREEN_FQDN"
  GHZ="$(curl -s --max-time 20 "https://$GREEN_FQDN/healthz" || echo '{}')"
  echo "  healthz: $GHZ"
  case "$GHZ" in
    *'"version_stamped":true'*) ;;
    *) die "green is not stamped — it would serve version 'dev'. NOT promoting; blue still has all traffic." ;;
  esac
  case "$GHZ" in
    *"\"version\":\"$BUILD_VERSION\""*) ;;
    *) die "green reports the wrong version (wanted $BUILD_VERSION). NOT promoting; blue still has all traffic." ;;
  esac
  curl -sf --max-time 20 "https://$GREEN_FQDN/readyz" >/dev/null \
    || die "green /readyz failed. NOT promoting; blue still has all traffic."
  echo "  readyz:  ok"

  _verify_startup "$APP"
  say "promoting green to 100%"
  az containerapp ingress traffic set "${AZ[@]}" -g "$RG" -n "$APP" \
    --revision-weight "$GREEN=100" "$BLUE=0" >/dev/null

  say "cutting ${LANE_WORKERS[*]} over to the same image (NOT blue-green — see header)"
  for a in "${LANE_WORKERS[@]}"; do
    _update_lane_worker "$a"
  done
  for a in "${LANE_WORKERS[@]}"; do
    printf '  %s ' "$a"
    for _ in $(seq 1 60); do
      img="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$a" --query properties.template.containers[0].image -o tsv 2>/dev/null || true)"
      [ "$img" = "$IMG" ] && { printf ' ✓\n'; break; }
      printf '.'; sleep 5
    done
  done

  _verify_remediation_scaler
  python3 "$SRC_ROOT/deploy/public/repair_queue_connection.py" "$RG" "$ASSESS_WORKER" "$REMEDIATE_WORKER" ${RELEASE_WORKER:+"$RELEASE_WORKER"}

  # Verified through the PUBLIC url, not green's. Green being healthy proves green is healthy;
  # only the public url proves traffic actually moved.
  say "verifying on the public url"
  for _ in $(seq 1 40); do
    AFTER="$(curl -s --max-time 20 "https://$FQDN/healthz" || echo '{}')"
    case "$AFTER" in *"\"version\":\"$BUILD_VERSION\""*) break ;; esac
    sleep 5
  done
  echo "  after: $AFTER"
  case "$AFTER" in
    *"\"version\":\"$BUILD_VERSION\""*) printf '\n\033[32m✓ %s is live on %s (blue %s kept at 0%% for rollback)\033[0m\n' "$BUILD_VERSION" "$APP" "$BLUE" ;;
    *) die "traffic did not move to green. Roll back with the commands below." ;;
  esac

  # Blue stays at 0% rather than being deactivated, so rollback needs no rebuild. Printed with the
  # real names filled in: recovery should be a paste, not a reconstruction under pressure.
  cat <<ROLLBACK

  Rollback — app is instant (a weight change, nothing provisions); the workers need ~20s:

    az containerapp ingress traffic set -g $RG -n $APP --revision-weight $BLUE=100 $GREEN=0
    az containerapp update -g $RG -n $DISCOVERY_WORKER --image $BLUE_IMG
    az containerapp update -g $RG -n $ASSESS_WORKER --image $BLUE_IMG
    az containerapp update -g $RG -n $REMEDIATE_WORKER --image $BLUE_IMG

  Run ALL FOUR. New workers under an old app are the mixed-version state promotion exists to close.
ROLLBACK
  _verify_startup "$APP" "${LANE_WORKERS[@]}"
  exit 0
fi

# ── 8. update app and stage workers, concurrently ──────────────────────────────────────────
# Same image, different roles. Scanning happens in the workers, so updating only the app
# ships the fixes nowhere useful. --no-wait so the two revisions provision in parallel and the
# window where they run different images is as short as it can be.
#
# EVERY UPDATE RETRIES THE ACA LOCK, because losing that race leaves production SPLIT. Azure
# serialises modifications per container app; a write while one is provisioning is refused with
#
#     (ContainerAppOperationInProgress) Cannot modify a container app 'acp-discovery' because
#     there is an active provisioning operation in progress. OperationId: '...'
#
# and under `set -e` that killed the job MID-LOOP. On 2026-09-02 it did so twice in six minutes
# (runs 33579832625 and 33580168055, for ca4d6e5d and 0f096be0): the app's update was accepted,
# acp-discovery's was refused, and the deploy died between them. Production ran the API on
# 2026.9.1.48 and all three workers on .41 for over half an hour — /healthz 200, /readyz 200,
# `degraded: []`, nothing red anywhere, and the OCR fix that had just merged live in the API and
# absent from the workers that actually run scans. That is the worst shape this script can leave:
# the mixed-version state its own header calls out, reached by the guard against it aborting.
#
# A refusal is a STATE, not a fault — the same command succeeds once the in-flight operation
# settles — so `_aca_retry` waits it out and fails fast on anything else. See readiness_probe.sh.
say "updating $APP + ${LANE_WORKERS[*]} concurrently"
_aca_retry python3 "$SRC_ROOT/deploy/public/update_api_image.py" \
  --subscription "$SUB" --group "$RG" --app "$APP" --image "$IMG" \
  --env "${API_ENV_VARS[@]}"
for a in "${LANE_WORKERS[@]}"; do
  _update_lane_worker "$a"
done

for a in "$APP" "${LANE_WORKERS[@]}"; do
  printf '  %s ' "$a"
  for _ in $(seq 1 60); do
    img="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$a" --query properties.template.containers[0].image -o tsv 2>/dev/null || true)"
    [ "$img" = "$IMG" ] && { printf ' ✓\n'; break; }
    printf '.'; sleep 5
  done
done

_verify_remediation_scaler
python3 "$SRC_ROOT/deploy/public/repair_queue_connection.py" "$RG" "$ASSESS_WORKER" "$REMEDIATE_WORKER" ${RELEASE_WORKER:+"$RELEASE_WORKER"}

# ── 8b. single-revision mode, so the new revision actually holds traffic ──────────────────────
# The whole normal path assumes Single mode — where the update above makes its new revision the
# sole active one at 100% (the same assumption the `--all` note near the top spells out). Nothing
# in this path sets traffic, because Single mode does it for us.
#
# A blue-green deploy breaks that assumption and does not restore it: it switches the app to
# Multiple mode (line ~253) and exits at the promotion (keeping blue at 0% for rollback), leaving
# the app in Multiple mode for good. The next NORMAL deploy then provisions its new revision at 0%
# traffic — healthy, ready, serving nobody — and the verify below fails with "expected version X,
# got Y" against a revision that is running perfectly. That is the mechanism behind every stuck
# deploy since the first blue-green: not a broken build, an unrouted one.
#
# Switching back to Single here sets the latest revision (the one just provisioned above) to 100%
# and deactivates the rest. It is a no-op when already Single, so a normal-only history never
# notices it; it only fires to unstick an app a blue-green left in Multiple mode. Placed AFTER the
# image-confirmation loop so the revision it routes 100% to is one we have just seen come up.
#
# EVERY APP THIS SCRIPT DEPLOYS, not just the one with ingress. This block named $APP only, so
# nothing in this repo ever asserted the worker services' revision mode — and no script sets it
# either, which is the shape of state that drifts and is never corrected. The lane workers have NO
# INGRESS, so an extra active revision is not stranded at 0% traffic the way the app's would be:
# it simply keeps running and keeps claiming jobs from the shared queue. The app's version of this
# bug is loud (the verify below fails with "expected version X, got Y"); theirs is silent.
#
# Derived from LANE_WORKERS rather than named one by one. Spelling the apps out here is what broke
# production deploys on 2026-09-01: this loop was written as `"$APP" "$WORKER" "$DISCOVERY_WORKER"`
# while the same day's #1172 retired the generic worker and deleted $WORKER, so under `set -u`
# every deploy died HERE —
#
#     deploy/public/redeploy.sh: line 433: WORKER: unbound variable
#
# after the images were already updated and before steps 9/9b ran, so nothing was verified and the
# job reported failure on a deploy that had in fact shipped. `bash -n` cannot see an unbound
# variable, and the test covering this loop pinned the literal app list, so both guards passed.
for a in "$APP" "${LANE_WORKERS[@]}"; do
  MODE="$(az containerapp show "${AZ[@]}" -g "$RG" -n "$a" --query properties.configuration.activeRevisionsMode -o tsv 2>/dev/null || echo Single)"
  if [ "$MODE" != "Single" ]; then
    say "returning $a to single-revision mode (was $MODE) so the new revision takes over"
    _aca_retry az containerapp revision set-mode "${AZ[@]}" -g "$RG" -n "$a" --mode single -o none
  fi
done

# Apply only on the normal path. The blue-green path exits above: patching its template during
# promotion would create a third revision and invalidate the blue/green pair being verified.
_apply_readiness_probe

# ── 9. verify ──────────────────────────────────────────────────────────────────────────────
say "verifying"
for _ in $(seq 1 40); do
  AFTER="$(curl -s --max-time 20 "https://$FQDN/healthz" || echo '{}')"
  case "$AFTER" in *"\"version\":\"$BUILD_VERSION\""*) break ;; esac
  sleep 5
done
echo "  after:  $AFTER"
curl -s --max-time 20 "https://$FQDN/readyz" | head -c 400; echo

# The CalVer must have actually moved. An image built without the build args reports "dev" and
# version_stamped:false — it would run perfectly well while every surface lied about what it is.
case "$AFTER" in
  *'"version_stamped":true'*) ;;
  *) die "deployed image is not stamped — it will serve version 'dev'. Build args were lost." ;;
esac
case "$AFTER" in
  *"\"version\":\"$BUILD_VERSION\""*) printf '\n\033[32m✓ %s is live on %s\033[0m\n' "$BUILD_VERSION" "$APP" ;;
  *) die "expected version $BUILD_VERSION, got: $AFTER" ;;
esac

# ── 9b. verify the WORKER SERVICES, per role, by asking them ───────────────────────────────
#
# Step 8's ✓ reads the TEMPLATE: the image ACA was TOLD to run. Step 9 reads /healthz: the image
# the app IS running. The worker services only ever had the first kind of check, and the gap
# between them is not theoretical — on 2026-09-01 the app rolled 2026.9.1.12 -> .23 across eleven
# deploys, every one printing `acp-worker ✓`, while the live worker tier reported an image built
# on 31 August throughout.
#
# THE SHARED HEARTBEAT KEY CANNOT ANSWER THIS, and that is the trap worth writing down. Every
# worker writes its beat twice (worker_main.py:105-106): to `worker_tier_heartbeat`, and to
# `worker_tier_heartbeat:<role>`. /readyz's `workers.version` reads the FIRST — one row, last
# writer wins — so with two worker services running it reports whichever beat most recently.
# Sampling production every 6s for 90s returned 2026.8.31.39 (pool=2) thirteen times and
# 2026.8.31.20 (pool=3) once: two services alternating in one field. Comparing THAT against the
# build being shipped would warn at random on every deploy, which is worse than not checking.
#
# So this reads `workers.roles.<role>.version` — each service's own key.
#
# IT CHECKS THE ROLES THIS DEPLOY UPDATED (LANE_ROLES), NOT WHICHEVER ROLES REPORTED, and that
# distinction is the whole of the second correction. Checking "every role that reported" is wrong
# in both directions at once:
#
#   - FALSE ALARM, forever. `worker_tier_heartbeat:<role>` is a settings row, and nothing reaps
#     it when the service that wrote it goes away. #1172 retired the generic worker, which ran as
#     ACP_WORKER_ROLE=processing; its last beat (2026.9.1.29) is still in the table and still in
#     /readyz. Measured against production on 2026-09-01, this step's own program printed
#     `processing=2026.9.1.29` against build 2026.9.1.32 — and would have on every future deploy,
#     because that row can never catch up. A warning nobody can clear is one people learn to skip.
#     It also cost the full 24x5s retry every time, since the condition never clears.
#   - SILENT PASS on the case that matters. A role that never reported at all contributed no line,
#     so a lane worker whose replicas never came up — the exact failure this step exists to catch —
#     read as ✓.
#
# Naming the roles fixes both: an absent one is now a failure, and a retired one is not our
# business. Roles outside LANE_ROLES are reported as a NOTE, not a warning, so that a service
# somebody adds without adding it here is still visible.
say "verifying worker services (per role — they have no ingress to curl)"
#
# ONE CLEAN POLL IS NOT AN ANSWER, and this loop used to break on the first one.
#
# Measured on 2026-09-01, deploy of 2026.9.1.36 (run 33568634537). Step 9b printed
# `✓ every deployed worker role is running 2026.9.1.36` at 23:03:55.5585. Discovery's row:
#
#     23:03:39.570  beat, version 2026.9.1.30
#    ~23:03:55.5    step 9b polls, sees .36, BREAKS, prints the tick
#     23:03:55.581  beat, version 2026.9.1.30
#     23:03:56.35   the workflow's own /readyz: discovery .30, age 0.5s
#
# Discovery beats every 15s (worker_main.py), and .30 was written at :39.57 and :55.58 — 16s
# apart, one replica's cadence. A .36 write in between is a SECOND WRITER. Two replicas, one on
# each image, both writing worker_tier_heartbeat:discovery, because the role-scoped key is
# last-writer-wins across REPLICAS and not merely across services. Breaking on the first clean
# poll turns that into a check that retries until it gets a green sample and then stops — it
# converges on green rather than being able to go red, which is the same defect as the f-string.
#
# So a role must stay clean across a span LONGER THAN ONE HEARTBEAT INTERVAL. An old replica
# still beating writes its version at least once per interval, so a window wider than that has
# to contain one of its writes to be observed. Any dirty poll resets the streak to zero.
#
# THIS IS PROBABILISTIC, NOT A PROOF, and pretending otherwise is how the last version of this
# check got believed. The row holds whichever write landed last; if the old replica's beat is
# consistently overwritten by the new one within a second or two, a 5s poll cadence can still
# miss it. That is why the revision-count check below exists — ACA can be asked how many
# revisions are actually running, which is an answer rather than a sample.
_HEARTBEAT_S=15                 # worker_main.py: `if now - last_beat >= 15`
_MIN_CLEAN_SPAN=$(( _HEARTBEAT_S + 5 ))
_ROLES_JSON=""
_STREAK_START=""
_SUSTAINED=0
_STALE="startup"                # non-empty until a poll says otherwise
for _ in $(seq 1 24); do
  _ROLES_JSON="$(curl -s --max-time 20 "https://$FQDN/readyz" || true)"
  # Every DEPLOYED role that is not running this build, one "role=problem" per line.
  # NO f-STRING HERE, and that is not style. A backslash inside an f-string expression is a
  # SyntaxError, so an earlier version of this line printed nothing on every input — which made
  # `_STALE` always empty and step 9b always print ✓. A check that cannot go red is worse than
  # no check; caught only by running it against sample payloads.
  _STALE="$(printf '%s' "$_ROLES_JSON" | python3 -c '
import json, sys
want = sys.argv[1]
required = sys.argv[2:]
try:
    roles = json.load(sys.stdin)["workers"].get("roles")
except Exception:
    roles = None
if not isinstance(roles, dict):
    # An older API without the field, or a curl that returned an error page. Reported, not
    # crashed and not passed: "we could not ask" is a third answer and must not read as ✓.
    print("readyz=no-roles-field")
    sys.exit(0)
for role in required:
    r = roles.get(role)
    if not isinstance(r, dict):
        print(role + "=absent")
        continue
    got = r.get("version")
    if got is None:
        # A heartbeat predating the version field. Unknown, not wrong.
        continue
    if got != want:
        print(role + "=" + str(got))
    elif r.get("alive") is False:
        # Right image, no longer beating: the replica wrote one beat and died.
        print(role + "=stale-" + str(r.get("age_s")) + "s")
' "$BUILD_VERSION" "${LANE_ROLES[@]}" 2>/dev/null || true)"
  if [ -n "$_STALE" ]; then
    _STREAK_START=""            # any dirty poll starts the span over
  else
    [ -n "$_STREAK_START" ] || _STREAK_START="$SECONDS"
    if [ $(( SECONDS - _STREAK_START )) -ge "$_MIN_CLEAN_SPAN" ]; then
      _SUSTAINED=1
      break
    fi
  fi
  sleep 5
done

# THE VERDICT IS `_SUSTAINED`, NOT `_STALE`, and that distinction is load-bearing. On exhaustion
# `_STALE` holds only the LAST poll's result, so a run that flapped for two minutes and happened
# to end on a clean poll would leave it empty and print the tick — exactly the bug this block was
# rewritten to remove, reintroduced one level up. An unsustained streak is not a pass.
if [ "${_SUSTAINED:-0}" != 1 ] && [ -z "$_STALE" ]; then
  _STALE="roles=never-clean-for-${_MIN_CLEAN_SPAN}s (last poll was clean; earlier ones were not)"
fi

# Roles that reported but are not ours to deploy. Informational: a retired service's row lives on
# (see above), and a NEW service missing from LANE_ROLES should be noticed rather than warned about.
_OTHER="$(printf '%s' "$_ROLES_JSON" | python3 -c '
import json, sys
try:
    roles = json.load(sys.stdin)["workers"].get("roles") or {}
except Exception:
    sys.exit(0)
known = set(sys.argv[1:])
for role, r in sorted(roles.items()):
    if role in known or not isinstance(r, dict):
        continue
    print(role + "=" + str(r.get("version")) + ("" if r.get("alive") else " (not beating)"))
' "${LANE_ROLES[@]}" 2>/dev/null || true)"
if [ -n "$_OTHER" ]; then
  printf '  note: roles reporting that this script does not deploy: %s\n' "$(printf '%s' "$_OTHER" | tr '\n' ' ')"
fi

# ── 9c. ASK ACA, rather than sampling a key it does not own ────────────────────────────────
#
# The heartbeat check above answers "is the running image the new one". This answers "is there
# exactly ONE running image", which is the question the heartbeat cannot: two replicas on
# different images both write worker_tier_heartbeat:<role> and the row keeps whichever landed
# last, so a sample is evidence and a revision count is an answer.
#
# It is also the thing the warning below has always told the reader to run by hand. Running it
# here costs one `az` call per lane worker on a deploy that has already spent minutes building,
# and it removes the step where a human is trusted to go and look.
#
# Step 8's restore already set these apps to Single mode, under which ACA deactivates the
# previous revision — so more than one active revision here means that did not take effect, which
# is precisely the silent state these no-ingress apps drift into (nothing strands an extra
# revision at 0% traffic the way it would for $APP; it just keeps claiming jobs).
_EXTRA_REVS=""
for a in "${LANE_WORKERS[@]}"; do
  # `|| true` on the query, then a numeric guard: a failed az call must not read as "1 revision".
  _n="$(az containerapp revision list "${AZ[@]}" -g "$RG" -n "$a" \
          --query "length([?properties.active])" -o tsv 2>/dev/null || true)"
  case "$_n" in
    1) ;;
    ''|*[!0-9]*) _EXTRA_REVS="$_EXTRA_REVS $a=unreadable" ;;
    *) _EXTRA_REVS="$_EXTRA_REVS $a=${_n}-active-revisions" ;;
  esac
done

if [ -z "$_STALE" ] && [ -z "$_EXTRA_REVS" ]; then
  printf '\033[32m  ✓ every deployed worker role (%s) is running %s, one active revision each\033[0m\n' \
    "${LANE_ROLES[*]}" "$BUILD_VERSION"
elif [ -z "$_STALE" ]; then
  printf '\n\033[33m  ! worker roles report %s, but ACA cannot confirm one revision each:\033[0m\n' \
    "$BUILD_VERSION" >&2
  printf '     %s\n' $_EXTRA_REVS >&2
  cat >&2 <<REVWARN
    The heartbeat says the new image is running; the revision count says an OLD one is running
    TOO. Both are true — they are different questions. A second active revision has no ingress to
    strand it, so it keeps claiming jobs from the shared queue, and the role key reports whichever
    replica beat last. This is why the tick above is not on its own a pass.

      az containerapp revision list -g $RG -n ${LANE_WORKERS[0]} \\
        --query "[?properties.active].{rev:name,created:properties.createdTime,img:properties.template.containers[0].image}" -o table

    Deactivate the old revision, or check why --mode single did not take.
REVWARN
else
  printf '\n\033[33m  ! deployed worker roles NOT running %s:\033[0m\n' "$BUILD_VERSION" >&2
  printf '      %s\n' $_STALE >&2
  cat >&2 <<WARN
    A worker service is not running the image this deploy shipped, even though its template was
    updated. Scans run in these services, so the estate is being assessed with older code.

    "=absent" means the role never reported at all — that service's replicas are not up, or its
    ACP_WORKER_ROLE does not match the name this script expects (only acp-discovery's is set in
    this repo; the others come from container-app env vars). "=stale-<n>s" means it wrote one beat
    on the right image and stopped. A version means an old revision is still the one beating.

    An old revision is the usual cause — these apps have no ingress, so nothing strands one at 0%
    traffic the way it would for $APP. Check each of ${LANE_WORKERS[*]}:

      az containerapp show -g $RG -n ${LANE_WORKERS[0]} --query properties.configuration.activeRevisionsMode
      az containerapp revision list -g $RG -n ${LANE_WORKERS[0]} \\
        --query "[?properties.active].{rev:name,created:properties.createdTime,img:properties.template.containers[0].image}" -o table

    A role reporting a build older than any recent deploy means that service's replicas were
    never replaced.
WARN
fi

# WARNS, IT DOES NOT DIE, deliberately. A mixed-version estate is genuinely dangerous — nothing
# sequences the tiers (ADR 0045 §6) — but by the time this runs the images have already been
# updated and the app is already serving the new build. Dying here does not unship any of that;
# it only turns a deploy that needs a follow-up into one that reports total failure, and the
# script has no way to put the estate back. Making it fatal is the rollout owner's call, not a
# default this script should adopt on its own. What it must not do is stay silent, which it did.
#
# The ORIGINAL reason recorded here — "this condition is PRE-EXISTING, so dying would red every
# deploy including the one shipping the cleanup" — was true of the retired `processing` role and
# is no longer the reason, because that role is no longer checked. Kept only as the note that the
# rationale changed; a warning is not load-bearing enough to promote to fatal by accident.

# This new gate is deliberately fatal: a crash-and-retry revision is not a clean
# startup, even if its last process is now ready. It never performs an automatic rollback.
_verify_startup "$APP" "${LANE_WORKERS[@]}"
