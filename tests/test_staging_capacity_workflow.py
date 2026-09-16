from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/validate-staging-scale-test.yml").read_text()


def test_targets_all_role_specific_staging_workers():
    for name in ("acp-discovery-staging", "acp-assess-staging", "acp-remediate-staging"):
        assert name in WORKFLOW
    assert "'acp-worker-staging'" not in WORKFLOW


def test_is_manual_confirmed_and_staging_guarded():
    assert "workflow_dispatch:" in WORKFLOW
    assert "inputs.confirm_scale_test" in WORKFLOW
    assert WORKFLOW.count("--check-staging-only") >= 2
    assert "ACP_CAPACITY_APPLY_ENABLED" not in WORKFLOW


def test_restores_complete_policy_even_after_failure():
    assert "--query properties.template.scale -o json > original-scale.json" in WORKFLOW
    assert "if: always() && steps.before.outputs.original_min != ''" in WORKFLOW
    assert '"scale": json.load(open("original-scale.json"))' in WORKFLOW
    assert "before == after" in WORKFLOW


def test_checks_limits_identity_and_rule_preservation():
    assert "STAGING_ACA_VCPU_QUOTA" in WORKFLOW
    assert "STAGING_PG_MAX_CONNECTIONS" in WORKFLOW
    assert "STAGING_PG_RESERVED_CONNECTIONS" in WORKFLOW
    assert "identity.principalId" in WORKFLOW
    assert "role assignment list" in WORKFLOW
    assert "Azure changed the existing rule array" in WORKFLOW


def test_staging_deploy_passes_capacity_gateway_inputs_to_redeploy():
    deploy = (ROOT / ".github/workflows/deploy-staging.yml").read_text()
    for name in ("STAGING_CAPACITY_APPLY_ENABLED", "STAGING_ACA_VCPU_QUOTA",
                 "STAGING_PG_MAX_CONNECTIONS", "STAGING_PG_RESERVED_CONNECTIONS"):
        assert name in deploy


def test_redeploy_stamps_gateway_settings_only_on_both_api_rollout_paths():
    script = (ROOT / "deploy/public/redeploy.sh").read_text()
    assert 'CAPACITY_APPLY_ENABLED=0' in script
    assert '[ "$CAPACITY_APPLY_REQUESTED" = 1 ]' in script
    assert '"WORKER_APP_NAMES=$DISCOVERY_WORKER,$ASSESS_WORKER,$REMEDIATE_WORKER${RELEASE_WORKER:+,$RELEASE_WORKER}"' in script
    assert '"CAPACITY_APPLY_APP_NAMES=$APP,$DISCOVERY_WORKER,$ASSESS_WORKER,$REMEDIATE_WORKER,$GPU_APP"' in script
    # Blue-green, normal rollout, and coherent bootstrap recovery all stamp the same API env.
    assert script.count('--env "${API_ENV_VARS[@]}"') == 3
    assert script.count('deploy/public/update_api_image.py') == 3
    assert "ACP_PG_RESERVED_CONNECTIONS must be smaller" in script
    for worker_update in script.split('for a in "${LANE_WORKERS[@]}"; do')[1:3]:
        assert 'API_ENV_VARS' not in worker_update.split("done", 1)[0]


def test_production_deploy_requires_explicit_capacity_opt_in_and_limits():
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "vars.PRODUCTION_CAPACITY_APPLY_ENABLED || '0'" in deploy
    for name in ("PRODUCTION_ACA_VCPU_QUOTA", "PRODUCTION_PG_MAX_CONNECTIONS",
                 "PRODUCTION_PG_RESERVED_CONNECTIONS"):
        assert f"vars.{name}" in deploy
