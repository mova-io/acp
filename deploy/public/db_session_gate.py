"""Wait until PostgreSQL client sessions fit the quiesced staging envelope."""
from __future__ import annotations

import argparse
import time

from schema_preflight import connection_string


def wait_for_budget(dsn: str, ceiling: int, timeout: float = 120) -> int:
    import psycopg2
    deadline = time.monotonic() + timeout
    last = -1
    while time.monotonic() < deadline:
        conn = psycopg2.connect(dsn, connect_timeout=5,
                                options='-c default_transaction_read_only=on -c statement_timeout=5000')
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pg_stat_activity "
                            "WHERE backend_type='client backend' AND pid<>pg_backend_pid()")
                last = int(cur.fetchone()[0])
        finally:
            conn.close()
        if last <= ceiling:
            return last
        time.sleep(3)
    raise RuntimeError(f"client sessions did not fall to the quiesced ceiling ({last}>{ceiling})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--subscription', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--app', required=True)
    parser.add_argument('--ceiling', type=int, required=True)
    args = parser.parse_args(argv)
    dsn = connection_string({}, args.subscription, args.group, args.app)
    count = wait_for_budget(dsn, args.ceiling)
    print(f"database client sessions fit quiesced envelope ({count}<={args.ceiling})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
