from sessions import Backend, SessionStore


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_store(ttl=300):
    backend = Backend()
    backend.save("a", "alice")
    backend.save("b", "bob")
    clock = FakeClock()
    return SessionStore(backend, ttl=ttl, clock=clock), backend, clock


def test_get_returns_session():
    store, _, _ = make_store()
    assert store.get("a") == {"id": "a", "user": "alice"}


def test_unknown_session_returns_none():
    store, _, _ = make_store()
    assert store.get("zzz") is None


def test_repeated_get_loads_from_backend_once():
    store, backend, _ = make_store()
    store.get("a")
    store.get("a")
    assert backend.loads == 1


def test_get_many_loads_each_session_once():
    store, backend, _ = make_store()
    store.get("a")
    result = store.get_many(["a", "b", "a"])
    assert set(result) == {"a", "b"}
    assert backend.loads == 2


def test_stats_count_hits_and_misses():
    store, _, _ = make_store()
    store.get("a")
    store.get("a")
    store.get("b")
    assert store.stats() == {"hits": 1, "misses": 2, "size": 2}


def test_revoked_session_expires_after_ttl():
    store, backend, clock = make_store(ttl=300)
    store.get("a")
    backend.revoke("a")
    clock.now += 301
    assert store.get("a") is None
