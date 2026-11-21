"""Bounded, process-local storage for opaque exact-source proof digests."""
from collections import OrderedDict
from threading import Lock


class EvidenceProofCache:
    """LRU metadata only; current sources and payloads must be checked again."""

    def __init__(self, max_entries=32768):
        if type(max_entries) is not int or max_entries < 0:
            raise ValueError('invalid_proof_cache_capacity')
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = Lock()

    def contains(self, key):
        with self._lock:
            if key not in self._entries:
                return False
            self._entries.move_to_end(key)
            return True

    def remember(self, key):
        with self._lock:
            self._entries[key] = None
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self):
        with self._lock:
            self._entries.clear()
