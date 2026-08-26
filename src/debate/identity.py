"""Human seats. Token is the seat; name is a label; IP is never identity."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from debate.errors import FloorError


@dataclass
class HumanSession:
    session_id: str
    token: str
    name: str
    ip: str
    slot: int
    watcher: bool = False
    connected: bool = True
    host: bool = False

    def public(self) -> dict:
        note = "you · host" if self.host else ("watching" if self.watcher else "seated")
        return {
            "session_id": self.session_id,
            "name": self.name,
            "slot": self.slot,
            "watcher": self.watcher,
            "connected": self.connected,
            "host": self.host,
            "note": note,
        }


@dataclass
class IdentityBook:
    _by_id: dict[str, HumanSession] = field(default_factory=dict)
    _by_token: dict[str, str] = field(default_factory=dict)
    _next_slot: int = 1

    def humans(self) -> list[HumanSession]:
        return list(self._by_id.values())

    def get_token(self, token: str) -> HumanSession | None:
        sid = self._by_token.get(token)
        return self._by_id.get(sid) if sid else None

    def get(self, session_id: str) -> HumanSession | None:
        return self._by_id.get(session_id)

    def join(
        self,
        name: str,
        ip: str,
        *,
        token: str | None = None,
        watcher: bool = False,
    ) -> HumanSession:
        cleaned = " ".join(name.split())
        if not cleaned:
            raise FloorError("invalid", "name must be 1–60 characters")
        if len(cleaned) > 60:
            raise FloorError("invalid", "name must be 1–60 characters")
        ip = (ip or "").strip()

        if token:
            held = self.get_token(token)
            if held:
                held.name = cleaned
                held.ip = ip
                held.watcher = watcher
                held.connected = True
                return held

        session = HumanSession(
            session_id=secrets.token_hex(8),
            token=secrets.token_urlsafe(24),
            name=cleaned,
            ip=ip,
            slot=self._next_slot,
            watcher=watcher,
            host=self._next_slot == 1,
        )
        self._next_slot += 1
        self._by_id[session.session_id] = session
        self._by_token[session.token] = session.session_id
        return session

    def clear(self, session_id: str) -> None:
        held = self._by_id.pop(session_id, None)
        if not held:
            return
        self._by_token.pop(held.token, None)
