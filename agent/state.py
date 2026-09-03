"""Per-conversation state for the chatbot service.

The original single-file implementation used three bare module-level dicts.
This module centralises them behind a small ``SessionStore`` so the storage
strategy can later be swapped (e.g. for Redis) without touching call sites.

Caveat: state is process-local, exactly like the original implementation.
Run uvicorn with a single worker (the shipped manage.sh does) or replace the
store backend before scaling out horizontally.
"""
import threading
from typing import Any


class SessionStore:
    """Thread-safe access to in-memory conversation history and context.

    ``history``/``context`` return the *live* underlying structures so callers
    can keep using familiar dict/list semantics; compound operations (append,
    create-if-missing, mark-imported) are guarded by a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, list] = {}
        self._context: dict[str, dict] = {}
        self._seen_imports: dict[str, dict] = {}

    def history(self, cid: str) -> list:
        """Return (creating if needed) the OpenAI-style message list."""
        with self._lock:
            return self._sessions.setdefault(cid, [])

    def context(self, cid: str) -> dict:
        """Return (creating if needed) the per-conversation tool context."""
        with self._lock:
            return self._context.setdefault(cid, {})

    def append(self, cid: str, message: dict) -> None:
        """Append one message dict to the conversation history."""
        with self._lock:
            self._sessions.setdefault(cid, []).append(message)

    def last_message(self, cid: str) -> dict | None:
        """Return the most recent message dict, or None for an empty history."""
        with self._lock:
            history = self._sessions.get(cid)
            return history[-1] if history else None

    def seen_import(self, key: str) -> dict | None:
        """Return the cached import result for ``key``, if any."""
        with self._lock:
            return self._seen_imports.get(key)

    def mark_imported(self, key: str, result: dict) -> None:
        """Remember the import result so retries stay idempotent."""
        with self._lock:
            self._seen_imports[key] = result


store: SessionStore = SessionStore()
