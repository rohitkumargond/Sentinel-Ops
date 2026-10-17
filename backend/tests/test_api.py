from datetime import datetime, timezone
import sqlite3

from fastapi.testclient import TestClient

from backend.app.main import app, DB_PATH, init_db

client = TestClient(app)
ADMIN_HEADERS = {"X-Dev-Role": "ADMIN"}
# Keep one explicit compatibility name while the endpoint coverage uses ADMIN.
COORDINATOR_HEADERS = ADMIN_HEADERS
LEGACY_COORDINATOR_HEADERS = {"X-Dev-Role": "COORDINATOR"}
CITIZEN_HEADERS = {"X-Dev-Role": "CITIZEN"}


def citizen_headers(device_id):
    return {**CITIZEN_HEADERS, "X-Device-Id": device_id}


def setup_function():
    # The workspace may be mounted from a host filesystem that prevents unlinking
    # a database file while another process has it open. Reset rows in place.
    init_db()
    with sqlite3.connect(DB_PATH) as db:
        db.execute("DELETE FROM audit_events")
        db.execute("DELETE FROM chat_messages")
        db.execute("DELETE FROM assignment_status_history")
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


def test_admin_is_command_role_and_legacy_coordinator_header_is_accepted():
    admin_login = client.post("/api/v1/auth/demo-login", json={"role": "admin"})
    assert admin_login.status_code == 200
    assert admin_login.json()["role"] == "ADMIN"

    legacy_login = client.post("/api/v1/auth/demo-login", json={"role": "coordinator"})
    assert legacy_login.status_code == 200
    assert legacy_login.json()["role"] == "ADMIN"
    assert legacy_login.json()["user_id"] == "demo-admin-1"

    incident = client.post(
        "/api/v1/incidents",
        json={"category": "Flood", "description": "Legacy header compatibility"},
        headers=ADMIN_HEADERS,
    ).json()
    assert client.patch(
        f"/api/v1/incidents/{incident['id']}",
        json={"status": "VERIFIED"},
        headers=LEGACY_COORDINATOR_HEADERS,
    ).status_code == 200

    location = client.post(
        "/api/v1/locations",
        headers=ADMIN_HEADERS,
        json={
            "incident_id": incident["id"],
            "latitude": 40.7128,
            "longitude": -74.0060,
            "captured_at": "2026-01-01T12:00:00Z",
        },
    )
    assert location.status_code == 201
    assert client.get("/api/v1/locations", headers=ADMIN_HEADERS).json()["count"] == 1


def test_duplicate_dispatch_requires_completed_reassignment():
    incident = client.post("/api/v1/incidents", json={"category": "Flood", "description": "Road blocked"}).json()
    first = client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue"},
        headers=COORDINATOR_HEADERS,
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Bravo Rescue"},
        headers=COORDINATOR_HEADERS,
    )
    assert duplicate.status_code == 409
    assert "active assignment" in duplicate.json()["detail"]

    completed = client.patch(
        f"/api/v1/assignments/{first.json()['id']}",
        json={"status": "COMPLETED"},
        headers=COORDINATOR_HEADERS,
    )
    assert completed.status_code == 200

    without_reassign = client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Bravo Rescue"},
        headers=COORDINATOR_HEADERS,
    )
    assert without_reassign.status_code == 409
    assert "reassign=true" in without_reassign.json()["detail"]

    reassigned = client.post(
        "/api/v1/assignments",
        json={"incident_id": incident["id"], "team_name": "Bravo Rescue", "reassign": True},
        headers=COORDINATOR_HEADERS,
    )
    assert reassigned.status_code == 201
    assert client.get("/api/v1/assignments", headers=COORDINATOR_HEADERS).json()["count"] == 2


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


def test_citizen_cannot_read_another_device_incident():
    incident = client.post(
        "/api/v1/incidents",
        headers=citizen_headers("device-a"),
        json={"category": "Medical", "description": "A private medical report"},
    ).json()
    assert incident["reporter_device_id"] == "device-a"

    assert client.get(
        f"/api/v1/incidents/{incident['id']}",
        headers=citizen_headers("device-b"),
    ).status_code == 403
    assert client.get(
        f"/api/v1/incidents/{incident['id']}",
        headers=citizen_headers("device-a"),
    ).status_code == 200


def test_citizen_can_list_own_incident_statuses():
    own = client.post(
        "/api/v1/incidents",
        headers=citizen_headers("device-a"),
        json={"category": "Medical", "description": "Status tracking report"},
    ).json()
    client.post(
        "/api/v1/incidents",
        headers=citizen_headers("device-b"),
        json={"category": "Medical", "description": "Another device report"},
    )
    client.patch(
        f"/api/v1/incidents/{own['id']}",
        headers=COORDINATOR_HEADERS,
        json={"status": "VERIFIED"},
    )

    response = client.get("/api/v1/my/incidents", headers=citizen_headers("device-a"))
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["id"] == own["id"]
    assert response.json()["items"][0]["status"] == "VERIFIED"


