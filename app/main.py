"""HTTP API for scheduled museum lighting scene orchestration."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from .models import HallIn, as_utc
from .simulator import simulate
from .store import StoredHall, store

app = FastAPI(
    title="Museum Lighting Orchestration API",
    version="1.0.0",
    description=(
        "Compile scheduled light scenes into an executable per-channel timeline. "
        "Validates lamp references, brightness ranges and non-overlapping fade intervals; "
        "emergency scenes preempt normal scenes and release only to scenes still in window."
    ),
)


def _stored_or_404(hall_id: str) -> StoredHall:
    stored = store.get(hall_id)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return stored


def _plan_response(stored: StoredHall) -> dict[str, Any]:
    return {
        "hall_id": stored.spec.id,
        "name": stored.spec.name,
        "version": stored.version,
        "created_at": stored.created_at.isoformat(),
        "updated_at": stored.updated_at.isoformat(),
        **{k: stored.plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")},
    }


def _reject_invalid(plan: dict, hall_id: str) -> JSONResponse:
    # 422 with concrete channel / interval / reason; nothing was written.
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "hall_id": hall_id,
            "message": "Orchestration rejected; no timeline was saved.",
            "errors": plan["errors"],
            "warnings": plan["warnings"],
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "halls": len(store.list())}


@app.put(
    "/halls/{hall_id}",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate and persist a hall orchestration (full replace)",
)
def put_hall(hall_id: str, hall: HallIn) -> Any:
    if hall.id != hall_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "id_mismatch",
                "message": f"Path hall id '{hall_id}' does not match body hall id '{hall.id}'.",
            },
        )
    stored, plan = store.put(hall)
    if stored is None:
        return _reject_invalid(plan, hall_id)
    return _plan_response(stored)


@app.post(
    "/validate",
    status_code=status.HTTP_200_OK,
    tags=["halls"],
    summary="Validate/compile without persisting (dry run)",
)
def validate(hall: HallIn) -> Any:
    plan = store.compile(hall)
    if not plan["ok"]:
        return _reject_invalid(plan, hall.id)
    return {"ok": True, "hall_id": hall.id, **{k: plan[k] for k in ("summary", "warnings", "timeline", "preemptions", "skipped_activations")}}


@app.get("/halls", tags=["halls"], summary="List persisted halls")
def list_halls() -> dict:
    items = [
        {
            "hall_id": s.spec.id,
            "name": s.spec.name,
            "version": s.version,
            "updated_at": s.updated_at.isoformat(),
            **s.plan["summary"],
        }
        for s in store.list()
    ]
    return {"halls": items, "count": len(items)}


@app.get("/halls/{hall_id}", tags=["halls"], summary="Get the compiled plan/timeline")
def get_hall(hall_id: str) -> dict:
    return _plan_response(_stored_or_404(hall_id))


@app.get(
    "/halls/{hall_id}/timeline",
    tags=["halls"],
    summary="Get just the executable per-channel command timeline",
)
def get_timeline(hall_id: str) -> dict:
    stored = _stored_or_404(hall_id)
    return {"hall_id": hall_id, "timeline": stored.plan["timeline"], "summary": stored.plan["summary"]}


@app.get(
    "/halls/{hall_id}/state",
    tags=["halls"],
    summary="Simulate effective channel levels at a given time",
)
def get_state(
    hall_id: str,
    at: datetime | None = Query(default=None, description="ISO 8601 time; defaults to now (UTC)."),
) -> dict:
    stored = _stored_or_404(hall_id)
    t = as_utc(at) if at is not None else datetime.now(timezone.utc)
    return simulate(stored.plan, t)


@app.delete(
    "/halls/{hall_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["halls"],
    summary="Delete a hall orchestration",
)
def delete_hall(hall_id: str) -> Response:
    if not store.delete(hall_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "hall_not_found", "message": f"Hall '{hall_id}' does not exist."},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
