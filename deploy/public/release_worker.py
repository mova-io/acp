"""Provision a bounded, private Release worker from the existing worker configuration.

Default is validation/dry-run. Secret values are never printed or put in arguments.
The new app starts at zero replicas; storage access is assigned before activation.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'api'))
import queue_scaler
import ast


def release_types():
    tree = ast.parse((ROOT / 'api/core.py').read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'RELEASE_LANE_JOB_TYPES' for t in n.targets))


def definition(source, secrets, name, image, environment):
    if environment not in {'staging', 'production'}:
        raise ValueError('Unknown environment')
    if (name.endswith('-staging')) != (environment == 'staging') or name == source['name']:
        raise ValueError('Release name must be distinct and match its environment')
    props = source['properties']
    template = deepcopy(props['template'])
    if len(template['containers']) != 1:
        raise ValueError('Exactly one source worker container is required')
    container = template['containers'][0]
    env = {e['name']: e for e in container.get('env', [])}
    if env.get('ACP_DEPLOY_ENV', {}).get('value') != environment:
        raise ValueError('Source worker environment does not match')
    if env.get('ACP_WORKER_ROLE', {}).get('value') != 'remediate':
        raise ValueError('Source must be the remediation worker')
    if any(r.get('identity') == 'system' for r in props['configuration'].get('registries', [])):
        raise ValueError('System-identity registry access requires explicit provisioning first')
    if any(s.get('keyVaultUrl') and s.get('identity') == 'system' for s in secrets):
        raise ValueError('System-identity Key Vault references require explicit access provisioning first')
    for key, value in {'ACP_WORKER_ROLE': 'release', 'ACP_WORKERS': '3', 'ACP_DB_MAX_CONN': '3',
                       'ACP_DEDICATED_RELEASE_WORKERS': '1', 'ACP_SHUTDOWN_DRAIN_SECONDS': '540'}.items():
        env[key] = {'name': key, 'value': value}
    container.update(name=name, image=image, env=list(env.values()), resources={'cpu': 1.0, 'memory': '2Gi'})
    # This service runs worker_main; copying web probes would leave it permanently unhealthy.
    container.pop('probes', None)
    # CLI show returns this response-only field; the activation ARM API rejects it.
    container.pop('imageType', None)
    template.pop('revisionSuffix', None)
    template.pop('customMetricsSettings', None)
    template['terminationGracePeriodSeconds'] = 600
    rules = props['template'].get('scale', {}).get('rules', [])
    queues = [r for r in rules if r.get('custom', {}).get('type') == 'postgresql']
    if len(queues) != 1:
        raise ValueError('Exactly one PostgreSQL queue rule is required')
    rule = deepcopy(queues[0]); rule['name'] = 'release-queue'
    rule['custom']['metadata'].update(query=queue_scaler.depth_query(release_types()), targetQueryValue='2')
    template['scale'] = dict(minReplicas=0, maxReplicas=1, pollingInterval=30, cooldownPeriod=300, rules=[rule])
    config = deepcopy(props['configuration'])
    config.pop('ingress', None); config.pop('dapr', None)
    config['activeRevisionsMode'] = 'Single'
    config['secrets'] = secrets
    identity = {'type': 'SystemAssigned'}
    user_identities = source.get('identity', {}).get('userAssignedIdentities')
    if user_identities:
        identity = {'type': 'SystemAssigned, UserAssigned', 'userAssignedIdentities': {k: {} for k in user_identities}}
    return dict(location=source['location'], name=name, type='Microsoft.App/containerApps', identity=identity,
                properties=dict(managedEnvironmentId=props['managedEnvironmentId'], configuration=config, template=template))


def az(*args):
    result = subprocess.run(['az', *args, '-o', 'json'], capture_output=True, text=True)
    if result.returncode:
        # Azure errors can echo request bodies containing secrets. Never replay stderr.
        raise RuntimeError('Azure command failed: ' + ' '.join(args[:2]))
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--subscription', required=True); p.add_argument('--resource-group', required=True)
    p.add_argument('--source', required=True); p.add_argument('--name', required=True)
    p.add_argument('--image', required=True, help='CI-verified image with release-role support')
    p.add_argument('--environment', required=True, choices=['staging', 'production'])
    p.add_argument('--blob-grants-file', type=Path,
        help='Explicit container grants JSON: scope plus reader/contributor role; default copies exact source grants')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    common = ['--subscription', a.subscription, '-g', a.resource_group]
    source = az('containerapp', 'show', *common, '-n', a.source)
    secrets = az('containerapp', 'secret', 'list', *common, '-n', a.source, '--show-values')
    spec = definition(source, secrets, a.name, a.image, a.environment)
    existing = az('containerapp', 'list', *common)
    if any(app['name'] == a.name for app in existing):
        raise ValueError('Target already exists; use the normal guarded redeploy path')
    principal = source['identity']['principalId']
    roles = az('role', 'assignment', 'list', '--subscription', a.subscription,
               '--assignee-object-id', principal, '--all')
    # Copy only the exact existing Blob Data Contributor scopes, never administrative roles.
    blob_roles = [r for r in roles if r['roleDefinitionId'].lower().endswith('/ba92f5b4-2d11-453d-a403-e96b0029c9fe')]
    if not blob_roles:
        raise ValueError('Source has no explicit Blob Data Contributor grant; verify storage access before provisioning')
    if a.blob_grants_file is not None:
        from release_blob_grants import select_blob_grants
        source_env = {e['name']: e for e in source['properties']['template']['containers'][0].get('env', [])}
        blob_roles = select_blob_grants(blob_roles, json.loads(a.blob_grants_file.read_text()),
            account=source_env.get('ACP_BLOB_ACCOUNT', {}).get('value'),
            subscription=a.subscription, environment=a.environment)
    print(f'{a.name}: private Release worker, 1 CPU/2Gi, three slots (two delivery, one report), one replica maximum.')
    if not a.apply:
        print('Validation only. No Azure resources changed.'); return
    # Delete secrets-bearing material even on a failed request.
    with tempfile.TemporaryDirectory(prefix='acp-release-') as directory:
        path = Path(directory) / 'worker.json'; path.write_text(json.dumps(spec)); path.chmod(0o600)
        # No ingress and no scaler: cannot claim a job before identity grants exist.
        active_rules = spec['properties']['template']['scale']['rules']
        spec['properties']['template']['scale']['rules'] = []
        path.write_text(json.dumps(spec))
        created = az('containerapp', 'create', *common, '-n', a.name, '--yaml', str(path))
    new_principal = created['identity']['principalId']
    for role in blob_roles:
        az('role', 'assignment', 'create', '--subscription', a.subscription,
           '--assignee-object-id', new_principal, '--assignee-principal-type', 'ServicePrincipal',
           '--role', role['roleDefinitionId'], '--scope', role['scope'],
           *(['--condition', role['condition'], '--condition-version', role['conditionVersion']]
             if role.get('condition') else []))
    template = spec['properties']['template']
    template['scale'].update(minReplicas=1, rules=active_rules)
    with tempfile.TemporaryDirectory(prefix='acp-release-activate-') as directory:
        path = Path(directory) / 'activate.json'
        path.write_text(json.dumps({'properties': {'template': template}})); path.chmod(0o600)
        az('rest', '--method', 'patch', '--url',
           'https://management.azure.com' + created['id'] + '?api-version=2025-07-01',
           '--body', '@' + str(path))
    print('Worker provisioned. Verify heartbeat and storage access before enabling dedicated routing.')


if __name__ == '__main__':
    main()
