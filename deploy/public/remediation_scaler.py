"""Build a narrow ACA template PATCH; never print live settings or secret values.

The CLI's --scale-rule flags replace the rules list. Copy the existing template
instead, changing only the normal worker rollout fields and our queue predicate.
https://learn.microsoft.com/en-us/rest/api/resource-manager/containerapps/container-apps/update
"""
from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def remediation_query(root=ROOT, dedicated=False):
    # Deploy runners intentionally do not install the application's Python stack.
    # Read the authoritative literal without importing core or starting its store.
    tree = ast.parse((root / 'api/core.py').read_text())
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign) and any(
                  isinstance(t, ast.Name) and t.id == 'REMEDIATE_LANE_JOB_TYPES'
                  for t in node.targets)]
    if len(values) != 1 or not isinstance(values[0], tuple) or not values[0] or any(
            not isinstance(v, str) or not v for v in values[0]):
        raise ValueError('authoritative remediation lane must be a nonempty literal tuple')
    module_spec = importlib.util.spec_from_file_location('target_queue_scaler', root / 'api/queue_scaler.py')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    kinds = values[0]
    if dedicated:
        release = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                       t.id == 'RELEASE_LANE_JOB_TYPES' for t in node.targets))
        kinds = tuple(kind for kind in kinds if kind not in release)
    return module.depth_query(kinds)


def worker_patch(template, image, grace, drain, query):
    result = deepcopy(template)
    containers = result.get('containers', [])
    # The existing image-only rollout assumes a single worker container. Refuse
    # ambiguity rather than accidentally replacing a sidecar image or its env.
    if len(containers) != 1:
        raise ValueError('remediation worker must have exactly one container')
    rules = result.get('scale', {}).get('rules', [])
    # staging_up.sh provisions through deploy.sh, which names this PostgreSQL
    # rule jobs-queued. Preserve either deployed name and its scale/auth settings.
    # Both names together are ambiguous: do not leave a second broad queue rule.
    matches = [r for r in rules if r.get('name') in ('remediation-queue', 'jobs-queued')]
    if len(matches) != 1:
        raise ValueError('exactly one existing remediation-queue or jobs-queued rule required')
    custom = matches[0].get('custom', {})
    metadata = custom.get('metadata', {})
    if custom.get('type') != 'postgresql' or not metadata.get('query') or not metadata.get('targetQueryValue'):
        raise ValueError('existing PostgreSQL queue rule and target required')
    metadata['query'] = query
    containers[0]['image'] = image
    env = containers[0].setdefault('env', [])
    entries = [e for e in env if e.get('name') == 'ACP_SHUTDOWN_DRAIN_SECONDS']
    if len(entries) > 1:
        raise ValueError('duplicate worker drain setting')
    if entries:
        entries[0].clear()
        entries[0].update(name='ACP_SHUTDOWN_DRAIN_SECONDS', value=str(drain))
    else:
        env.append({'name': 'ACP_SHUTDOWN_DRAIN_SECONDS', 'value': str(drain)})
    result['terminationGracePeriodSeconds'] = int(grace)
    # A suffix is a revision identifier, not persistent worker configuration.
    # Let ACA allocate the new revision as the existing image-only update does.
    result['revisionSuffix'] = ''
    return {'properties': {'template': result}}


def main():
    source, dest, image, grace, drain = sys.argv[1:]
    data = json.loads(Path(source).read_text())
    dedicated = os.environ.get('ACP_DEDICATED_RELEASE_WORKERS') == '1'
    patch = worker_patch(data['properties']['template'], image, grace, drain,
                         remediation_query(Path.cwd(), dedicated=dedicated))
    env = patch['properties']['template']['containers'][0].setdefault('env', [])
    env[:] = [entry for entry in env if entry.get('name') != 'ACP_DEDICATED_RELEASE_WORKERS']
    env.append({'name': 'ACP_DEDICATED_RELEASE_WORKERS', 'value': '1' if dedicated else '0'})
    if os.environ.get('ACP_DEPLOY_TARGET_ENV') == 'staging':
        env[:] = [entry for entry in env if entry.get('name') not in
                  ('ACP_WORKERS', 'ACP_DB_MAX_CONN')]
        env.extend(({'name': 'ACP_WORKERS', 'value': '2'},
                    {'name': 'ACP_DB_MAX_CONN', 'value': '5'}))
    Path(dest).write_text(json.dumps(patch))


if __name__ == '__main__':
    main()
