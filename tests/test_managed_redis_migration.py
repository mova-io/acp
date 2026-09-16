import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('redis_cache_migration', ROOT / 'scripts/redis_cache_migration.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_managed_target_avoids_cluster_redirects_and_credential_eviction():
    resources = json.loads((ROOT / 'deploy/public/managed-redis.json').read_text())['resources']
    cache, database = resources
    assert cache['properties']['highAvailability'] == 'Enabled'
    assert database['properties']['clusteringPolicy'] == 'NoCluster'
    assert database['properties']['clientProtocol'] == 'Encrypted'
    assert database['properties']['evictionPolicy'] == 'NoEviction'
    assert database['properties']['persistence'] == {'aofEnabled': False, 'rdbEnabled': False}


@pytest.mark.parametrize('url', ['redis://cache:6379/1', 'https://cache', 'rediss://cache/2', 'rediss://cache/0?db=1', 'redis://cache?db=2'])
def test_unsupported_database_is_refused_without_repeating_url(url):
    with pytest.raises(migration.MigrationRefused, match='database-zero'):
        migration.validate_url(url)


def test_target_requires_tls():
    with pytest.raises(migration.MigrationRefused, match='TLS'):
        migration.validate_url('redis://cache/0', target=True)
    migration.validate_url('rediss://cache:10000/0', target=True)


def test_ttls_are_reduced_not_restarted():
    assert migration.remaining_ttl(1000, 300) == 700
    assert migration.remaining_ttl(200, 300) is None
    assert migration.remaining_ttl(-2, 0) is None
    assert migration.remaining_ttl(-1, 300) == 0


def test_copy_cannot_start_without_holding_admission():
    with pytest.raises(migration.MigrationRefused, match='Hold incoming'):
        migration.copy_database(None, None)


def test_copy_verifies_semantics_instead_of_version_specific_dump_bytes():
    class Cache:
        def __init__(self, value, payload):
            self.value, self.payload = value, payload
        def type(self, key):
            return b'string'
        def get(self, key):
            return self.value
        def dump(self, key):
            return self.payload
    old, new = Cache(b'private session', b'redis7-dump'), Cache(b'private session', b'redis8-dump')
    assert old.dump(b'key') != new.dump(b'key')
    assert migration.content_digest(old, b'key') == migration.content_digest(new, b'key')
    new.value = b'different session'
    assert migration.content_digest(old, b'key') != migration.content_digest(new, b'key')


class FakeClock:
    now_ms = 0

    def __call__(self):
        return self.now_ms / 1000


class FakePipeline:
    def __init__(self, cache):
        self.cache, self.operations = cache, []

    def pttl(self, key):
        self.operations.append(('pttl', key))
        return self

    def pexpiretime(self, key):
        self.operations.append(('pexpiretime', key))
        return self

    def dump(self, key):
        self.operations.append(('dump', key))
        return self

    def execute(self):
        return [getattr(self.cache, operation)(key) for operation, key in self.operations]


class FakeCache:
    """RESP-shaped values and independently advancing target RESTORE processing."""
    def __init__(self, clock, keys=None, restore_delay=0, expiry_shift=0):
        self.clock, self.keys = clock, dict(keys or {})
        self.restore_delay, self.expiry_shift = restore_delay, expiry_shift
        self.after_restore = None
        self.before_scan = None
        self.before_type = None

    def _entry(self, key):
        value = self.keys.get(key)
        if value and value[1] is not None and value[1] <= self.clock.now_ms:
            del self.keys[key]
            return None
        return value

    def ping(self):
        return True

    def info(self, section=None):
        return {'used_memory': 1000, 'redis_version': '7.4.1'}

    def dbsize(self):
        return len(list(self.scan_iter()))

    def scan_iter(self, count=250):
        if self.before_scan:
            self.before_scan(self)
        return iter([key for key in list(self.keys) if self._entry(key)])

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def type(self, key):
        if self.before_type:
            callback, self.before_type = self.before_type, None
            callback(self)
        return b'string' if self._entry(key) else b'none'

    def get(self, key):
        value = self._entry(key)
        return value[0] if value else None

    def dump(self, key):
        return self.get(key)

    def pttl(self, key):
        value = self._entry(key)
        return -2 if value is None else -1 if value[1] is None else value[1] - self.clock.now_ms

    def pexpiretime(self, key):
        value = self._entry(key)
        return -2 if value is None else -1 if value[1] is None else value[1]

    def time(self):
        return self.clock.now_ms // 1000, (self.clock.now_ms % 1000) * 1000

    def delete(self, key):
        return int(self.keys.pop(key, None) is not None)

    def restore(self, key, ttl, payload, replace=False, absttl=False):
        assert replace is False
        self.clock.now_ms += self.restore_delay
        self.keys[key] = (payload, None if ttl == 0 else (ttl if absttl else self.clock.now_ms + ttl) + self.expiry_shift)
        if self.after_restore:
            self.after_restore(self, key)


def test_network_transit_cannot_extend_the_source_expiration():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 7000)})
    target = FakeCache(clock, restore_delay=1000)
    migration.copy_database(source, target, source_quiesced=True, clock=clock)
    assert target.pexpiretime(b'session') == source.pexpiretime(b'session') == 7000


