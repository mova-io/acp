import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/public'))
import db_session_gate as gate  # noqa: E402
import schema_preflight as schema  # noqa: E402


def app_document(database_entry):
    return {'properties': {'template': {'containers': [{
        'name': 'acp-app-staging', 'env': [database_entry],
    }]}}}


def test_connection_string_resolves_inline_value_without_secret_read(monkeypatch):
    dsn = 'postgresql://inline.example/acp'
    monkeypatch.setattr(schema, 'azure', lambda *_args: pytest.fail('unexpected secret read'))
    assert schema.connection_string(
        app_document({'name': 'DATABASE_URL', 'value': dsn}),
        'sub', 'group', 'acp-app-staging') == dsn


def test_connection_string_resolves_secret_reference_from_realistic_app(monkeypatch):
    dsn = 'postgresql://secret.example/acp'
    calls = []

    def azure(subscription, *args):
        calls.append((subscription, args))
        return [{'name': 'database-url', 'value': dsn}]

    monkeypatch.setattr(schema, 'azure', azure)
    assert schema.connection_string(
        app_document({'name': 'DATABASE_URL', 'secretRef': 'database-url'}),
        'sub', 'group', 'acp-app-staging') == dsn
    assert calls == [('sub', ('containerapp', 'secret', 'list', '-g', 'group',
                              '-n', 'acp-app-staging', '--show-values'))]


@pytest.mark.parametrize('document', [None, {}, {'properties': {}},
                                      {'properties': {'template': {}}},
                                      {'properties': {'template': {'containers': 'bad'}}}])
def test_malformed_app_shape_refuses_without_raw_lookup_errors(document):
    with pytest.raises(schema.PreflightRefused, match='container_configuration_unreadable'):
        schema.connection_string(document, 'sub', 'group', 'acp-app-staging')


def test_main_fetches_app_before_resolving_connection_and_waiting(monkeypatch, capsys):
    document = app_document({'name': 'DATABASE_URL', 'value': 'hidden-dsn'})
    events = []

    def azure(subscription, *args):
        events.append(('azure', subscription, args))
        return document

    def connection(app, subscription, group, name):
        events.append(('connection', app, subscription, group, name))
        return 'hidden-dsn'

    def wait(dsn, ceiling):
        events.append(('wait', dsn, ceiling))
        return 7

    monkeypatch.setattr(gate, 'azure', azure)
    monkeypatch.setattr(gate, 'connection_string', connection)
    monkeypatch.setattr(gate, 'wait_for_budget', wait)
    assert gate.main(['--subscription', 'sub', '--group', 'group',
                      '--app', 'acp-app-staging', '--ceiling', '8']) == 0
    assert [event[0] for event in events] == ['azure', 'connection', 'wait']
    assert events[1][1] is document
    assert '7<=8' in capsys.readouterr().out


def test_main_malformed_azure_document_fails_closed_without_secret_output(monkeypatch, capsys):
    monkeypatch.setattr(gate, 'azure', lambda *_args: {})
    assert gate.main(['--subscription', 'sub', '--group', 'group',
                      '--app', 'acp-app-staging', '--ceiling', '8']) == 1
    output = capsys.readouterr()
    assert output.out.strip() == 'database session gate refused'
    assert output.err == ''
