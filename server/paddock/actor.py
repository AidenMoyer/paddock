"""Who is making a request, and what they may do."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Actor:
    user_id: int
    username: str
    is_admin: bool
    kind: str                      # web | device | token | ai | system
    scopes: set[str] = field(default_factory=set)   # read | write | admin
    device_id: int | None = None
    token_id: int | None = None
    label: str = ""
    remote: bool = False           # arrived through the WireGuard tunnel

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def audit_name(self) -> str:
        return self.label or f"{self.kind}:{self.username}"


def for_user(user: dict, kind: str = "web", device_id: int | None = None) -> Actor:
    scopes = {"read", "write"} | ({"admin"} if user["is_admin"] else set())
    return Actor(user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), kind=kind,
                 scopes=scopes, device_id=device_id, label=f"{kind}:{user['username']}")


SYSTEM = Actor(user_id=0, username="system", is_admin=True, kind="system", scopes={"read", "write", "admin"},
               label="system")
