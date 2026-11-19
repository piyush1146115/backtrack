"""Session lookup with an in-memory cache in front of a slow backend."""

import time


class Backend:
    """Stands in for the session database. Counts loads so tests can see cache misses."""

    def __init__(self):
        self._rows = {}
        self.loads = 0

    def save(self, session_id, user):
        self._rows[session_id] = {"id": session_id, "user": user}

    def revoke(self, session_id):
        self._rows.pop(session_id, None)

    def load(self, session_id):
        self.loads += 1
        row = self._rows.get(session_id)
        return dict(row) if row else None


class SessionStore:
    def __init__(self, backend, ttl=300, clock=time.monotonic):
        self.backend = backend
        self.ttl = ttl  # seconds
        self.clock = clock
        self._cache = {}  # session_id -> (session, expires_at)
        self._hits = 0
        self._misses = 0

    def get(self, session_id):
        entry = self._cache.get(session_id)
        if entry is not None:
            session, expires_at = entry
            if self.clock() < expires_at:
                self._hits += 1
                return session
            del self._cache[session_id]
        self._misses += 1
        session = self.backend.load(session_id)
        if session is not None:
            self._cache[session_id] = (session, self.clock() + self.ttl * 1000)
        return session

    def get_many(self, session_ids):
        return {sid: self.get(sid) for sid in dict.fromkeys(session_ids)}

    def stats(self):
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}
