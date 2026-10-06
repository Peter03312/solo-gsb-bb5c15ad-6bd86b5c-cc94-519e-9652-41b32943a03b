from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


client = TestClient(app)


def iso(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _clear():
    store._halls.clear()
    yield
    store._halls.clear()


def lamp(lid, default=0):
    return {"id": lid, "default_level": default}


def scene(sid, start, end, items, priority="normal", **kw):
    return {"id": sid, "priority": priority, "start_at": start, "end_at": end, "items": items, **kw}


def item(lid, level, fade=0, restore_fade=None):
    d = {"lamp_id": lid, "level": level, "fade_seconds": fade}
    if restore_fade is not None:
        d["restore_fade_seconds"] = restore_fade
    return d


VALID_HALL = {
    "id": "hall-1",
    "name": "Bronze Gallery",
    "lamps": [lamp("L1", default=0), lamp("L2", default=10)],
    "scenes": [
        scene("morning", iso(2026, 10, 5, 8), iso(2026, 10, 5, 12),
              [item("L1", 60, fade=300), item("L2", 40, fade=600)]),
        scene("noon", iso(2026, 10, 5, 12), iso(2026, 10, 5, 18),
              [item("L1", 90), item("L2", 90)]),
    ],
}


# --------------------------------------------------------------------------- #
def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_hall_compiles_and_persists():
    r = client.put("/halls/hall-1", json=VALID_HALL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["lamps"] == 2
    assert body["summary"]["normal_scenes"] == 2
    assert len(body["timeline"]["L1"]) == 2
    # first command starts from the lamp default
    assert body["timeline"]["L1"][0]["from_level"] == 0
    assert body["timeline"]["L1"][0]["to_level"] == 60
    assert body["timeline"]["L1"][0]["fade_seconds"] == 300
    # noon command on L2 starts from morning's 40
    l2_noon = body["timeline"]["L2"][1]
    assert l2_noon["from_level"] == 40 and l2_noon["to_level"] == 90

    assert client.get("/halls/hall-1").status_code == 200
    assert client.get("/halls").json()["count"] == 1


def test_touching_fade_intervals_are_allowed():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
            scene("b", iso(2026, 10, 5, 8, 10), iso(2026, 10, 5, 20), [item("L1", 70, fade=0)]),
        ],
    }
    r = client.post("/validate", json=hall)
    assert r.status_code == 200, r.text


def test_overlapping_fade_intervals_rejected_with_details():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 50, fade=600)]),
            scene("b", iso(2026, 10, 5, 8, 9, 59), iso(2026, 10, 5, 20), [item("L1", 70, fade=10)]),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == "fade_interval_overlap"
    assert err["channel"] == "L1"
    assert err["between_scenes"] == ["a", "b"]
    assert err["interval"][0] == iso(2026, 10, 5, 8, 9, 59)
    assert "overlap" in err["message"]
    # not persisted
    assert client.get("/halls/h").status_code == 404


def test_unknown_lamp_rejected():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("GHOST", 50)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    codes = {e["code"] for e in r.json()["errors"]}
    assert "unknown_lamp" in codes
    assert client.get("/halls/h").status_code == 404


def test_level_out_of_range_rejected():
    # 101 fails pydantic shape validation, 150 via JSON accepted by client but rejected
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 8), iso(2026, 10, 5, 20), [item("L1", 150)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422


def test_invalid_time_window_rejected():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [scene("a", iso(2026, 10, 5, 9), iso(2026, 10, 5, 8), [item("L1", 50)])],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    assert any(e["code"] == "invalid_time_window" for e in r.json()["errors"])


def test_emergency_scenes_may_not_overlap():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("e1", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 5)], "emergency"),
            scene("e2", iso(2026, 10, 5, 11, 15), iso(2026, 10, 5, 11, 45), [item("L1", 5)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 422
    assert any(e["code"] == "emergency_overlap" for e in r.json()["errors"])


# --------------------------------------------------------------------------- #
def test_emergency_preempts_and_restores_still_valid_scene():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80, fade=600)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30),
                  [item("L1", 10, fade=0, restore_fade=120)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 200, r.text
    cmds = r.json()["timeline"]["L1"]
    types = [(c["type"], c["scene_id"]) for c in cmds]
    assert ("fade", "A") in types
    assert ("emergency_enter", "E") in types
    assert ("emergency_exit", "E") in types
    enter = next(c for c in cmds if c["type"] == "emergency_enter")
    exit_ = next(c for c in cmds if c["type"] == "emergency_exit")
    assert enter["from_level"] == 80 and enter["to_level"] == 10
    # A started before E and is valid until 12:00 -> restored to 80
    assert exit_["restored_scene_id"] == "A"
    assert exit_["to_level"] == 80
    assert exit_["fade_seconds"] == 120

    pre = r.json()["preemptions"][0]
    assert pre["emergency_scene_id"] == "E" and pre["restored_scene_id"] == "A"

    # State during emergency = emergency level; after release = restored level
    s_during = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 10)}).json()
    assert s_during["channels"]["L1"]["level"] == 10
    assert s_during["active_emergency_scene_ids"] == ["E"]
    s_after = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 35)}).json()
    assert s_after["channels"]["L1"]["level"] == 80
    assert s_after["active_emergency_scene_ids"] == []


