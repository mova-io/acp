"""Add a bounded Startup gate in the same revision as the API image update.

Readiness and liveness remain unchanged. Captured templates may contain secrets;
they are never printed and the PATCH file is private and removed on every path.

EVERY PATCH MUST NAME ITS OWN REVISION SUFFIX. This endpoint "patches a Container
App using JSON Merge Patch" (Microsoft.App/containerApps update), so a member the
body omits is LEFT UNCHANGED rather than cleared -- dropping revisionSuffix does
not ask ACA to allocate a name, it re-uses whichever explicit suffix the resource
already carries. A revision name is <app>--<suffix> and is a unique identifier of
an immutable revision, so the next image-only rollout then asks for an existing
revision under a different template. Production twice retained its old image after
an accepted PATCH while carrying an explicit suffix; a fresh named revision recovered
the rollout. Naming every new revision avoids depending on omission to reset the
previous suffix. Acceptance is still verified by the rollout's readiness/version gates.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

API_VERSION = '2025-10-02-preview'
# Documented revision-suffix rules: lower-case alphanumerics and dashes, leading
# alphabetic, trailing alphanumeric, no doubled dash, and the revision NAME it
# forms is bounded at 64 characters.
REVISION_SUFFIX = re.compile(r'^[a-z]([a-z0-9-]*[a-z0-9])?$')
REVISION_NAME_LIMIT = 64
STARTUP_PROBE = {
    'type': 'Startup',
    'httpGet': {'path': '/healthz', 'port': 8077, 'scheme': 'HTTP'},
    'initialDelaySeconds': 1, 'periodSeconds': 10, 'timeoutSeconds': 2,
    'successThreshold': 1, 'failureThreshold': 10,
}


def valid_suffix(app, suffix):
    """A suffix ACA will accept, and a revision name short enough to carry it."""
    return bool(REVISION_SUFFIX.fullmatch(suffix)) and '--' not in suffix \
        and len(app) + len('--') + len(suffix) <= REVISION_NAME_LIMIT


def derived_suffix(app, image, token=None):
    """Name the rollout after its image tag, plus a token so a repeat of the same
    image asks for a NEW revision instead of colliding with the one it already
    provisioned. An explicit caller suffix (blue-green) is never reached here."""
    token = token or secrets.token_hex(3)
    tag = re.sub(r'[^a-z0-9]+', '-', image.rpartition(':')[2].lower()).strip('-')
    room = REVISION_NAME_LIMIT - len(app) - len('--') - len(token) - len('-')
    if room < 1 or not re.fullmatch(r'[a-z0-9]+', token):
        raise ValueError('image cannot form a safe Container Apps revision suffix')
    return ('r' + tag)[:room].rstrip('-') + '-' + token


def update_template(template, app, image, env, revision_suffix=None, token=None):
    result = deepcopy(template)
    matches = [c for c in result['containers'] if c.get('name') == app]
    if len(matches) != 1:
        raise ValueError('API container selection refused')
    container = matches[0]
    probes = container.get('probes') or []
    startup = [p for p in probes if p.get('type') == 'Startup']
    if len(startup) > 1:
        raise ValueError('duplicate Startup probes refused')
    if startup:
        p = startup[0]
        http = p.get('httpGet', {})
        tcp = p.get('tcpSocket', {})
        gates_api = ((not tcp and http.get('port') == 8077 and http.get('path') == '/healthz'
                      and http.get('scheme', 'HTTP') == 'HTTP')
                     or (not http and tcp.get('port') == 8077))
        if not gates_api:
            raise ValueError('configured Startup endpoint requires explicit review')
        budget = p.get('initialDelaySeconds', 0) + p.get('periodSeconds', 10) * (p.get('failureThreshold', 3) - 1)
        if budget < 90:
            raise ValueError('configured Startup budget requires explicit review')
    else:
        container['probes'] = [*probes, deepcopy(STARTUP_PROBE)]
    container['image'] = image
    values = {e['name']: e for e in container.get('env', [])}
    for name, value in env.items():
        values[name] = {'name': name, 'value': value}
    container['env'] = list(values.values())
    result['revisionSuffix'] = revision_suffix or derived_suffix(app, image, token)
    if not valid_suffix(app, result['revisionSuffix']):
        raise ValueError('revision suffix requires explicit review')
    # These are response-only in the pinned PATCH API, as in the scaler repair.
    for c in result['containers']:
        c.pop('imageType', None)
    result.get('scale', {}).pop('customMetricsSettings', None)
    return result


def azure(subscription, *args):
    result = subprocess.run(['az', *args, '--subscription', subscription,
                             '--only-show-errors', '-o', 'json'],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        if 'ContainerAppOperationInProgress' in result.stderr:
            raise RuntimeError('ContainerAppOperationInProgress')
        if 'conflicting concurrent write' in result.stderr.lower():
            raise RuntimeError('conflicting concurrent write')
        raise RuntimeError('API image update provider failure')
    return json.loads(result.stdout) if result.stdout.strip() else None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--app', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--revision-suffix')
    parser.add_argument('--env', nargs='*', default=[])
    args = parser.parse_args(argv)
    path = None
    try:
        env = dict(value.split('=', 1) for value in args.env)
        app = azure(args.subscription, 'containerapp', 'show', '-g', args.group, '-n', args.app)
        expected_id = (f'/subscriptions/{args.subscription}/resourceGroups/{args.group}'
                       f'/providers/Microsoft.App/containerApps/{args.app}')
        if app['id'].lower() != expected_id.lower():
            raise ValueError('API resource boundary mismatch')
        template = update_template(app['properties']['template'], args.app, args.image,
                                   env, args.revision_suffix)
        fd, name = tempfile.mkstemp(prefix='acp-api-image-', suffix='.json')
        path = Path(name)
        with os.fdopen(fd, 'w') as output:
            json.dump({'properties': {'template': template}}, output)
        azure(args.subscription, 'rest', '--method', 'patch', '--url',
              'https://management.azure.com' + app['id'] + '?api-version=' + API_VERSION,
              '--body', '@' + str(path))
        print('API image revision accepted with bounded Startup gate: '
              + args.app + '--' + template['revisionSuffix'], flush=True)
        return 0
    except Exception as exc:
        # Only busy categories are exposed for the existing bounded retry matcher.
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        if reason not in ('ContainerAppOperationInProgress', 'conflicting concurrent write'):
            reason = 'API image update refused'
        print(reason, file=sys.stderr)
        return 1
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
