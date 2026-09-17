import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_real_uvicorn_lifespan_failure_emits_root_sanitized_evidence(tmp_path):
    process = tmp_path / 'process_main.py'
    process.write_text((ROOT / 'api/process_main.py').read_text())
    (tmp_path / 'app.py').write_text('''
from fastapi import FastAPI
app = FastAPI()
@app.on_event("startup")
def fail():
    raise RuntimeError("DATABASE_URL=postgresql://user:secret@host/db")
''')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'PORT': '0',
           'CONTAINER_APP_REVISION': 'api--test'}
    env.pop('DATABASE_URL', None)
    result = subprocess.run([sys.executable, str(process)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 3
    rows = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{')]
    assert rows == [{'event': 'startup.process', 'state': 'failed',
                     'error_type': 'RuntimeError', 'sqlstate': None}]
    assert 'secret' not in result.stderr and 'DATABASE_URL' not in result.stderr
    assert 'details redacted' in result.stderr


def test_durable_receipt_is_first_write_wins(monkeypatch):
    sys.path.insert(0, str(ROOT / 'api'))
    import process_main
    statements = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, params): statements.append((sql, params))

    class Connection:
        autocommit = False
        def cursor(self): return Cursor()
        def close(self): pass

    class Driver:
        @staticmethod
        def connect(*_args, **_kwargs): return Connection()

    monkeypatch.setitem(sys.modules, 'psycopg2', Driver)
    monkeypatch.setenv('DATABASE_URL', 'redacted-dsn')
    monkeypatch.setenv('CONTAINER_APP_REVISION', 'api--test')
    process_main.record_failure(RuntimeError('credential=secret'))
    sql, params = statements[0]
    assert 'ON CONFLICT(key) DO NOTHING' in sql
    assert params[0] == 'startup_failure:api--test'
    assert json.loads(params[1]) == {'revision': 'api--test', 'phase': 'process_startup',
                                     'error_type': 'RuntimeError', 'sqlstate': None}
    assert 'credential' not in params[1] and 'secret' not in params[1]
