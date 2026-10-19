from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4
import json
import sqlite3
from fastapi.staticfiles import StaticFiles

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "sentinel_ops.db"
app = FastAPI(title="Sentinel-Ops API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# The Docker image includes the compiled dashboard. Local development still
# uses Vite at :5173, so the mount is optional and does not affect the API.
WEB_DIST = ROOT / "apps" / "dashboard" / "dist"
if WEB_DIST.exists():
    app.mount("/dashboard", StaticFiles(directory=WEB_DIST, html=True), name="dashboard")


@app.get("/", include_in_schema=False)
def dashboard_entry():
    """Make the single-container deployment usable from its host root URL."""
    if WEB_DIST.exists():
        return RedirectResponse(url="/dashboard/")
    return {"service": "sentinel-ops", "dashboard": "/dashboard/", "status": "api-only"}

class Priority(str, Enum):
    critical = "CRITICAL"; urgent = "URGENT"; normal = "NORMAL"; informational = "INFORMATIONAL"

class UserRole(str, Enum):
    citizen = "CITIZEN"
    admin = "ADMIN"
    field_officer = "FIELD_OFFICER"


DEMO_USERS = {
    "admin": {"role": UserRole.admin, "user_id": "demo-admin-1", "display_name": "Demo Admin"},
    "field_officer": {"role": UserRole.field_officer, "user_id": "demo-field-officer-1", "display_name": "Demo Field Officer"},
    "citizen": {"role": UserRole.citizen, "user_id": "demo-citizen-1", "display_name": "Demo Citizen"},
}

# Older local dashboard builds sent COORDINATOR. Keep that header working while
# making ADMIN the only command role exposed by the current API and demo UI.
LEGACY_ROLE_ALIASES = {"COORDINATOR": UserRole.admin}
LEGACY_DEMO_ROLE_ALIASES = {"coordinator": "admin"}


class DemoLogin(BaseModel):
    role: str

# Development-only role selection. This is deliberately not production authentication.
def development_role(
    x_dev_role: Optional[str] = Header(default=None, alias="X-Dev-Role"),
    x_role: Optional[str] = Header(default=None, alias="X-Role"),
) -> UserRole:
    value = (x_dev_role or x_role or UserRole.admin.value).strip().upper().replace("-", "_").replace(" ", "_")
    legacy_role = LEGACY_ROLE_ALIASES.get(value)
    value = legacy_role.value if legacy_role else value
    try:
        return UserRole(value)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Invalid development role. Use the local demo roles ADMIN, FIELD_OFFICER, or CITIZEN. COORDINATOR is accepted as a legacy ADMIN header.",
        )

def require_admin(role: UserRole = Depends(development_role)) -> UserRole:
    if role is not UserRole.admin:
        raise HTTPException(
            status_code=403,
            detail="Admin role required for this endpoint (development auth only).",
        )
    return role


# Compatibility for local integrations importing the old dependency name.
require_coordinator = require_admin


def demo_user_id(
    x_demo_user: Optional[str] = Header(default=None, alias="X-Demo-User"),
    x_field_officer_id: Optional[str] = Header(default=None, alias="X-Field-Officer-Id"),
) -> Optional[str]:
    return (x_demo_user or x_field_officer_id or "").strip() or None


def require_field_officer(
    role: UserRole = Depends(development_role),
    user_id: Optional[str] = Depends(demo_user_id),
) -> str:
    if role is not UserRole.field_officer:
        raise HTTPException(status_code=403, detail="Field officer role required for this endpoint (development auth only).")
    if not user_id:
        raise HTTPException(status_code=403, detail="X-Demo-User is required for field officer access (development auth only).")
    return user_id


def require_citizen(
    role: UserRole = Depends(development_role),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-Id"),
) -> str:
    if role is not UserRole.citizen:
        raise HTTPException(status_code=403, detail="Citizen role required for this endpoint (development auth only).")
    if not x_device_id or not x_device_id.strip():
        raise HTTPException(status_code=403, detail="X-Device-Id is required for citizen access (development auth only).")
    return x_device_id.strip()

class IncidentCreate(BaseModel):
    category: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=3, max_length=2000)
    reporter_device_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    regional_visibility: bool = False
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    people_affected: int = Field(default=1, ge=1, le=10000)
    urgent_needs: list[str] = Field(default_factory=list)

class IncidentPatch(BaseModel):
    status: Optional[str] = None
    priority: Optional[Priority] = None
    coordinator_note: Optional[str] = Field(default=None, max_length=2000)
    regional_visibility: Optional[bool] = None

