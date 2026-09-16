"""Prepare the exact pinned PostgreSQL schema before replacing any serving images.

Azure responses and credentials stay in captured memory. Only allowlisted status fields
leave this process; a failed or timed-out child can never authorize a rollout.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import multiprocessing
import os
import re
from pathlib import Path
import subprocess
import sys
import time


class PreflightRefused(RuntimeError):
    pass


def safe_error(exc):
    code = getattr(exc, 'pgcode', None)
    reason = str(exc) if isinstance(exc, PreflightRefused) else None
    return {'error_type': type(exc).__name__,
            'sqlstate': code if isinstance(code, str) and re.fullmatch('[A-Z0-9]{5}', code) else None,
            'reason': reason if isinstance(reason, str) and re.fullmatch('[a-z_]{1,60}', reason) else None}


def azure(subscription, *args):
    try:
        result = subprocess.run(['az', *args, '--subscription', subscription,
                                 '--only-show-errors', '-o', 'json'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise PreflightRefused('azure_read_failed')
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError):
        raise PreflightRefused('azure_read_failed') from None


def connection_string(app, subscription, group, name):
    try:
        entries = named_container(app, name).get('env', [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) or
                                                not isinstance(entry.get('name'), str)
                                                for entry in entries):
            raise PreflightRefused('container_configuration_unreadable')
        env = {entry['name']: entry for entry in entries}
        entry = env.get('DATABASE_URL', {})
        value = entry.get('value')
        if entry.get('secretRef'):
            secrets = azure(subscription, 'containerapp', 'secret', 'list', '-g', group,
                            '-n', name, '--show-values')
            if not isinstance(secrets, list) or any(not isinstance(secret, dict)
                                                    for secret in secrets):
                raise PreflightRefused('container_configuration_unreadable')
            value = next((secret.get('value') for secret in secrets
                          if secret.get('name') == entry['secretRef']), None)
        if not isinstance(value, str) or not value:
            raise PreflightRefused('postgresql_connection_required')
        return value
    except PreflightRefused:
        raise
    except (KeyError, TypeError, AttributeError):
        raise PreflightRefused('container_configuration_unreadable') from None


def named_container(app, name):
    try:
        properties = app.get('properties') if isinstance(app, dict) else None
        template = properties.get('template') if isinstance(properties, dict) else None
        containers = template.get('containers') if isinstance(template, dict) else None
        if not isinstance(containers, list) or any(not isinstance(c, dict) for c in containers):
            raise PreflightRefused('container_configuration_unreadable')
    except PreflightRefused:
        raise
    except (TypeError, AttributeError):
        raise PreflightRefused('container_configuration_unreadable') from None
    selected = [c for c in containers if c.get('name') == name]
    if len(selected) != 1:
        raise PreflightRefused('role_container_selection_refused')
    return selected[0]


def database_identity(dsn):
    from psycopg2.extensions import parse_dsn
    try:
        fields = parse_dsn(dsn)
        host = fields.get('host', '')
        if not host or ',' in host or not fields.get('dbname'):
            raise ValueError()
        return host, fields.get('hostaddr', ''), fields.get('port', '5432'), fields['dbname']
    except Exception:
        raise PreflightRefused('database_identity_unverifiable') from None


def target_connections(subscription, group, names, environment):
    rows = []
    for name in names:
        if (environment == 'staging') != name.endswith('-staging'):
            raise PreflightRefused('environment_boundary_mismatch')
        app = azure(subscription, 'containerapp', 'show', '-g', group, '-n', name)
        env = {e['name']: e.get('value') for e in named_container(app, name).get('env', [])}
        if env.get('ACP_DEPLOY_ENV') != environment:
            raise PreflightRefused('environment_boundary_mismatch')
        if app['properties']['latestRevisionName'] != app['properties']['latestReadyRevisionName']:
            raise PreflightRefused('target_revision_not_ready')
        rows.append((name, app['properties']['latestRevisionName'],
                     connection_string(app, subscription, group, name)))
    if len({database_identity(row[2]) for row in rows}) != 1:
        raise PreflightRefused('role_database_mismatch')
    return rows


def schema_marker(dsn):
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=5,
                           options='-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000')
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.acp_schema_version')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute('SELECT version, checksum FROM public.acp_schema_version ORDER BY version DESC LIMIT 1')
            return cur.fetchone()
    finally:
        conn.close()


def prepare(dsn, api_path, timeout_seconds=120):
    started = time.monotonic()
    sys.path.insert(0, str(Path(api_path).resolve()))
    store = importlib.import_module('store')
    if Path(store.__file__).resolve().parent != Path(api_path).resolve():
        raise PreflightRefused('pinned_source_path_mismatch')
    adapter = store._PgAdapter
    want = adapter._SCHEMA_VERSION
    checksum = adapter._schema_checksum()
    if checksum != adapter._SCHEMA_CHECKSUM_AT_VERSION:
        raise PreflightRefused('pinned_schema_checksum_mismatch')

    def verified(marker):
        if not marker or marker[0] < want:
            return False
        if marker[0] == want and marker[1] != checksum:
            raise PreflightRefused('target_schema_checksum_mismatch')
        return True

    before = schema_marker(dsn)
    if verified(before):
        return {'state': 'current', 'version': want, 'observed_version': before[0]}
    # Older recovery images may be deployed against an already-prepared schema.
    # A schema-changing pin must contain the bounded migration hook.
    if 'timeout_seconds' not in inspect.signature(adapter.init_schema).parameters or not hasattr(adapter, '_before_schema_ddl'):
        raise PreflightRefused('pinned_migration_bounds_unavailable')

    class DeploymentAdapter(adapter):
        def _before_schema_ddl(self, conn):
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.jobs')")
                if cur.fetchone()[0] is None:
                    return
                # Refuse a held reader immediately, before any DDL. Once elected,
                # the queue check and migration share this transaction/lock.
                cur.execute('LOCK TABLE public.jobs IN ACCESS EXCLUSIVE MODE NOWAIT')
                cur.execute("SELECT COUNT(*) FROM public.jobs WHERE status IN ('queued','running')")
                if cur.fetchone()[0]:
                    raise PreflightRefused('active_jobs_prevent_migration')

    remaining = timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise PreflightRefused('migration_deadline_exceeded')
    DeploymentAdapter(dsn).init_schema(timeout_seconds=remaining)
    after = schema_marker(dsn)
    if not verified(after):
        raise PreflightRefused('migration_marker_not_committed')
    return {'state': 'prepared', 'version': want, 'observed_version': after[0]}


def migration_child(pipe, dsn, api_path, timeout_seconds):
    try:
        pipe.send({'ok': True, **prepare(dsn, api_path, timeout_seconds)})
    except BaseException as exc:
        pipe.send({'ok': False, 'state': 'refused', **safe_error(exc)})
    finally:
        pipe.close()


def bounded_prepare(dsn, api_path, timeout_seconds=120, *, receipt_grace_seconds=15):
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=migration_child, args=(send, dsn, api_path, timeout_seconds))
    child.start()
    send.close()
    try:
        if not receive.poll(timeout_seconds + receipt_grace_seconds):
            raise PreflightRefused('migration_process_deadline_exceeded')
        try:
            receipt = receive.recv()
        except EOFError:
            raise PreflightRefused('migration_receipt_missing') from None
        child.join(5)
        if child.is_alive() or child.exitcode != 0 or not receipt.get('ok'):
            print(json.dumps({'event': 'schema.preflight', **receipt}), flush=True)
            raise PreflightRefused('migration_not_verified')
        return receipt
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
        if child.is_alive():
            child.kill()
            child.join()
        receive.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--environment', choices=('staging', 'production'), required=True)
    parser.add_argument('--api-path', required=True)
    parser.add_argument('apps', nargs='+')
    args = parser.parse_args(argv)
    try:
        rows = target_connections(args.subscription, args.group, args.apps, args.environment)
        receipt = bounded_prepare(rows[0][2], args.api_path)
        # No image update is authorized if the configuration changed during migration.
        after = target_connections(args.subscription, args.group, args.apps, args.environment)
        if rows != after:
            raise PreflightRefused('target_configuration_changed')
        print(json.dumps({'event': 'schema.preflight', **receipt}), flush=True)
        return 0
    except BaseException as exc:
        print(json.dumps({'event': 'schema.preflight', 'ok': False,
                          **safe_error(exc)}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
