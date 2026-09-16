from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "deploy/public/redeploy.sh").read_text()
SCALER = (ROOT / "deploy/public/remediation_scaler.sh").read_text()


def test_staging_sizes_every_role_and_leaves_production_api_unchanged():
    assert 'API_ENV_VARS+=("ACP_DB_MAX_CONN=8")' in SCRIPT
    assert '"$DISCOVERY_WORKER"|"$ASSESS_WORKER"|"$REMEDIATE_WORKER") staging_capacity=("ACP_WORKERS=2" "ACP_DB_MAX_CONN=5")' in SCALER
    assert '"$RELEASE_WORKER") staging_capacity=("ACP_WORKERS=3" "ACP_DB_MAX_CONN=6")' in SCALER
    assert 'if [ "${DEPLOY_TARGET_ENV:-production}" = staging ]' in SCALER
    command = SCALER.split('_aca_retry az containerapp update', 1)[1].split('--no-wait', 1)[0]
    assert command.index('"${restore_scale[@]}"') < command.index('--set-env-vars')


def test_current_pool_defaults_are_role_specific():
    current = SCRIPT.split('_current_pool() {', 1)[1].split('\n  }', 1)[0]
    assert 'api) default_workers=0' in current
    assert 'release) default_workers=3' in current
    assert '*) default_workers=2' in current
    assert '_current_pool "$APP" api' in SCRIPT


def test_overbudget_migration_quiesces_and_proves_database_drain_before_api():
    branch = SCRIPT.split('if [ "$DEPLOY_TARGET_ENV" = staging ]; then\n  if [ "$STAGING_DB_BOOTSTRAP" = 1 ]', 1)[1]
    assert branch.index('_quiesce_staging_workers') < branch.index('_update_api_normal')
    quiesce = SCRIPT.split('_quiesce_staging_workers() {', 1)[1].split('\n}', 1)[0]
    assert '--min-replicas 0' in quiesce
    assert 'revision deactivate' in quiesce
    assert 'revision_replica_gate.py' in quiesce and '"$active" = 0' in quiesce
    assert 'db_session_gate.py' in quiesce
    assert quiesce.index('db_session_gate.py') < quiesce.index('_queue_active_after_quiescence')


def test_each_staging_cohort_converges_before_the_next_update():
    branch = SCRIPT.split('say "updating staging API within the PostgreSQL rollout budget"', 1)[1]
    branch = branch.split('else\n  say "updating $APP', 1)[0]
    assert branch.index('_wait_rollout_cohort "$APP"') < branch.index('for a in "${LANE_WORKERS[@]}"')
    loop = branch.split('for a in "${LANE_WORKERS[@]}"; do', 1)[1].split('done', 1)[0]
    assert loop.index('_update_lane_worker "$a"') < loop.index('_wait_rollout_cohort "$a"')
    convergence = SCRIPT.split('_wait_rollout_cohort() {', 1)[1].split('\n}', 1)[0]
    assert 'revisions_json=' in convergence
    assert 'active==[latest]' in convergence
    assert '--revision "$prior"' in convergence
    assert 'old_replicas' in convergence
    assert '"$old_replicas" = 0' in convergence


def test_interrupted_bootstrap_restores_old_worker_images_and_floors():
    restore = SCRIPT.split('_restore_staging_workers() {', 1)[1].split('\n}', 1)[0]
    assert 'STAGING_OLD_IMAGES' in restore
    assert 'STAGING_OLD_MINS' in restore
    assert '--termination-grace-period "$WORKER_TERMINATION_GRACE_SECONDS"' in restore
    assert '_wait_recovery_cohort' in restore
    assert 'recovery failed for $name' in restore
    assert 'trap _staging_bootstrap_exit EXIT' in SCRIPT


def test_quiescence_targets_pre_update_revision_and_accounts_for_new_active_revision():
    quiesce = SCRIPT.split('_quiesce_staging_workers() {', 1)[1].split('\n}', 1)[0]
    before_update = quiesce.split('--min-replicas 0', 1)[0]
    assert 'revision="$(az containerapp revision list' in before_update
    after_update = quiesce.split('--min-replicas 0', 1)[1]
    assert '--revision "$revision"' in after_update
    assert 'while read -r active_revision' in after_update


def test_deliberate_quiescence_does_not_call_worker_startup_or_heartbeat_gate():
    quiesce = SCRIPT.split('_quiesce_staging_workers() {', 1)[1].split('\n}', 1)[0]
    assert '_verify_startup' not in quiesce
    assert 'worker_tier_heartbeat' not in quiesce
