from datetime import datetime, timezone
import sqlite3

from fastapi.testclient import TestClient

from backend.app.main import app, DB_PATH, init_db

client = TestClient(app)
COORDINATOR_HEADERS = {"X-Dev-Role": "COORDINATOR"}
CITIZEN_HEADERS = {"X-Dev-Role": "CITIZEN"}


def setup_function():
    # The workspace may be mounted from a host filesystem that prevents unlinking
    # a database file while another process has it open. Reset rows in place.
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.execute("DELETE FROM audit_events")
        db.execute("DELETE FROM assignments")
        db.execute("DELETE FROM incidents")
        db.execute("DELETE FROM location_samples")


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_incident_triage_and_transition():
    created = client.post(
        "/api/v1/incidents",
        json={"category": "Fire", "description": "Person trapped in occupied building", "people_affected": 2},
    ).json()
    assert created["priority"] == "CRITICAL"
    incident_id = created["id"]
    assert client.get(f"/api/v1/incidents/{incident_id}").status_code == 200

    updated = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "VERIFIED", "coordinator_note": "Verified by command"},
        headers=COORDINATOR_HEADERS,
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "VERIFIED"

    invalid = client.patch(
        f"/api/v1/incidents/{incident_id}",
        json={"status": "CLOSED"},
        headers=COORDINATOR_HEADERS,
    )
    assert invalid.status_code == 409
    assert "VERIFIED to CLOSED" in invalid.json()["detail"]
    assert "DISPATCHED" in invalid.json()["detail"]


def test_dispatch():
    incident = client.post("/api/v1/incidents", json={"category": "Flood", "description": "Road blocked"}).json()
    response = client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue"},
        headers=COORDINATOR_HEADERS,
    )
    assert response.status_code == 201
    assert client.get("/api/v1/assignments", headers=COORDINATOR_HEADERS).json()["count"] == 1


def test_citizen_cannot_update_assign_or_read_locations():
    incident = client.post("/api/v1/incidents", json={"category": "Medical", "description": "Injured person"}).json()
    location = client.post(
        "/api/v1/locations",
        headers=CITIZEN_HEADERS,
        json={
            "incident_id": incident["id"],
            "latitude": 40.7128,
            "longitude": -74.0060,
            "accuracy_m": 8.5,
            "captured_at": "2026-01-01T12:00:00Z",
        },
    )
    assert location.status_code == 201

    assert client.patch(
        f"/api/v1/incidents/{incident['id']}",
        json={"status": "VERIFIED"},
        headers=CITIZEN_HEADERS,
    ).status_code == 403
    assert client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue"},
        headers=CITIZEN_HEADERS,
    ).status_code == 403
    assert client.get("/api/v1/assignments", headers=CITIZEN_HEADERS).status_code == 403
    assert client.get(
        f"/api/v1/incidents/{incident['id']}/locations",
        headers=CITIZEN_HEADERS,
    ).status_code == 403


def test_location_persists_and_reads_without_websocket():
    incident = client.post("/api/v1/incidents", json={"category": "Flood", "description": "Water rising"}).json()
    response = client.post(
        "/api/v1/locations",
        headers=CITIZEN_HEADERS,
        json={
            "incident_id": incident["id"],
            "latitude": 0,
            "longitude": 0,
            "accuracy_m": 12,
            "captured_at": "2026-02-03T04:05:06+02:00",
        },
    )
    assert response.status_code == 201
    assert response.json()["latitude"] == 0
    assert response.json()["longitude"] == 0

    read = client.get(f"/api/v1/incidents/{incident['id']}/locations", headers=COORDINATOR_HEADERS)
    assert read.status_code == 200
    assert read.json()["count"] == 1
    assert read.json()["items"][0]["captured_at"] == "2026-02-03T02:05:06+00:00"


def test_device_location_and_location_validation():
    device_location = client.post(
        "/api/v1/locations",
        headers=CITIZEN_HEADERS,
        json={
            "device_id": "phone-17",
            "latitude": 51.5074,
            "longitude": -0.1278,
            "captured_at": "2026-03-04T10:00:00Z",
        },
    )
    assert device_location.status_code == 201
    assert device_location.json()["incident_id"] is None

    missing_reference = client.post(
        "/api/v1/locations",
        json={"latitude": 10, "longitude": 20, "captured_at": "2026-03-04T10:00:00Z"},
    )
    assert missing_reference.status_code == 422
    assert "incident_id or device_id is required" in missing_reference.text

    invalid_coordinates = client.post(
        "/api/v1/locations",
        json={
            "device_id": "phone-17",
            "latitude": 91,
            "longitude": 20,
            "captured_at": "2026-03-04T10:00:00Z",
        },
    )
    assert invalid_coordinates.status_code == 422

    naive_timestamp = client.post(
        "/api/v1/locations",
        json={"device_id": "phone-17", "latitude": 10, "longitude": 20, "captured_at": "2026-03-04T10:00:00"},
    )
    assert naive_timestamp.status_code == 422


def test_location_update_is_broadcast():
    incident = client.post("/api/v1/incidents", json={"category": "Fire", "description": "Smoke reported"}).json()
    with client.websocket_connect("/ws/events") as websocket:
        response = client.post(
            "/api/v1/locations",
            json={
                "incident_id": incident["id"],
                "latitude": 12.34,
                "longitude": 56.78,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 201
        event = websocket.receive_json()
        assert event["type"] == "location.updated"
        assert event["data"]["id"] == response.json()["id"]
