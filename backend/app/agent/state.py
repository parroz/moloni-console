"""In-memory session store for the agent. Lost on uvicorn restart (intentional, v1)."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentSession:
    session_id: str
    supplier_slug: str = "auto"
    test_mode: bool = True  # default safe: nothing is written until user toggles off
    pdf_bytes: bytes | None = None
    pdf_filename: str | None = None
    # Anthropic message format: list of {role, content[]}.
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    # One concurrent run per session (lock prevents user from sending two messages at once).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Trivial dict-backed store. One process, one store."""

    def __init__(self, ttl_seconds: int = 60 * 60 * 6) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._ttl = ttl_seconds

    def create(self) -> AgentSession:
        sid = str(uuid.uuid4())
        s = AgentSession(session_id=sid)
        self._sessions[sid] = s
        return s

    def get(self, session_id: str) -> AgentSession | None:
        s = self._sessions.get(session_id)
        if s is None:
            return None
        s.last_used = time.time()
        return s

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def gc(self) -> int:
        """Drop sessions older than TTL. Returns number removed."""
        cutoff = time.time() - self._ttl
        stale = [sid for sid, s in self._sessions.items() if s.last_used < cutoff]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)


# Singleton store
store = SessionStore()
