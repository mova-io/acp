"""Late CI events must not roll back a newer live staging build."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('staging_pin_guard', ROOT/'deploy/public/staging_pin_guard.py')
guard=importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

@pytest.fixture
def history(tmp_path):
    def git(*a):
        return subprocess.check_output(['git','-C',str(tmp_path),*a],text=True).strip()
    git('init','-q');git('config','user.email','fixture@example.test');git('config','user.name','Fixture')
    git('commit','--allow-empty','-qm','older');older=git('rev-parse','HEAD')
    git('commit','--allow-empty','-qm','newer');newer=git('rev-parse','HEAD')
    git('checkout','-qb','diverged',older);git('commit','--allow-empty','-qm','other');other=git('rev-parse','HEAD')
    return str(tmp_path),older,newer,other

def health(sha):return {'commit':sha,'version_stamped':True}

def test_late_workflow_event_uses_actual_pin_not_default_branch_metadata(history):
    repo,old,new,_=history
    event={'workflow_run':{'head_sha':old},'default_branch_head':new}
    assert guard.decision(repo,event['workflow_run']['head_sha'],health(new),'automatic')=='skip'
    assert guard.decision(repo,new,health(old),'automatic')=='deploy'
    assert guard.decision(repo,new,health(new),'automatic')=='deploy'

@pytest.mark.parametrize('mode,rollback,allowed',[('manual',False,False),('manual',True,True),('automatic',True,False)])
def test_rollback_requires_explicit_manual_intent(history,mode,rollback,allowed):
    repo,old,new,_=history
    if allowed:assert guard.decision(repo,old,health(new),mode,rollback)=='deploy'
    else:
        with pytest.raises(ValueError):guard.decision(repo,old,health(new),mode,rollback)

@pytest.mark.parametrize('live',[{}, {'commit':'short','version_stamped':True}, {'commit':'a'*40,'version_stamped':False}, {'commit':'b'*40,'version_stamped':True}])
def test_unverifiable_live_state_fails_closed_with_explicit_recovery(history,live):
    repo,old,_,_=history
    with pytest.raises(ValueError):guard.decision(repo,old,live,'automatic')
    assert guard.decision(repo,old,live,'manual',True)=='deploy'

def test_divergence_requires_recovery_intent(history):
    repo,_,new,other=history
    with pytest.raises(ValueError):guard.decision(repo,new,health(other),'automatic')

def test_guard_runs_before_ci_build_or_mutation_and_workflow_executes_current_code():
    script=(ROOT/'deploy/public/redeploy.sh').read_text()
    assert script.index('GUARD_DECISION=')<script.index('# The CI gate, ENFORCED')
    workflow=(ROOT/'.github/workflows/deploy-staging.yml').read_text()
    assert 'ref: ${{ github.workflow_sha }}' in workflow
    assert 'staging commit differs from requested pin' in workflow
    assert "if: steps.staging_deploy.outputs.staging_deployed == 'true'" in workflow
    assert "ACP_PIN: ${{ github.event.workflow_run.head_sha || inputs.pin }}" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.allow_rollback" in workflow

def test_real_shell_guard_skips_without_reaching_ci_or_build(history,tmp_path):
    import os
    repo,old,new,_=history
    # Execute the production guard block against disposable ancestry and fake transport.
    text=(ROOT/'deploy/public/redeploy.sh').read_text()
    block=text[text.index('if [ "$DEPLOY_TARGET_ENV" = staging ] &&'):text.index('# The CI gate, ENFORCED')]
    target=Path(repo)/'deploy/public';target.mkdir(parents=True)
    (target/'staging_pin_guard.py').write_text((ROOT/'deploy/public/staging_pin_guard.py').read_text())
    tools=tmp_path/'bin';tools.mkdir()
    (tools/'az').write_text('#!/bin/sh\necho staging.example.test\n')
    (tools/'curl').write_text('#!/bin/sh\nprintf \'%s\' \'{"commit":"'+new+'","version_stamped":true}\'\n')
    for tool in tools.iterdir():tool.chmod(0o755)
    output=tmp_path/'output'
    env=dict(os.environ,PATH=str(tools)+os.pathsep+os.environ['PATH'],SRC_ROOT=repo,PIN=old,DEPLOY_TARGET_ENV='staging',ACP_STAGING_DEPLOY_MODE='automatic',GITHUB_OUTPUT=str(output),ACP_PIN=old)
    result=subprocess.run(['bash','-c','set -euo pipefail\nAZ=(--subscription fixture);RG=fixture;APP=fixture-staging\nsay(){ echo "$*"; };die(){ exit 1; }\n'+block+'echo CI_OR_BUILD_REACHED'],env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert 'CI_OR_BUILD_REACHED' not in result.stdout
    assert output.read_text()=='staging_deployed=false\n'


def test_tooling_ci_is_separate_and_has_no_manual_or_skip_exemption():
    workflow=(ROOT/'.github/workflows/deploy-staging.yml').read_text()
    block=workflow.split('      - name: Verify deployment tooling CI',1)[1].split('      - uses: actions/setup-dotnet',1)[0]
    assert 'TOOLING_SHA: ${{ github.workflow_sha }}' in block
    assert 'gh run watch "$RUN_ID" --interval 60 --exit-status' in block
    assert '--commit "$TOOLING_SHA"' in block
    assert 'skip_ci_gate' not in block and 'allow_rollback' not in block
    assert workflow.index('Verify deployment tooling CI') < workflow.index('Azure login (OIDC)')

@pytest.mark.parametrize('ci_exit',[0,1])
def test_actual_tooling_gate_rejects_failed_ci(history,tmp_path,ci_exit):
    import os
    import textwrap
    repo,_,_,toolsha=history
    workflow=(ROOT/'.github/workflows/deploy-staging.yml').read_text()
    block=workflow.split('      - name: Verify deployment tooling CI',1)[1].split('      - uses: actions/setup-dotnet',1)[0]
    run=textwrap.dedent(block.split('        run: |\n',1)[1])
    tools=tmp_path/'gate-bin';tools.mkdir()
    (tools/'gh').write_text('#!/bin/sh\nif [ "$2" = list ]; then echo 123; else exit '+str(ci_exit)+'; fi\n')
    (tools/'gh').chmod(0o755)
    env=dict(os.environ,PATH=str(tools)+os.pathsep+os.environ['PATH'],TOOLING_SHA=toolsha)
    result=subprocess.run(['bash','-c',run+'\necho DEPLOY_REACHED\n'],cwd=repo,env=env,text=True,capture_output=True)
    assert result.returncode==ci_exit,result.stderr
    assert ('DEPLOY_REACHED' in result.stdout)==(ci_exit==0)


def test_confirmation_uses_resolved_pin_even_if_branch_moves(history):
    repo,old,new,_=history
    subprocess.run(['git','-C',repo,'branch','deploy-ref',old],check=True)
    frozen=subprocess.check_output(['git','-C',repo,'rev-parse','deploy-ref'],text=True).strip()
    subprocess.run(['git','-C',repo,'branch','-f','deploy-ref',new],check=True)
    assert subprocess.check_output(['git','-C',repo,'rev-parse','deploy-ref'],text=True).strip()!=frozen
    workflow=(ROOT/'.github/workflows/deploy-staging.yml').read_text()
    confirm=workflow.split('      - name: Confirm staging reports the new build',1)[1]
    assert 'ACP_EXPECTED_PIN: ${{ steps.staging_deploy.outputs.staging_pin }}' in confirm
    assert 'git rev-parse' not in confirm
    script=(ROOT/'deploy/public/redeploy.sh').read_text()
    assert 'echo "staging_pin=$PIN"' in script


def test_rollback_rejects_ci_bypass_before_authorization():
    script=(ROOT/'deploy/public/redeploy.sh').read_text()
    block=script.split('  if [ "${ACP_STAGING_ALLOW_ROLLBACK:-0}" = 1 ]; then',1)[1].split('  fi',1)[0]
    result=subprocess.run(['bash','-c','set -eu; die(){ echo "$*" >&2; exit 1; }; '+block],env={'ACP_PIN':'a'*40,'ACP_SKIP_CI_GATE':'1'},text=True,capture_output=True)
    assert result.returncode==1
    assert 'cannot bypass image CI' in result.stderr