def test_regional_incidents_are_visible_to_citizens():
    citizen_public = client.post(
        "/api/v1/incidents",
        headers=citizen_headers("device-a"),
        json={
            "category": "Flood",
            "description": "Citizen report cannot publish itself",
            "regional_visibility": True,
        },
    )
    assert citizen_public.status_code == 403

    regional = client.post(
        "/api/v1/incidents",
        headers=COORDINATOR_HEADERS,
        json={
            "category": "Flood",
            "description": "Regional river flooding",
            "regional_visibility": True,
        },
    ).json()
    private = client.post(
        "/api/v1/incidents",
        headers=COORDINATOR_HEADERS,
        json={
            "category": "Medical",
            "description": "Private report",
            "reporter_device_id": "device-b",
        },
    ).json()

    listed = client.get("/api/v1/incidents", headers=citizen_headers("device-a"))
    assert listed.status_code == 200
    listed_ids = {item["id"] for item in listed.json()["items"]}
    assert regional["id"] in listed_ids
    assert private["id"] not in listed_ids
    assert client.get(
        f"/api/v1/incidents/{regional['id']}",
        headers=citizen_headers("device-a"),
    ).status_code == 200


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


def test_admin_can_clear_server_demo_data_without_citizen_access():
    incident = client.post(
        "/api/v1/incidents",
        headers=ADMIN_HEADERS,
        json={"category": "Flood", "description": "Reset flow test", "latitude": 40.7, "longitude": -74.0},
    ).json()
    assignment = client.post(
        "/api/v1/assignments",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue"},
    )
    assert assignment.status_code == 201
    location = client.post(
        "/api/v1/locations",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "latitude": 40.7, "longitude": -74.0, "captured_at": "2026-03-04T10:00:00Z"},
    )
    assert location.status_code == 201
    message = client.post(
        f"/api/v1/incidents/{incident['id']}/chat",
        headers=ADMIN_HEADERS,
        json={"body": "Reset flow message"},
    )
    assert message.status_code == 201

    assert client.post("/api/v1/admin/demo-data/clear", headers=CITIZEN_HEADERS).status_code == 403
    cleared = client.post("/api/v1/admin/demo-data/clear", headers=ADMIN_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["scope"] == "SERVER_DEMO_DATA_ONLY"
    assert cleared.json()["cleared"]["incidents"] == 1
    assert cleared.json()["cleared"]["chat_messages"] == 1
    assert cleared.json()["cleared"]["assignment_status_history"] == 1
    assert client.get("/api/v1/incidents", headers=ADMIN_HEADERS).json()["count"] == 0
    assert client.get("/api/v1/assignments", headers=ADMIN_HEADERS).json()["count"] == 0
    assert client.get("/api/v1/locations", headers=ADMIN_HEADERS).json()["count"] == 0


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


def test_local_demo_login_and_field_officer_briefing_flow():
    login = client.post("/api/v1/auth/demo-login", json={"role": "field officer"})
    assert login.status_code == 200
    assert login.json()["role"] == "FIELD_OFFICER"
    assert login.json()["production_auth"] is False

    incident = client.post(
        "/api/v1/incidents",
        headers=COORDINATOR_HEADERS,
        json={"category": "Flood", "description": "Road blocked at the bridge", "latitude": 40.7128, "longitude": -74.0060},
    ).json()
    assignment = client.post(
        "/api/v1/assignments",
        headers=COORDINATOR_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue", "field_officer_id": "demo-field-officer-1", "notes": "Check the western entrance first."},
    )
    assert assignment.status_code == 201

    field_headers = {"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "demo-field-officer-1"}
    assert client.get("/api/v1/incidents", headers=field_headers).status_code == 403
    briefing = client.get("/api/v1/field-officer/briefing", headers=field_headers)
    assert briefing.status_code == 200
    assert briefing.json()["assignments"][0]["latitude"] == 40.7128
    assert briefing.json()["assignments"][0]["notes"] == "Check the western entrance first."
    assert briefing.json()["safe_locations"]

    status = client.patch(
        f"/api/v1/assignments/{assignment.json()['id']}",
        headers=field_headers,
        json={"status": "EN_ROUTE"},
    )
    assert status.status_code == 200
    other_officer = client.patch(
        f"/api/v1/assignments/{assignment.json()['id']}",
        headers={"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "demo-field-officer-2"},
        json={"status": "ON_SCENE"},
    )
    assert other_officer.status_code == 403

    location = client.post(
        "/api/v1/field-officer/locations",
        headers=field_headers,
        json={"incident_id": incident["id"], "latitude": 40.713, "longitude": -74.005, "accuracy_m": 5, "captured_at": "2026-03-04T10:00:00Z"},
    )
    assert location.status_code == 201
    all_locations = client.get("/api/v1/locations", headers=COORDINATOR_HEADERS)
    assert all_locations.status_code == 200
    assert any(item["device_id"] == "demo-field-officer-1" for item in all_locations.json()["items"])


def test_chat_history_persists_for_admin_and_assigned_field_officer():
    incident = client.post(
        "/api/v1/incidents",
        headers=citizen_headers("chat-owner"),
        json={"category": "Medical", "description": "Chat persistence incident"},
    ).json()
    assignment = client.post(
        "/api/v1/assignments",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue", "field_officer_id": "demo-field-officer-1"},
    ).json()
    admin_message = client.post(
        f"/api/v1/incidents/{incident['id']}/chat",
        headers=ADMIN_HEADERS,
        json={"body": "Proceed to the west entrance", "client_message_id": "admin-message-1"},
    )
    assert admin_message.status_code == 201
    duplicate = client.post(
        f"/api/v1/chat/messages?incident_id={incident['id']}",
        headers=ADMIN_HEADERS,
        json={"body": "Proceed to the west entrance", "client_message_id": "admin-message-1"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == admin_message.json()["id"]

    field_message = client.post(
        f"/api/v1/incidents/{incident['id']}/chat",
        headers={"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "demo-field-officer-1"},
        json={"text": "Received and moving now", "client_message_id": "field-message-1"},
    )
    assert field_message.status_code == 201
    history = client.get(f"/api/v1/incidents/{incident['id']}/chat", headers=ADMIN_HEADERS)
    assert history.status_code == 200
    assert [item["body"] for item in history.json()["items"]] == ["Proceed to the west entrance", "Received and moving now"]
    assert history.json()["items"][0]["assignment_id"] == assignment["id"]
    assert client.get(f"/api/v1/incidents/{incident['id']}/chat", headers=citizen_headers("chat-owner")).status_code == 403


def test_offline_friendly_message_lifecycle_is_idempotent_without_fabricating_delivery():
    incident = client.post("/api/v1/incidents", json={"category": "Fire", "description": "Offline chat lifecycle"}).json()
    client.post(
        "/api/v1/assignments",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue", "field_officer_id": "demo-field-officer-1"},
    )
    response = client.post(
        f"/api/v1/incidents/{incident['id']}/chat",
        headers=ADMIN_HEADERS,
        json={"body": "Queued while offline", "client_message_id": "outbox-1"},
    )
    assert response.status_code == 201
    assert response.json()["sync_state"] == "PERSISTED"
    assert response.json().get("delivery_state") is None
    retry = client.post(
        f"/api/v1/incidents/{incident['id']}/chat",
        headers=ADMIN_HEADERS,
        json={"body": "Queued while offline", "client_message_id": "outbox-1"},
    )
    assert retry.json()["id"] == response.json()["id"]


def test_completed_task_history_is_durable_and_scoped_to_field_officer():
    incident = client.post("/api/v1/incidents", json={"category": "Flood", "description": "Completed task history"}).json()
    assignment = client.post(
        "/api/v1/assignments",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Alpha Rescue", "field_officer_id": "demo-field-officer-1"},
    ).json()
    field_headers = {"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "demo-field-officer-1"}
    assert client.patch(f"/api/v1/assignments/{assignment['id']}", headers=field_headers, json={"status": "EN_ROUTE"}).status_code == 200
    assert client.patch(f"/api/v1/assignments/{assignment['id']}", headers=field_headers, json={"status": "ON_SCENE"}).status_code == 200
    assert client.patch(f"/api/v1/assignments/{assignment['id']}", headers=field_headers, json={"status": "COMPLETED"}).status_code == 200
    history = client.get("/api/v1/field-officer/tasks/history", headers=field_headers)
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == assignment["id"]
    assert history.json()["items"][0]["status"] == "COMPLETED"
    assert client.get("/api/v1/field-officer/tasks/history", headers={"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "other-officer"}).json()["count"] == 0
    admin_history = client.get("/api/v1/assignments/history", headers=ADMIN_HEADERS)
    assert admin_history.status_code == 200
    assert [item["status"] for item in admin_history.json()["items"][:4]] == ["COMPLETED", "ON_SCENE", "EN_ROUTE", "PENDING"]


def test_field_officer_status_is_visible_to_admin_and_incident_owner_without_locations():
    incident = client.post(
        "/api/v1/incidents",
        headers=citizen_headers("status-owner"),
        json={"category": "Medical", "description": "Status visibility incident"},
    ).json()
    assignment = client.post(
        "/api/v1/assignments",
        headers=ADMIN_HEADERS,
        json={"incident_id": incident["id"], "team_name": "Medical Unit 2", "field_officer_id": "demo-field-officer-1"},
    ).json()
    field_headers = {"X-Dev-Role": "FIELD_OFFICER", "X-Demo-User": "demo-field-officer-1"}
    assert client.patch(f"/api/v1/assignments/{assignment['id']}", headers=field_headers, json={"status": "EN_ROUTE"}).status_code == 200
    admin_item = client.get(f"/api/v1/incidents/{incident['id']}", headers=ADMIN_HEADERS).json()
    citizen_item = client.get(f"/api/v1/incidents/{incident['id']}", headers=citizen_headers("status-owner")).json()
    assert admin_item["field_officer_status"] == "EN_ROUTE"
    assert citizen_item["field_officer_status"] == "EN_ROUTE"
    assert citizen_item["field_officer_team"] == "Medical Unit 2"
    assert "field_officer_id" not in citizen_item
    assert "location_samples" not in citizen_item
    assert client.get(f"/api/v1/incidents/{incident['id']}", headers=citizen_headers("other-owner")).status_code == 403
