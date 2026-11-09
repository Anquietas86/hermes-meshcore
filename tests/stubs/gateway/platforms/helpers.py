import time


class MessageDeduplicator:
    def __init__(self, max_size=2000, ttl_seconds=300):
        self._seen = {}
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id):
        now = time.time()
        if msg_id in self._seen and now - self._seen[msg_id] < self._ttl:
            return True
        self._seen[msg_id] = now
        return False
