# Sourced by redeploy.sh. Called only after its normal CI/queue/dependency gates.
_prepare_remediation_worker_patch() {
  local live
  # Files inherit mktemp's 0600 permissions; never log the template (env may be
  # sensitive). Keep them under WORK so the existing EXIT trap removes failures.
  live="$(mktemp "$WORK/remediation-live-XXXXXX")"
  REMEDIATION_PATCH="$(mktemp "$WORK/remediation-patch-XXXXXX")"
  _aca_retry az containerapp show "${AZ[@]}" -g "$RG" -n "$REMEDIATE_WORKER" \
    --query '{id:id,properties:{template:properties.template}}' -o json > "$live"
  python3 "$SRC_ROOT/deploy/public/remediation_scaler.py" "$live" "$REMEDIATION_PATCH" "$IMG" \
    "$WORKER_TERMINATION_GRACE_SECONDS" "$WORKER_DRAIN_SECONDS"
  REMEDIATION_RESOURCE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$live")"
  rm -f "$live"
}

_update_lane_worker() {
  local app="$1"
  local staging_capacity=()
  if [ "${DEPLOY_TARGET_ENV:-production}" = staging ]; then
    case "$app" in
      "$DISCOVERY_WORKER"|"$ASSESS_WORKER"|"$REMEDIATE_WORKER") staging_capacity=("ACP_WORKERS=2" "ACP_DB_MAX_CONN=5") ;;
      "$RELEASE_WORKER") staging_capacity=("ACP_WORKERS=3" "ACP_DB_MAX_CONN=6") ;;
    esac
  fi
  if [ "$app" != "$REMEDIATE_WORKER" ]; then
    # Existing Release apps predate the six-connection sizing in release_worker.py. Normalize
    # every image rollout as well as first creation; otherwise the creation-only fix leaves the
    # deployed worker pinned at three connections indefinitely.
    local release_capacity=()
    local restore_scale=()
    if [ "${STAGING_WORKERS_QUIESCED:-0}" = 1 ]; then
      restore_scale=(--min-replicas "$(_staging_old_min "$app")")
    fi
    if [ -n "${RELEASE_WORKER:-}" ] && [ "$app" = "$RELEASE_WORKER" ]; then
      release_capacity=("ACP_WORKERS=3" "ACP_DB_MAX_CONN=6")
    fi
    _aca_retry az containerapp update "${AZ[@]}" -g "$RG" -n "$app" --image "$IMG" \
      --termination-grace-period "$WORKER_TERMINATION_GRACE_SECONDS" \
      "${restore_scale[@]}" \
      --set-env-vars "ACP_SHUTDOWN_DRAIN_SECONDS=$WORKER_DRAIN_SECONDS" \
        "ACP_DEDICATED_RELEASE_WORKERS=${DEDICATED_RELEASE:-0}" "${release_capacity[@]}" \
        "${staging_capacity[@]}" \
        --no-wait -o none
    return
  fi
  # One PATCH preserves the full scale configuration and worker settings.
  _aca_retry az rest --method patch --url "https://management.azure.com${REMEDIATION_RESOURCE_ID}?api-version=2025-07-01" \
    --body "@$REMEDIATION_PATCH" -o none
}

_verify_remediation_scaler() {
  local live
  live="$(mktemp "$WORK/remediation-verify-XXXXXX")"
  _aca_retry az containerapp show "${AZ[@]}" -g "$RG" -n "$REMEDIATE_WORKER" \
    --query properties.template.scale -o json > "$live"
  python3 - "$REMEDIATION_PATCH" "$live" <<'PYVERIFY'
import json, sys
expected = json.load(open(sys.argv[1]))['properties']['template']['scale']
actual = json.load(open(sys.argv[2]))
if actual != expected:
    raise SystemExit('remediation scaler verification failed: live scale differs from preserved target')
print('  remediation queue query and existing scale settings verified')
PYVERIFY
  rm -f "$live" "$REMEDIATION_PATCH"
}