def test_scene_due_during_emergency_is_skipped_and_not_resumed():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80)]),
            scene("B", iso(2026, 10, 5, 11, 15), iso(2026, 10, 5, 11, 45), [item("L1", 40)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    assert r.status_code == 200, r.text
    body = r.json()
    skipped_ids = [s["scene_id"] for s in body["skipped_activations"]]
    assert skipped_ids == ["B"]
    # B has no executable command on the channel
    assert all(c["scene_id"] != "B" for c in body["timeline"]["L1"])
    # restore goes back to A, not B
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    assert exit_["restored_scene_id"] == "A"


def test_no_restore_when_no_prior_scene_remains_valid():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 11, 15), [item("L1", 80)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
        ],
    }
    r = client.put("/halls/h", json=hall)
    body = r.json()
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    # A ended (11:15) before E released (11:30) -> cannot restore
    assert exit_["restored_scene_id"] is None
    assert exit_["to_level"] == 10
    assert "note" in exit_


def test_emergency_does_not_touch_unrelated_channels():
    hall = {
        "id": "h", "lamps": [lamp("L1"), lamp("L2")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12),
                  [item("L1", 80), item("L2", 70)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 5)], "emergency"),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    assert all(c["priority"] == "normal" for c in body["timeline"]["L2"])
    state = client.get("/halls/h/state", params={"at": iso(2026, 10, 5, 11, 10)}).json()
    assert state["channels"]["L1"]["level"] == 5
    assert state["channels"]["L2"]["level"] == 70


def test_mid_fade_preemption_restores_interpolated_level():
    # A fades 0 -> 80 over 2 hours; E cuts in exactly 1 hour in -> held level 40
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 14), [item("L1", 80, fade=7200)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30),
                  [item("L1", 10, fade=0)], "emergency"),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    exit_ = next(c for c in body["timeline"]["L1"] if c["type"] == "emergency_exit")
    assert exit_["restored_scene_id"] == "A"
    assert exit_["to_level"] == 40


def test_scene_starting_exactly_at_release_runs_after_exit():
    hall = {
        "id": "h", "lamps": [lamp("L1")],
        "scenes": [
            scene("A", iso(2026, 10, 5, 10), iso(2026, 10, 5, 12), [item("L1", 80)]),
            scene("E", iso(2026, 10, 5, 11), iso(2026, 10, 5, 11, 30), [item("L1", 10)], "emergency"),
            scene("C", iso(2026, 10, 5, 11, 30), iso(2026, 10, 5, 12), [item("L1", 50)]),
        ],
    }
    body = client.put("/halls/h", json=hall).json()
    cmds = body["timeline"]["L1"]
    times = [(c["at"], c["type"], c["scene_id"]) for c in cmds]
    exit_idx = next(i for i, c in enumerate(times) if c[1] == "emergency_exit")
    c_idx = next(i for i, c in enumerate(times) if c[2] == "C")
    assert exit_idx < c_idx
    c_cmd = cmds[c_idx]
    assert c_cmd["from_level"] == 10 and c_cmd["to_level"] == 50
    # C is not marked skipped (release instant is outside the half-open window)
    assert all(s["scene_id"] != "C" for s in body["skipped_activations"])


def test_delete_and_404():
    client.put("/halls/hall-1", json=VALID_HALL)
    assert client.delete("/halls/hall-1").status_code == 204
    assert client.get("/halls/hall-1").status_code == 404
    assert client.delete("/halls/hall-1").status_code == 404


def test_id_mismatch_is_400():
    r = client.put("/halls/other", json=VALID_HALL)
    assert r.status_code == 400
