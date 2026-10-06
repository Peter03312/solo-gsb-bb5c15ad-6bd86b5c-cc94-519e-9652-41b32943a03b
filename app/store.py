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


def _version_conflict_message(expected: int | None, current: int | None) -> str:
    if current is None:
        return f"Hall does not exist yet; expected_version must be null, got {expected}."
    if expected is None:
        return (
            f"Hall already exists at version {current}; "
            "expected_version was null (reserved for halls not yet saved)."
        )
    return f"Expected version {expected} but current version is {current}."


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

    def publish_batch(
        self, items: list[tuple[str, int | None, HallIn]]
    ) -> tuple[list[StoredHall] | None, list[dict] | None]:
        """Validate, version-check and atomically persist a batch of halls.

        ``items`` are ``(hall_id, expected_version, spec)`` triples. Exactly one
        of the return values is set: the stored halls (in batch order) on
        success, or the per-hall failure dicts — in which case nothing was
        written and every version is left unchanged.
        """
        plans: dict[str, dict] = {}
        failures: list[tuple[int, dict]] = []

        # Phase 1 — pure validation/compilation; no shared state is touched.
        for idx, (hall_id, _expected, spec) in enumerate(items):
            if hall_id != spec.id:
                failures.append((idx, {
                    "hall_id": hall_id,
                    "code": "id_mismatch",
                    "message": f"Item hall id '{hall_id}' does not match orchestration id '{spec.id}'.",
                }))
                continue
            plan = compile_hall(spec)
            if plan["ok"]:
                plans[hall_id] = plan
            else:
                failures.append((idx, {
                    "hall_id": hall_id,
                    "code": "invalid_orchestration",
                    "message": "Orchestration rejected by validation.",
                    "errors": plan["errors"],
                    "warnings": plan["warnings"],
                }))

        # Phase 2 — compare-and-set under the lock. The expected-version check
        # and the write happen in the same critical section, so two concurrent
        # batches carrying the same expected version for a hall cannot both
        # win: the loser observes the version the winner just bumped.
        with self._lock:
            for idx, (hall_id, expected, _spec) in enumerate(items):
                if hall_id not in plans:
                    continue  # already reported in phase 1
                current = self._halls.get(hall_id)
                current_version = current.version if current is not None else None
                if expected != current_version:
                    failures.append((idx, {
                        "hall_id": hall_id,
                        "code": "version_conflict",
                        "message": _version_conflict_message(expected, current_version),
                        "expected_version": expected,
                        "current_version": current_version,
                    }))
            if failures:
                return None, [f for _, f in sorted(failures, key=lambda p: p[0])]

            now = datetime.now(timezone.utc)
            stored_batch: list[StoredHall] = []
            for hall_id, _expected, spec in items:
                prev = self._halls.get(hall_id)
                stored = StoredHall(
                    spec=spec,
                    plan=plans[hall_id],
                    created_at=prev.created_at if prev else now,
                    updated_at=now,
                    version=(prev.version + 1) if prev else 1,
                )
                self._halls[hall_id] = stored
                stored_batch.append(stored)
            return stored_batch, None

    def list(self) -> list[StoredHall]:
        with self._lock:
            return list(self._halls.values())

    def delete(self, hall_id: str) -> bool:
        with self._lock:
            return self._halls.pop(hall_id, None) is not None


store = HallStore()