class AssignmentCreate(BaseModel):
    incident_id: UUID
    team_name: str = Field(min_length=2, max_length=120)
    field_officer_id: Optional[str] = Field(default=None, min_length=2, max_length=120)
    notes: str = Field(default="", max_length=1000)
    reassign: bool = False


class AssignmentPatch(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str):
        normalized = value.strip().upper()
        if normalized not in {"PENDING", "EN_ROUTE", "ON_SCENE", "COMPLETED", "CANCELLED"}:
            raise ValueError("status must be PENDING, EN_ROUTE, ON_SCENE, COMPLETED, or CANCELLED")
        return normalized


class ChatMessageCreate(BaseModel):
    body: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    client_message_id: Optional[str] = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def message_body(self):
        value = (self.body or self.text or "").strip()
        if not value:
            raise ValueError("body is required")
        self.body = value
        return self

class LocationSampleCreate(BaseModel):
    incident_id: Optional[UUID] = None
    device_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    captured_at: datetime

    @model_validator(mode="after")
    def has_reference(self):
        if self.incident_id is None and not self.device_id:
            raise ValueError("incident_id or device_id is required")
        return self

    @field_validator("latitude", "longitude", "accuracy_m")
    @classmethod
    def finite_number(cls, value):
        if value is not None and not isfinite(value):
            raise ValueError("coordinate and accuracy values must be finite")
        return value

    @field_validator("captured_at")
    @classmethod
    def timezone_aware_timestamp(cls, value: datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone, for example 2026-01-01T12:00:00Z")
        return value

def now(): return datetime.now(timezone.utc).isoformat()
def connect():
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row; return db


def ensure_column(db, table: str, column: str, definition: str):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, category TEXT, description TEXT, priority TEXT, status TEXT, delivery_state TEXT, latitude REAL, longitude REAL, accuracy_m REAL, people_affected INTEGER, urgent_needs TEXT, coordinator_note TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS assignments (id TEXT PRIMARY KEY, incident_id TEXT, team_name TEXT, status TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS assignment_status_history (id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, status TEXT NOT NULL, changed_by_role TEXT NOT NULL, changed_by_user_id TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chat_messages (id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, assignment_id TEXT, sender_id TEXT NOT NULL, sender_role TEXT NOT NULL, body TEXT NOT NULL, client_message_id TEXT, created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_client_message_id ON chat_messages (sender_id, client_message_id) WHERE client_message_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_chat_messages_incident_created ON chat_messages (incident_id, created_at);
        CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, event_type TEXT, target_id TEXT, metadata TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS location_samples (id TEXT PRIMARY KEY, incident_id TEXT, device_id TEXT, latitude REAL NOT NULL, longitude REAL NOT NULL, accuracy_m REAL, captured_at TEXT NOT NULL, created_at TEXT NOT NULL, CHECK (incident_id IS NOT NULL OR device_id IS NOT NULL));
        CREATE INDEX IF NOT EXISTS idx_location_samples_incident_captured ON location_samples (incident_id, captured_at);""")
        ensure_column(db, "incidents", "reporter_device_id", "TEXT")
        ensure_column(db, "incidents", "regional_visibility", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "assignments", "field_officer_id", "TEXT")
        # Repair duplicate active rows left by an older development database
        # before adding the invariant below.
        duplicate_incidents = db.execute(
            "SELECT incident_id FROM assignments WHERE status NOT IN ('COMPLETED', 'CANCELLED') "
            "GROUP BY incident_id HAVING COUNT(*) > 1"
        ).fetchall()
        for duplicate in duplicate_incidents:
            rows = db.execute(
                "SELECT id FROM assignments WHERE incident_id=? AND status NOT IN ('COMPLETED', 'CANCELLED') "
                "ORDER BY created_at DESC, id DESC",
                (duplicate[0],),
            ).fetchall()
            for row in rows[1:]:
                db.execute("UPDATE assignments SET status='CANCELLED' WHERE id=?", (row[0],))
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_assignments_one_active_per_incident "
            "ON assignments (incident_id) WHERE status NOT IN ('COMPLETED', 'CANCELLED')"
        )
init_db()


SAFE_LOCATIONS = [
    {"id": "safe-central-school", "name": "Central School Shelter", "type": "SHELTER", "latitude": 40.7128, "longitude": -74.0060, "status": "OPEN", "note": "Demo catalog entry; verify capacity and access on arrival."},
    {"id": "safe-riverside-medical", "name": "Riverside Medical Point", "type": "MEDICAL", "latitude": 40.7182, "longitude": -73.9975, "status": "OPEN", "note": "Demo catalog entry; verify operating status on arrival."},
]
def triage(category, description):
    text = f"{category} {description}".lower()
    for priority, words in [(Priority.critical, ["trapped", "not breathing", "severe bleeding", "occupied building", "collapse"]), (Priority.urgent, ["flood", "isolated", "fire", "medical", "injured", "blocked"])]:
        evidence = [w for w in words if w in text]
        if evidence: return priority, evidence
    return Priority.normal, []
def row_dict(row):
    item = dict(row)
    item["urgent_needs"] = json.loads(item["urgent_needs"] or "[]")
    if "regional_visibility" in item:
        item["regional_visibility"] = bool(item["regional_visibility"])
    return item


def field_status_for_incident(incident_id: str, include_identity: bool = False):
    """Return assignment status only; never include GPS/location data."""
    with connect() as db:
        row = db.execute(
            "SELECT id, team_name, field_officer_id, status, created_at FROM assignments "
            "WHERE incident_id=? ORDER BY CASE WHEN status NOT IN ('COMPLETED','CANCELLED') THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
    if not row:
        return {
            "field_officer_status": None,
            "field_officer_assigned": False,
            "field_officer_team": None,
            "assignment_id": None,
        }
    result = {
        "field_officer_status": row["status"],
        "field_officer_assigned": bool(row["field_officer_id"]),
        "field_officer_team": row["team_name"],
        "assignment_id": row["id"],
    }
    if include_identity:
        result["field_officer_id"] = row["field_officer_id"]
    return result


def decorate_incident(item, role: UserRole):
    item.update(field_status_for_incident(item["id"], include_identity=role is not UserRole.citizen))
    return item


def assignment_for_actor(incident_id: str, role: UserRole, user_id: Optional[str]):
    with connect() as db:
        if role is UserRole.admin:
            row = db.execute(
                "SELECT * FROM assignments WHERE incident_id=? ORDER BY created_at DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM assignments WHERE incident_id=? AND field_officer_id=? ORDER BY created_at DESC LIMIT 1",
                (incident_id, user_id),
            ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="This role is not assigned to the incident.")
    return row


REGIONAL_CATEGORIES = {"flood", "cyclone", "wildfire", "earthquake", "regional emergency"}


def is_regional_incident(row) -> bool:
    return bool(row["regional_visibility"]) and row["category"].strip().lower() in REGIONAL_CATEGORIES


def fetch_incident(incident_id: UUID):
    with connect() as db:
        row = db.execute("SELECT * FROM incidents WHERE id=?", (str(incident_id),)).fetchone()
    if not row:
        raise HTTPException(404, "Incident not found")
    return row


def visible_incident_or_403(row, role: UserRole, device_id: Optional[str] = None):
    if role is UserRole.citizen:
        if not device_id:
            raise HTTPException(status_code=403, detail="X-Device-Id is required for citizen access (development auth only).")
        if row["reporter_device_id"] != device_id and not is_regional_incident(row):
            raise HTTPException(status_code=403, detail="Citizen device is not authorized to view this incident.")
    return row

class Hub:
    clients = {}
    async def broadcast(self, event, roles=None, user_ids=None):
        for client, metadata in list(self.clients.items()):
            if roles and metadata["role"] not in roles:
                continue
            if user_ids and metadata["user_id"] not in user_ids:
                continue
            try: await client.send_json(event)
            except Exception: self.clients.pop(client, None)
hub = Hub()

@app.on_event("startup")
def startup(): init_db()
@app.get("/health")
def health(): return {"status":"ok", "service":"sentinel-ops-api", "database":"sqlite"}
@app.post("/api/v1/auth/demo-login")
def demo_login(payload: DemoLogin):
    """Issue a deliberately local, non-secret demo identity; this is not production authentication."""
    key = payload.role.strip().lower().replace("-", "_").replace(" ", "_")
    key = LEGACY_DEMO_ROLE_ALIASES.get(key, key)
    if key not in DEMO_USERS:
        raise HTTPException(status_code=422, detail="Choose admin, field officer, or citizen for the local demo login.")
    user = DEMO_USERS[key]
    return {
        "mode": "LOCAL_DEMO_ONLY",
        "authenticated": True,
        "role": user["role"].value,
        "user_id": user["user_id"],
        "display_name": user["display_name"],
        "session": f"demo-session-{key}",
        "production_auth": False,
    }
@app.get("/api/v1/system/status")
def system_status():
    with connect() as db: count = db.execute("SELECT COUNT(*) FROM incidents WHERE status NOT IN ('RESOLVED','CLOSED')").fetchone()[0]
    return {"mode":"LOCAL_DEVELOPMENT", "open_incidents":count, "last_sync":None, "transport":"UNSUPPORTED"}


@app.post("/api/v1/admin/demo-data/clear")
def clear_demo_data(_: UserRole = Depends(require_admin)):
    """Clear server-side demo data without touching browser-local offline caches.

    The local MVP currently has incidents, assignments, location samples, and
    audit history. The name-based selection also clears optional message or
    history tables if a later local schema adds them.
    """
    with connect() as db:
        table_names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        clearable = {
            "incidents", "assignments", "location_samples", "audit_events",
        }
        clearable.update(
            name for name in table_names
            if any(token in name.lower() for token in ("message", "history", "histories"))
        )
        counts = {}
        for table in table_names:
            if table not in clearable:
                continue
            counts[table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            db.execute(f'DELETE FROM "{table}"')
    result = {"cleared": counts, "total": sum(counts.values()), "scope": "SERVER_DEMO_DATA_ONLY"}
    return result

@app.post("/api/v1/incidents", status_code=201)
async def create_incident(
    payload: IncidentCreate,
    role: UserRole = Depends(development_role),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-Id"),
):
    if role is UserRole.field_officer:
        raise HTTPException(status_code=403, detail="Field officers use assigned dispatch updates and location reporting (development auth only).")
    device_id = x_device_id.strip() if x_device_id and x_device_id.strip() else None
    if role is UserRole.citizen and not device_id:
        raise HTTPException(status_code=403, detail="X-Device-Id is required for citizen reports (development auth only).")
    if role is UserRole.citizen and payload.reporter_device_id and payload.reporter_device_id != device_id:
        raise HTTPException(status_code=403, detail="reporter_device_id must match X-Device-Id for citizen reports.")
    if role is UserRole.citizen and payload.regional_visibility:
        raise HTTPException(status_code=403, detail="Only an admin can mark an incident regionally visible (development auth only).")
    reporter_device_id = payload.reporter_device_id or device_id
    priority, evidence = triage(payload.category, payload.description); incident_id, timestamp = str(uuid4()), now()
    with connect() as db:
        db.execute(
            "INSERT INTO incidents (id, category, description, priority, status, delivery_state, latitude, longitude, accuracy_m, "
            "people_affected, urgent_needs, coordinator_note, created_at, updated_at, reporter_device_id, regional_visibility) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (incident_id, payload.category, payload.description, priority.value, "AWAITING_VERIFICATION", "ACKNOWLEDGED", payload.latitude, payload.longitude, payload.accuracy_m, payload.people_affected, json.dumps(payload.urgent_needs), "", timestamp, timestamp, reporter_device_id, int(payload.regional_visibility)),
        )
        db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (str(uuid4()), "INCIDENT_CREATED", incident_id, json.dumps({"triage_evidence":evidence}), timestamp))
    result = {"id":incident_id,"priority":priority.value,"triage_evidence":evidence,"delivery_state":"ACKNOWLEDGED","status":"AWAITING_VERIFICATION","created_at":timestamp,"reporter_device_id":reporter_device_id,"regional_visibility":payload.regional_visibility}
    await hub.broadcast({"type":"incident.created","data":result}, roles={"ADMIN"})
    if reporter_device_id:
        await hub.broadcast({"type":"incident.created","data":{**result, "reporter_device_id": None}}, roles={"CITIZEN"}, user_ids={reporter_device_id})
    return result

@app.get("/api/v1/incidents")
def list_incidents(
    search: str = "",
    status: Optional[str] = None,
    priority: Optional[Priority] = None,
    limit: int = Query(50, ge=1, le=200),
    role: UserRole = Depends(development_role),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-Id"),
):
    if role is UserRole.field_officer:
        raise HTTPException(status_code=403, detail="Field officers receive only their assigned briefing (development auth only).")
    device_id = x_device_id.strip() if x_device_id and x_device_id.strip() else None
    if role is UserRole.citizen and not device_id:
        raise HTTPException(status_code=403, detail="X-Device-Id is required for citizen access (development auth only).")
    query = "SELECT * FROM incidents WHERE (category LIKE ? OR description LIKE ?)"; params = [f"%{search}%", f"%{search}%"]
    if role is UserRole.citizen:
        regional_categories = ",".join("?" for _ in REGIONAL_CATEGORIES)
        query += f" AND (reporter_device_id = ? OR (regional_visibility = 1 AND lower(category) IN ({regional_categories})))"
        params.extend([device_id, *sorted(REGIONAL_CATEGORIES)])
    if status: query += " AND status = ?"; params.append(status)
    if priority: query += " AND priority = ?"; params.append(priority.value)
    query += " ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'URGENT' THEN 1 ELSE 2 END, created_at DESC LIMIT ?"; params.append(limit)
    with connect() as db: items = [decorate_incident(row_dict(r), role) for r in db.execute(query, params).fetchall()]
    return {"items":items,"count":len(items)}


@app.get("/api/v1/my/incidents")
def list_my_incidents(device_id: str = Depends(require_citizen)):
    with connect() as db:
        items = [decorate_incident(row_dict(r), UserRole.citizen) for r in db.execute(
            "SELECT * FROM incidents WHERE reporter_device_id=? ORDER BY created_at DESC",
            (device_id,),
        ).fetchall()]
    return {"items": items, "count": len(items)}


@app.get("/api/v1/incidents/{incident_id}")
def get_incident(
    incident_id: UUID,
    role: UserRole = Depends(development_role),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-Id"),
):
    if role is UserRole.field_officer:
        raise HTTPException(status_code=403, detail="Field officers receive incident details through their assigned briefing (development auth only).")
    row = fetch_incident(incident_id)
    device_id = x_device_id.strip() if x_device_id and x_device_id.strip() else None
    visible_incident_or_403(row, role, device_id)
    return decorate_incident(row_dict(row), role)
@app.patch("/api/v1/incidents/{incident_id}")
async def update_incident(incident_id: UUID, patch: IncidentPatch, _: UserRole = Depends(require_admin)):
    current = row_dict(fetch_incident(incident_id)); allowed = {"AWAITING_VERIFICATION":["VERIFIED","CLOSED"],"VERIFIED":["DISPATCHED","RESOLVED"],"DISPATCHED":["RESOLVED"]}
    allowed_next = allowed.get(current["status"], [])
    if patch.status and patch.status != current["status"] and patch.status not in allowed_next:
        options = ", ".join(allowed_next) if allowed_next else "none"
        raise HTTPException(409, f"Invalid status transition from {current['status']} to {patch.status}. Allowed next statuses: {options}.")
    timestamp = now(); values = (patch.status or current["status"], patch.priority.value if patch.priority else current["priority"], patch.coordinator_note if patch.coordinator_note is not None else current["coordinator_note"], int(patch.regional_visibility) if patch.regional_visibility is not None else int(current["regional_visibility"]), timestamp, str(incident_id))
    with connect() as db: db.execute("UPDATE incidents SET status=?,priority=?,coordinator_note=?,regional_visibility=?,updated_at=? WHERE id=?", values); db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (str(uuid4()),"INCIDENT_UPDATED",str(incident_id),json.dumps({"status":values[0],"regional_visibility":bool(values[3])}),timestamp))
    result = decorate_incident(row_dict(fetch_incident(incident_id)), UserRole.admin)
    await hub.broadcast({"type":"incident.updated","data":result}, roles={"ADMIN"})
    if result.get("reporter_device_id"):
        citizen_result = {key: value for key, value in result.items() if key != "reporter_device_id" and key not in {"latitude", "longitude", "accuracy_m"}}
        await hub.broadcast({"type":"incident.updated","data":citizen_result}, roles={"CITIZEN"}, user_ids={result["reporter_device_id"]})
    return result
@app.post("/api/v1/assignments", status_code=201)
async def create_assignment(payload: AssignmentCreate, _: UserRole = Depends(require_admin)):
    incident = fetch_incident(payload.incident_id)
    incident_key = str(payload.incident_id)
    with connect() as db:
        assignments = db.execute(
            "SELECT status FROM assignments WHERE incident_id=? ORDER BY created_at DESC",
            (incident_key,),
        ).fetchall()
        active = next((row for row in assignments if row["status"] not in {"COMPLETED", "CANCELLED"}), None)
        if active:
            raise HTTPException(status_code=409, detail="Incident already has an active assignment; complete or cancel it before dispatching again.")
        if assignments and not payload.reassign:
            raise HTTPException(status_code=409, detail="Incident was previously assigned; set reassign=true after the prior assignment is completed or cancelled.")
        if payload.reassign and not assignments:
            raise HTTPException(status_code=409, detail="Reassignment requires a prior completed or cancelled assignment.")
        assignment = {"id":str(uuid4()),"incident_id":incident_key,"team_name":payload.team_name,"field_officer_id":payload.field_officer_id,"status":"PENDING","notes":payload.notes,"created_at":now()}
        try:
            db.execute("INSERT INTO assignments (id, incident_id, team_name, status, notes, created_at, field_officer_id) VALUES (?,?,?,?,?,?,?)", (assignment["id"], assignment["incident_id"], assignment["team_name"], assignment["status"], assignment["notes"], assignment["created_at"], assignment["field_officer_id"]))
            db.execute(
                "INSERT INTO assignment_status_history VALUES (?,?,?,?,?,?)",
                (str(uuid4()), assignment["id"], "PENDING", UserRole.admin.value, "demo-admin-1", assignment["created_at"]),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Incident already has an active assignment; complete or cancel it before dispatching again.")
        db.execute("UPDATE incidents SET status='DISPATCHED',updated_at=? WHERE id=?", (assignment["created_at"], incident_key))
    await hub.broadcast(
        {"type":"assignment.created","data":assignment},
        roles={"ADMIN", "FIELD_OFFICER"},
    )
    if incident["reporter_device_id"]:
        await hub.broadcast(
            {"type":"assignment.created","data":{"incident_id": assignment["incident_id"], "status": "PENDING", "team_name": assignment["team_name"]}},
            roles={"CITIZEN"}, user_ids={incident["reporter_device_id"]},
        )
    return assignment


@app.patch("/api/v1/assignments/{assignment_id}")
async def update_assignment(assignment_id: UUID, patch: AssignmentPatch, role: UserRole = Depends(development_role), user_id: Optional[str] = Depends(demo_user_id)):
    if role not in {UserRole.admin, UserRole.field_officer}:
        raise HTTPException(status_code=403, detail="Admin or assigned field officer role required (development auth only).")
    with connect() as db:
        row = db.execute("SELECT * FROM assignments WHERE id=?", (str(assignment_id),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assignment not found")
        if role is UserRole.field_officer and (not user_id or row["field_officer_id"] != user_id):
            raise HTTPException(status_code=403, detail="Field officer is not assigned to this dispatch.")
        if role is UserRole.field_officer and patch.status != row["status"]:
            allowed = {"PENDING": {"EN_ROUTE", "CANCELLED"}, "EN_ROUTE": {"ON_SCENE", "CANCELLED"}, "ON_SCENE": {"COMPLETED", "CANCELLED"}}
            if patch.status not in allowed.get(row["status"], set()):
                raise HTTPException(status_code=409, detail=f"Field officer status must advance from {row['status']} in order.")
        if row["status"] in {"COMPLETED", "CANCELLED"} and patch.status != row["status"]:
            raise HTTPException(status_code=409, detail="Completed or cancelled assignments cannot be reopened.")
        changed_at = now()
        db.execute("UPDATE assignments SET status=? WHERE id=?", (patch.status, str(assignment_id)))
        db.execute(
            "INSERT INTO assignment_status_history VALUES (?,?,?,?,?,?)",
            (str(uuid4()), str(assignment_id), patch.status, role.value, user_id or "demo-admin-1", changed_at),
        )
        incident = db.execute("SELECT reporter_device_id FROM incidents WHERE id=?", (row["incident_id"],)).fetchone()
    result = {**dict(row), "status": patch.status, "updated_at": changed_at}
    await hub.broadcast({"type":"assignment.updated","data":result}, roles={"ADMIN", "FIELD_OFFICER"})
    if incident and incident["reporter_device_id"]:
        await hub.broadcast(
            {"type":"assignment.updated","data":{"incident_id": row["incident_id"], "status": patch.status, "team_name": row["team_name"]}},
            roles={"CITIZEN"}, user_ids={incident["reporter_device_id"]},
        )
    return result
@app.get("/api/v1/assignments")
def list_assignments(_: UserRole = Depends(require_admin)):
    with connect() as db: rows = [dict(r) for r in db.execute("SELECT * FROM assignments ORDER BY created_at DESC").fetchall()]
    return {"items":rows,"count":len(rows)}


@app.get("/api/v1/assignments/history")
def assignment_history(_: UserRole = Depends(require_admin)):
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT h.*, a.incident_id, a.team_name, a.field_officer_id, i.category "
            "FROM assignment_status_history h JOIN assignments a ON a.id=h.assignment_id "
            "JOIN incidents i ON i.id=a.incident_id ORDER BY h.created_at DESC"
        ).fetchall()]
    return {"items": rows, "count": len(rows)}


@app.get("/api/v1/field-officer/tasks/history")
def field_officer_task_history(user_id: str = Depends(require_field_officer)):
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT a.*, i.category, i.description, i.priority, i.status AS incident_status "
            "FROM assignments a JOIN incidents i ON i.id=a.incident_id "
            "WHERE a.field_officer_id=? AND a.status IN ('COMPLETED','CANCELLED') "
            "ORDER BY a.created_at DESC",
            (user_id,),
        ).fetchall()]
    return {"items": rows, "count": len(rows), "field_officer_id": user_id}


def chat_actor(role: UserRole, user_id: Optional[str]) -> str:
    return user_id or ("demo-admin-1" if role is UserRole.admin else "")


def authorize_chat(incident_id: UUID, role: UserRole, user_id: Optional[str]):
    if role not in {UserRole.admin, UserRole.field_officer}:
        raise HTTPException(status_code=403, detail="Admin or assigned field officer role required for chat (development auth only).")
    return assignment_for_actor(str(incident_id), role, user_id)


@app.get("/api/v1/incidents/{incident_id}/chat")
def list_chat_messages(
    incident_id: UUID,
    role: UserRole = Depends(development_role),
    user_id: Optional[str] = Depends(demo_user_id),
    limit: int = Query(200, ge=1, le=500),
):
    assignment = authorize_chat(incident_id, role, user_id)
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT id, incident_id, assignment_id, sender_id, sender_role, body, client_message_id, created_at "
            "FROM chat_messages WHERE incident_id=? ORDER BY created_at ASC LIMIT ?",
            (str(incident_id), limit),
        ).fetchall()]
    return {"items": rows, "count": len(rows), "assignment_id": assignment["id"]}


@app.post("/api/v1/incidents/{incident_id}/chat", status_code=201)
async def create_chat_message(
    incident_id: UUID,
    payload: ChatMessageCreate,
    role: UserRole = Depends(development_role),
    user_id: Optional[str] = Depends(demo_user_id),
):
    assignment = authorize_chat(incident_id, role, user_id)
    sender_id = chat_actor(role, user_id)
    message_id = str(uuid4())
    created_at = now()
    message = {
        "id": message_id,
        "incident_id": str(incident_id),
        "assignment_id": assignment["id"],
        "sender_id": sender_id,
        "sender_role": role.value,
        "body": payload.body,
        "client_message_id": payload.client_message_id,
        "created_at": created_at,
        "sync_state": "PERSISTED",
    }
    with connect() as db:
        if payload.client_message_id:
            existing = db.execute(
                "SELECT id, incident_id, assignment_id, sender_id, sender_role, body, client_message_id, created_at "
                "FROM chat_messages WHERE sender_id=? AND client_message_id=?",
                (sender_id, payload.client_message_id),
            ).fetchone()
            if existing:
                return {**dict(existing), "sync_state": "PERSISTED"}
        db.execute(
            "INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?)",
            (message_id, str(incident_id), assignment["id"], sender_id, role.value, payload.body, payload.client_message_id, created_at),
        )
    await hub.broadcast({"type": "chat.message.created", "data": message}, roles={"ADMIN"})
    if assignment["field_officer_id"]:
        await hub.broadcast(
            {"type": "chat.message.created", "data": message},
            roles={"FIELD_OFFICER"}, user_ids={assignment["field_officer_id"]},
        )
    return message


@app.get("/api/v1/chat/messages")
def list_chat_messages_alias(
    incident_id: UUID,
    role: UserRole = Depends(development_role),
    user_id: Optional[str] = Depends(demo_user_id),
    limit: int = Query(200, ge=1, le=500),
):
    return list_chat_messages(incident_id, role, user_id, limit)


@app.post("/api/v1/chat/messages", status_code=201)
async def create_chat_message_alias(
    payload: ChatMessageCreate,
    incident_id: UUID = Query(...),
    role: UserRole = Depends(development_role),
    user_id: Optional[str] = Depends(demo_user_id),
):
    return await create_chat_message(incident_id, payload, role, user_id)


@app.get("/api/v1/field-officer/briefing")
def field_officer_briefing(user_id: str = Depends(require_field_officer)):
    with connect() as db:
        rows = db.execute(
            "SELECT a.*, i.category, i.description, i.priority, i.status AS incident_status, i.latitude, i.longitude, "
            "i.accuracy_m, i.people_affected, i.coordinator_note, i.updated_at AS incident_updated_at "
            "FROM assignments a JOIN incidents i ON i.id=a.incident_id "
            "WHERE a.field_officer_id=? AND a.status NOT IN ('COMPLETED','CANCELLED') ORDER BY a.created_at DESC",
            (user_id,),
        ).fetchall()
    assignments = [dict(row) for row in rows]
    return {
        "mode": "LOCAL_DEMO_ONLY",
        "field_officer_id": user_id,
        "assignments": assignments,
        "safe_locations": SAFE_LOCATIONS,
        "production_auth": False,
    }


@app.get("/api/v1/locations")
def list_locations(_: UserRole = Depends(require_admin)):
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM location_samples ORDER BY captured_at DESC, created_at DESC"
        ).fetchall()]
    return {"items": rows, "count": len(rows)}


@app.post("/api/v1/field-officer/locations", status_code=201)
async def create_field_officer_location(payload: LocationSampleCreate, user_id: str = Depends(require_field_officer)):
    if payload.device_id and payload.device_id != user_id:
        raise HTTPException(status_code=403, detail="Field officer location device_id must match the local demo identity.")
    if payload.incident_id is not None:
        fetch_incident(payload.incident_id)
        with connect() as db:
            assigned = db.execute(
                "SELECT 1 FROM assignments WHERE incident_id=? AND field_officer_id=? AND status NOT IN ('COMPLETED','CANCELLED')",
                (str(payload.incident_id), user_id),
            ).fetchone()
        if not assigned:
            raise HTTPException(status_code=403, detail="Field officer is not assigned to this incident.")
    sample_id, created_at = str(uuid4()), now()
    captured_at = payload.captured_at.astimezone(timezone.utc).isoformat()
    sample = {"id": sample_id, "incident_id": str(payload.incident_id) if payload.incident_id is not None else None, "device_id": user_id, "latitude": payload.latitude, "longitude": payload.longitude, "accuracy_m": payload.accuracy_m, "captured_at": captured_at, "created_at": created_at}
    with connect() as db:
        db.execute("INSERT INTO location_samples VALUES (?,?,?,?,?,?,?,?)", tuple(sample.values()))
    await hub.broadcast({"type": "location.updated", "data": sample}, roles={"ADMIN"})
    return sample
@app.post("/api/v1/locations", status_code=201)
async def create_location(payload: LocationSampleCreate, role: UserRole = Depends(development_role)):
    if role is UserRole.field_officer:
        raise HTTPException(status_code=403, detail="Field officers must use the assignment-scoped location endpoint (development auth only).")
    if payload.incident_id is not None:
        fetch_incident(payload.incident_id)
    sample_id, created_at = str(uuid4()), now()
    captured_at = payload.captured_at.astimezone(timezone.utc).isoformat()
    sample = {
        "id": sample_id,
        "incident_id": str(payload.incident_id) if payload.incident_id is not None else None,
        "device_id": payload.device_id,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "accuracy_m": payload.accuracy_m,
        "captured_at": captured_at,
        "created_at": created_at,
    }
    with connect() as db:
        db.execute("INSERT INTO location_samples VALUES (?,?,?,?,?,?,?,?)", tuple(sample.values()))
    await hub.broadcast({"type": "location.updated", "data": sample}, roles={"ADMIN"})
    return sample

@app.get("/api/v1/incidents/{incident_id}/locations")
def list_incident_locations(incident_id: UUID, _: UserRole = Depends(require_admin)):
    fetch_incident(incident_id)
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM location_samples WHERE incident_id=? ORDER BY captured_at ASC, created_at ASC",
            (str(incident_id),),
        ).fetchall()]
    return {"items": rows, "count": len(rows)}
@app.websocket("/ws/events")
async def events(websocket: WebSocket):
    raw_role = (websocket.query_params.get("role") or "ADMIN").strip().upper().replace("-", "_")
    raw_role = LEGACY_ROLE_ALIASES.get(raw_role, raw_role)
    try:
        role = UserRole(raw_role).value
    except ValueError:
        await websocket.close(code=1008)
        return
    user_id = (websocket.query_params.get("user_id") or websocket.query_params.get("device_id") or "").strip()
    await websocket.accept(); hub.clients[websocket] = {"role": role, "user_id": user_id}
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: hub.clients.pop(websocket, None)
