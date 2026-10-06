"""Thread-safe in-memory store of validated hall orchestrations."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import HallIn
from .engine import compile_hall


@dataclass
class StoredHall:
    spec: HallIn
    plan: dict
    created_at: datetime
    updated_at: datetime
    version: int


class HallStore:
    def __init__(self) -> None:
        self._halls: dict[str, StoredHall] = {}
        self._lock = threading.RLock()

    @staticmethod
    def compile(spec: HallIn) -> dict:
        return compile_hall(spec)

    def put(self, spec: HallIn) -> tuple[StoredHall | None, dict]:
        """Validate and persist. Returns ``(stored, error_body)`` — exactly one is set."""
        plan = compile_hall(spec)
        if not plan["ok"]:
            return None, plan
        now = datetime.now(timezone.utc)
        with self._lock:
            prev = self._halls.get(spec.id)
            stored = StoredHall(
                spec=spec,
                plan=plan,
                created_at=prev.created_at if prev else now,
                updated_at=now,
                version=(prev.version + 1) if prev else 1,
            )
            self._halls[spec.id] = stored
        return stored, None

    def get(self, hall_id: str) -> StoredHall | None:
        with self._lock:
            return self._halls.get(hall_id)

    def list(self) -> list[StoredHall]:
        with self._lock:
            return list(self._halls.values())

    def delete(self, hall_id: str) -> bool:
        with self._lock:
            return self._halls.pop(hall_id, None) is not None


store = HallStore()
