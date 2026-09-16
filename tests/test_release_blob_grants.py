"""Offline permission-subset fixtures for explicit Release container grants."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_blob_grants', ROOT / 'deploy/public/release_blob_grants.py')
grants = importlib.util.module_from_spec(spec); spec.loader.exec_module(grants)
SUB = '8fab0f8f-b577-45d7-a485-ec32f73b22be'
ACCOUNT = f'/subscriptions/{SUB}/resourceGroups/mdk-accessibility/providers/Microsoft.Storage/storageAccounts/acpremediatedstore'


def source(scope=ACCOUNT, **extra):
    return {'scope': scope, 'roleDefinitionId': f'/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{grants.CONTRIBUTOR}', **extra}


def container(name='sources', account=ACCOUNT):
    return account + '/blobServices/default/containers/' + name


def select(requested, sources=None, **extra):
    return grants.select_blob_grants([source()] if sources is None else sources, requested,
        account='acpremediatedstore', subscription=SUB, environment=extra.get('environment', 'production'))


def test_reader_and_contributor_are_narrow_subsets_without_changing_source():
    original = source()
    chosen = select([{'scope': container(), 'role': 'reader'},
        {'scope': container('release-packages'), 'role': 'contributor'}], [original])
    assert [r['scope'] for r in chosen] == [container(), container('release-packages')]
    assert chosen[0]['roleDefinitionId'].endswith('/' + grants.READER)
    assert chosen[1]['roleDefinitionId'].endswith('/' + grants.CONTRIBUTOR)
    assert original == source()


@pytest.mark.parametrize('scope', [ACCOUNT, ACCOUNT.rsplit('/providers', 1)[0], f'/subscriptions/{SUB}',
    container(account=ACCOUNT.replace('acpremediatedstore', 'otheraccount')),
    container().replace(SUB, '00000000-0000-0000-0000-000000000000'),
    container('acp-staging-sources'), container('bad--name'), container() + '/blob/secret', container() + '/'])
def test_noncontainer_otheraccount_other_subscription_and_staging_are_rejected(scope):
    with pytest.raises(ValueError):
        select([{'scope': scope, 'role': 'contributor'}])


@pytest.mark.parametrize('requested', [[], {}, [{'scope': container(), 'role': 'owner'}],
    [{'scope': container(), 'role': 'reader', 'condition': 'override'}],
    [{'scope': container(), 'role': 'reader'}, {'scope': container().upper(), 'role': 'contributor'}]])
def test_malformed_widening_and_duplicate_requests_fail(requested):
    with pytest.raises(ValueError):
        select(requested)


def test_container_source_cannot_grant_sibling_and_nonblob_role_cannot_grant():
    with pytest.raises(ValueError):
        select([{'scope': container('release-packages'), 'role': 'contributor'}], [source(container())])
    with pytest.raises(ValueError):
        select([{'scope': container(), 'role': 'reader'}], [source(roleDefinitionId='owner')])


def test_conditional_exact_clone_retains_conditions_but_narrowing_and_downgrade_fail():
    conditional = source(container(), condition='original restriction', conditionVersion='2.0')
    chosen = select([{'scope': container(), 'role': 'contributor'}], [conditional])
    assert chosen == [conditional]
    with pytest.raises(ValueError):
        select([{'scope': container(), 'role': 'reader'}], [conditional])
    with pytest.raises(ValueError):
        select([{'scope': container(), 'role': 'contributor'}], [source(condition='restriction', conditionVersion='2.0')])


def test_explicit_staging_scope_selection_keeps_environment_boundary():
    chosen = select([{'scope': container('acp-staging-sources'), 'role': 'reader'}], environment='staging')
    assert chosen[0]['scope'] == container('acp-staging-sources')
    with pytest.raises(ValueError):
        select([{'scope': container(), 'role': 'reader'}], environment='staging')


@pytest.mark.parametrize('bad_scope', ['', None])
def test_missing_source_scope_never_authorizes_any_container(bad_scope):
    with pytest.raises(ValueError):
        select([{'scope': container(), 'role': 'reader'}], [source(bad_scope)])


def test_scope_boundary_does_not_allow_a_similarly_named_container():
    with pytest.raises(ValueError):
        select([{'scope': container('sources-extra'), 'role': 'reader'}], [source(container())])


@pytest.fixture
def provisioner(monkeypatch):
    from test_release_worker_lane import module, source as worker_source
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/public'))
    worker = module('release_worker')
    original = worker_source()
    original['identity']['principalId'] = 'source-principal'
    original['properties']['template']['containers'][0]['env'].append(
        {'name': 'ACP_BLOB_ACCOUNT', 'value': 'acpremediatedstore'})
    calls, source_grants = [], [source()]
    def azure(*args):
        calls.append(args)
        if args[:2] == ('containerapp', 'show'): return original
        if args[:3] == ('containerapp', 'secret', 'list'):
            return [{'name': 'database', 'value': 'DO_NOT_LOG_SECRET'}]
        if args[:2] == ('containerapp', 'list'): return []
        if args[:3] == ('role', 'assignment', 'list'): return source_grants
        if args[:2] == ('containerapp', 'create'):
            path = Path(args[args.index('--yaml') + 1])
            definition = __import__('json').loads(path.read_text())
            assert definition['properties']['template']['scale']['minReplicas'] == 0
            assert definition['properties']['template']['scale']['rules'] == []
            return {'id': '/release-app', 'identity': {'principalId': 'new-principal'}}
        if args[:3] == ('role', 'assignment', 'create'): return {}
        if args[0] == 'rest': return {}
        pytest.fail('Unexpected Azure operation')
    monkeypatch.setattr(worker, 'az', azure)
    return worker, calls, source_grants


def arguments(monkeypatch, *, file=None, apply=True):
    args = ['release_worker.py', '--subscription', SUB, '--resource-group', 'mdk-accessibility',
        '--source', 'acp-remediate', '--name', 'acp-release', '--image', 'CI_VERIFIED_IMAGE',
        '--environment', 'production']
    if file is not None: args += ['--blob-grants-file', str(file)]
    if apply: args += ['--apply']
    monkeypatch.setattr('sys.argv', args)


def test_full_apply_assigns_only_selected_grants_before_activation(provisioner, tmp_path, monkeypatch, capsys):
    import json
    worker, calls, original_roles = provisioner
    path = tmp_path / 'grants.json'
    path.write_text(json.dumps([{'scope': container(), 'role': 'reader'},
        {'scope': container('release-packages'), 'role': 'contributor'}]))
    arguments(monkeypatch, file=path)
    worker.main()
    assigned = [args for args in calls if args[:3] == ('role', 'assignment', 'create')]
    assert len(assigned) == 2
    assert [a[a.index('--scope') + 1] for a in assigned] == [container(), container('release-packages')]
    assert assigned[0][assigned[0].index('--role') + 1].endswith(grants.READER)
    assert all(a[a.index('--assignee-object-id') + 1] == 'new-principal' for a in assigned)
    assert calls.index(assigned[-1]) < next(i for i, a in enumerate(calls) if a[0] == 'rest')
    assert original_roles == [source()]
    assert 'DO_NOT_LOG_SECRET' not in capsys.readouterr().out


def test_default_apply_preserves_exact_conditional_source_clone(provisioner, monkeypatch):
    worker, calls, original_roles = provisioner
    original_roles[0].update(condition='exact-source-condition', conditionVersion='2.0')
    arguments(monkeypatch)
    worker.main()
    assigned = next(a for a in calls if a[:3] == ('role', 'assignment', 'create'))
    assert assigned[assigned.index('--scope') + 1] == ACCOUNT
    assert assigned[assigned.index('--condition') + 1] == 'exact-source-condition'
    assert assigned[assigned.index('--condition-version') + 1] == '2.0'


def test_unauthorized_selection_fails_before_create_or_grant(provisioner, tmp_path, monkeypatch):
    worker, calls, _ = provisioner
    path = tmp_path / 'grants.json'
    path.write_text('[{"scope":"' + ACCOUNT + '","role":"contributor"}]')
    arguments(monkeypatch, file=path)
    with pytest.raises(ValueError): worker.main()
    assert not any(a[:2] == ('containerapp', 'create') or a[:3] == ('role', 'assignment', 'create')
        or a[0] == 'rest' for a in calls)


def test_explicit_dryrun_never_mutates_azure(provisioner, tmp_path, monkeypatch):
    worker, calls, _ = provisioner
    path = tmp_path / 'grants.json'
    path.write_text('[{"scope":"' + container() + '","role":"reader"}]')
    arguments(monkeypatch, file=path, apply=False)
    worker.main()
    assert not any(a[:2] == ('containerapp', 'create') or a[:3] == ('role', 'assignment', 'create')
        or a[0] == 'rest' for a in calls)


def test_definition_strips_response_only_image_type_without_changing_source():
    from test_release_worker_lane import module, source as worker_source
    worker = module('release_worker'); original = worker_source()
    original['properties']['template']['containers'][0]['imageType'] = 'ContainerImage'
    original['properties']['template']['customMetricsSettings'] = {'inactive': True}
    definition = worker.definition(original, [], 'acp-release', 'verified', 'production')
    assert 'imageType' not in definition['properties']['template']['containers'][0]
    assert 'customMetricsSettings' not in definition['properties']['template']
    assert original['properties']['template']['containers'][0]['imageType'] == 'ContainerImage'
    assert original['properties']['template']['customMetricsSettings'] == {'inactive': True}