def test_even_small_target_lifetime_extensions_are_refused():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 7000)})
    target = FakeCache(clock, expiry_shift=50)
    with pytest.raises(migration.MigrationRefused):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)


def test_persistent_source_key_deleted_during_copy_is_refused():
    clock = FakeClock()
    source = FakeCache(clock, {b'revoked-session': (b'private', None)})
    target = FakeCache(clock)
    target.after_restore = lambda cache, key: source.keys.pop(key)
    with pytest.raises(migration.MigrationRefused, match='Source'):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)


def test_source_value_mutation_is_refused_after_copy():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', None)})
    target = FakeCache(clock)
    target.after_restore = lambda cache, key: source.keys.update({key: (b'changed', None)})
    with pytest.raises(migration.MigrationRefused, match='Source changed'):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)


def test_expired_source_key_must_not_remain_alive_on_target():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 500)})
    target = FakeCache(clock, restore_delay=600)
    migration.copy_database(source, target, source_quiesced=True, clock=clock)
    assert source.get(b'session') is target.get(b'session') is None


def test_source_expiration_extension_is_a_mutation_even_when_content_matches():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 7000)})
    target = FakeCache(clock)
    target.after_restore = lambda cache, key: source.keys.update({key: (b'private', 12000)})
    with pytest.raises(migration.MigrationRefused, match='Source'):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)


def test_extra_target_key_created_during_copy_is_refused():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', None)})
    target = FakeCache(clock)
    target.after_restore = lambda cache, key: cache.keys.update({b'unexpected': (b'other session', None)})
    with pytest.raises(migration.MigrationRefused):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)


def test_raw_connection_errors_cannot_print_endpoints_or_credentials(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    def fail(*args, **kwargs):
        raise RuntimeError('Connection rediss://:private-password@secret-host:10000/0 failed')
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(Redis=SimpleNamespace(from_url=fail)))
    monkeypatch.setattr(sys, 'argv', ['redis_cache_migration.py'])
    monkeypatch.setenv('ACP_REDIS_SOURCE_URL', 'redis://source:6379/0')
    monkeypatch.setenv('ACP_REDIS_TARGET_URL', 'rediss://:private-password@secret-host:10000/0')
    assert migration.main() == 1
    output = capsys.readouterr().out
    assert 'RuntimeError' in output
    assert 'private-password' not in output
    assert 'secret-host' not in output


def test_natural_expiration_between_final_scan_and_read_is_not_a_write():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 7000)})
    target = FakeCache(clock)
    def arm_expiration(cache, key):
        source.before_scan = lambda old: setattr(old, 'before_type', lambda client: setattr(clock, 'now_ms', 8000))
    target.after_restore = arm_expiration
    result = migration.copy_database(source, target, source_quiesced=True, clock=clock)
    assert source.get(b'session') is target.get(b'session') is None
    assert result['verified_live_keys'] == 0


def test_target_lifetime_mutation_after_initial_restore_check_is_refused():
    clock = FakeClock()
    source = FakeCache(clock, {b'session': (b'private', 7000)})
    target = FakeCache(clock)
    def arm_mutation(cache, key):
        source.before_scan = lambda old: target.keys.update({key: (b'private', 9000)})
    target.after_restore = arm_mutation
    with pytest.raises(migration.MigrationRefused):
        migration.copy_database(source, target, source_quiesced=True, clock=clock)
