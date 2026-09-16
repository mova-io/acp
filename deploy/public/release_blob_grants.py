"""Validate explicit Release storage grants as subsets of existing source grants."""
from copy import deepcopy
import re

CONTRIBUTOR = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
READER = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
CONTAINER_SCOPE = re.compile(
    r'/subscriptions/([0-9a-f-]{36})/resourcegroups/([^/]+)/providers/microsoft\.storage/'
    r'storageaccounts/([a-z0-9]{3,24})/blobservices/default/containers/([a-z0-9][a-z0-9-]{1,61}[a-z0-9])')


def select_blob_grants(source_roles, requested, *, account, subscription, environment):
    """No conditions are inferred: conditional grants require exact scope and role."""
    if environment not in {'production', 'staging'}:
        raise ValueError('Unknown grant environment')
    if not isinstance(account, str) or not re.fullmatch(r'[a-z0-9]{3,24}', account):
        raise ValueError('Explicit grants require the intended ACP_BLOB_ACCOUNT value')
    if not isinstance(requested, list) or not requested:
        raise ValueError('Explicit grants must be a non-empty list')
    sources = [r for r in source_roles if str(r.get('roleDefinitionId', '')).lower().endswith('/' + CONTRIBUTOR)]
    output, seen = [], set()
    for grant in requested:
        if not isinstance(grant, dict) or set(grant) != {'scope', 'role'}:
            raise ValueError('Each grant requires exactly scope and role')
        scope, role = grant['scope'], grant['role']
        if not isinstance(scope, str) or not isinstance(role, str) or role not in {'reader', 'contributor'}:
            raise ValueError('Grant role must be reader or contributor')
        normalized = scope.lower()
        match = CONTAINER_SCOPE.fullmatch(normalized)
        if not match or match[1] != subscription.lower() or match[3] != account:
            raise ValueError('Grant must select a container in the intended account and subscription')
        container = match[4]
        if '--' in container:
            raise ValueError('Invalid container name')
        if (environment == 'production' and 'staging' in container) or (
                environment == 'staging' and 'staging' not in container):
            raise ValueError('Container does not match the deployment environment')
        if normalized in seen:
            raise ValueError('Duplicate container scope')
        seen.add(normalized)
        parents = [r for r in sources if isinstance(r.get('scope'), str) and r['scope'] and (
            normalized == r['scope'].lower().rstrip('/')
            or normalized.startswith(r['scope'].lower().rstrip('/') + '/'))]
        # An unconditional parent authorizes its subset. A conditional parent cannot
        # be rewritten or downgraded safely; an exact contributor clone retains it.
        permitted = [r for r in parents if not r.get('condition') or (
            normalized == str(r['scope']).lower().rstrip('/') and role == 'contributor')]
        if not permitted:
            raise ValueError('Selected grant is not authorized by a safely retained source Blob grant')
        source = next((r for r in permitted if not r.get('condition')), permitted[0])
        selected = deepcopy(source)
        selected['scope'] = scope
        if role == 'reader':
            selected['roleDefinitionId'] = source['roleDefinitionId'].rsplit('/', 1)[0] + '/' + READER
        output.append(selected)
    return output
