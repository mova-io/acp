"""Copy a quiescent transient database without printing secrets or extending TTLs.

URLs are supplied through environment variables. The default is a read-only
preflight; operators must hold admission before explicitly enabling the copy.
"""
import argparse
import hashlib
import json
import os
import time
from urllib.parse import urlsplit


class MigrationRefused(RuntimeError):
    pass


def validate_url(url, *, target=False):
    parts = urlsplit(url)
    if (parts.scheme not in {'redis', 'rediss'} or not parts.hostname
            or parts.path not in {'', '/', '/0'} or parts.query or parts.fragment):
        raise MigrationRefused('A Redis database-zero URL is required.')
    if target and parts.scheme != 'rediss':
        raise MigrationRefused('The managed target must use TLS.')


def remaining_ttl(ttl_ms, age_ms):
    if ttl_ms == -1:
        return 0  # RESTORE uses zero for a persistent key.
    if ttl_ms < 0 or ttl_ms - age_ms <= 0:
        return None
    return max(1, int(ttl_ms - age_ms))


def _canonical(value):
    if isinstance(value, bytes):
        return {'binary': value.hex()}
    if isinstance(value, dict):
        return sorted([[_canonical(key), _canonical(item)] for key, item in value.items()], key=repr)
    if isinstance(value, set):
        return sorted([_canonical(item) for item in value], key=repr)
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def content_digest(client, key):
    kind = client.type(key)
    readers = {b'string': lambda: client.get(key), b'hash': lambda: client.hgetall(key),
        b'list': lambda: client.lrange(key, 0, -1), b'set': lambda: client.smembers(key),
        b'zset': lambda: client.zrange(key, 0, -1, withscores=True),
        b'stream': lambda: client.xrange(key)}
    if kind == b'none':
        return None
    if kind not in readers:
        raise MigrationRefused('An unsupported source key type requires separate migration.')
    payload = json.dumps(_canonical([kind, readers[kind]()]), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).digest()


def preflight(source, target, *, memory_ceiling=250_000_000):
    if not source.ping() or not target.ping():
        raise MigrationRefused('Both Redis services must be reachable.')
    memory = source.info('memory').get('used_memory')
    if type(memory) is not int or memory < 0 or memory > memory_ceiling:
        raise MigrationRefused('Source memory does not fit the target with required headroom.')
    if target.dbsize() != 0:
        raise MigrationRefused('The target database must be empty.')
    return {'source_keys': source.dbsize(), 'source_used_memory': memory, 'target_empty': True}


def copy_database(source, target, *, source_quiesced=False, clock=time.monotonic):
    if not source_quiesced:
        raise MigrationRefused('Hold incoming requests and job admission before copying.')
    preflight(source, target)
    originals = {}
    copied = expired = 0
    for key in source.scan_iter(count=250):
        if key in originals:
            continue
        started = clock()
        ttl, expires_at, payload = source.pipeline(transaction=True).pttl(key).pexpiretime(key).dump(key).execute()
        original = content_digest(source, key)
        restore_ttl = remaining_ttl(ttl, (clock() - started) * 1000)
        if payload is None or original is None or restore_ttl is None:
            expired += 1
            continue
        # Relative RESTORE would start the lifetime after network transit. Preserve
        # the source's exact server expiry timestamp instead (Redis 7+).
        target.restore(key, 0 if expires_at == -1 else expires_at, payload,
                       replace=False, absttl=expires_at != -1)
        if content_digest(target, key) != original:
            if source.pttl(key) == -2 and target.pttl(key) == -2:
                expired += 1
                continue
            raise MigrationRefused('Copied typed content failed verification.')
        if target.pexpiretime(key) != expires_at:
            raise MigrationRefused('Copy changed the source expiry timestamp.')
        originals[key] = (original, expires_at)
        copied += 1
    # Detect writes during the maintenance window instead of claiming a safe copy.
    current = set(source.scan_iter(count=250))
    for key in list(current):
        if key not in originals:
            raise MigrationRefused('Source changed during copying; keep the old cache active.')
        source_digest = content_digest(source, key)
        if source_digest is None:
            seconds, microseconds = source.time()
            expires_at = originals[key][1]
            if expires_at > 0 and seconds * 1000 + microseconds // 1000 >= expires_at:
                current.remove(key)
                target.delete(key)
                continue
        if source_digest != originals[key][0]:
            raise MigrationRefused('Source changed during copying; keep the old cache active.')
        if source.pexpiretime(key) != originals[key][1]:
            raise MigrationRefused('Source expiry changed during copying.')
        if content_digest(target, key) != originals[key][0]:
            raise MigrationRefused('Target no longer matches the quiescent source.')
        if target.pexpiretime(key) != originals[key][1]:
            raise MigrationRefused('Target expiry changed during copying.')
    seconds, microseconds = source.time()
    now_ms = seconds * 1000 + microseconds // 1000
    for key in originals.keys() - current:
        expires_at = originals[key][1]
        if expires_at == -1 or now_ms < expires_at:
            raise MigrationRefused('Source key was deleted before its recorded expiry.')
        target.delete(key)  # Remove any expired remnant even if server clocks differ.
    if set(target.scan_iter(count=250)) != current:
        raise MigrationRefused('Target keyspace changed during copying.')
    return {'copied_keys': copied, 'expired_keys_skipped': expired, 'verified_live_keys': len(current)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--copy', action='store_true')
    parser.add_argument('--source-quiesced', action='store_true')
    args = parser.parse_args()
    try:
        import redis
        source_url, target_url = os.environ.get('ACP_REDIS_SOURCE_URL', ''), os.environ.get('ACP_REDIS_TARGET_URL', '')
        validate_url(source_url)
        validate_url(target_url, target=True)
        options = dict(decode_responses=False, socket_connect_timeout=5, socket_timeout=5)
        source = redis.Redis.from_url(source_url, **options,
            **({'ssl_check_hostname': True} if urlsplit(source_url).scheme == 'rediss' else {}))
        target = redis.Redis.from_url(target_url, ssl_check_hostname=True, **options)
        result = copy_database(source, target, source_quiesced=args.source_quiesced) if args.copy else preflight(source, target)
        print(json.dumps(result, sort_keys=True))
        return 0
    except MigrationRefused as error:
        print(str(error))
    except Exception as error:
        # Redis exceptions may contain endpoints; never print their raw messages.
        print('Redis migration failed (' + type(error).__name__ + '); keep the old cache active.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
