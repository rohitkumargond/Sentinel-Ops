from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4
import json
import sqlite3

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "sentinel_ops.db"
app = FastAPI(title="Sentinel-Ops API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class Priority(str, Enum):
    critical = "CRITICAL"; urgent = "URGENT"; normal = "NORMAL"; informational = "INFORMATIONAL"

class UserRole(str, Enum):
    citizen = "CITIZEN"
    coordinator = "COORDINATOR"

# Development-only role selection. This is deliberately not production authentication.
def development_role(
    x_dev_role: Optional[str] = Header(default=None, alias="X-Dev-Role"),
    x_role: Optional[str] = Header(default=None, alias="X-Role"),
) -> UserRole:
    value = (x_dev_role or x_role or UserRole.coordinator.value).strip().upper()
    try:
        return UserRole(value)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Invalid development role. Use X-Dev-Role: CITIZEN or X-Dev-Role: COORDINATOR.",
        )

def require_coordinator(role: UserRole = Depends(development_role)) -> UserRole:
    if role is not UserRole.coordinator:
        raise HTTPException(
            status_code=403,
            detail="Coordinator role required for this endpoint (development auth only).",
        )
    return role

class IncidentCreate(BaseModel):
    category: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=3, max_length=2000)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    people_affected: int = Field(default=1, ge=1, le=10000)
    urgent_needs: list[str] = Field(default_factory=list)

class IncidentPatch(BaseModel):
    status: Optional[str] = None
    priority: Optional[Priority] = None
    coordinator_note: Optional[str] = Field(default=None, max_length=2000)

class AssignmentCreate(BaseModel):
    incident_id: UUID; team_name: str = Field(min_length=2, max_length=120); notes: str = Field(default="", max_length=1000)

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
def init_db():
    with connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, category TEXT, description TEXT, priority TEXT, status TEXT, delivery_state TEXT, latitude REAL, longitude REAL, accuracy_m REAL, people_affected INTEGER, urgent_needs TEXT, coordinator_note TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS assignments (id TEXT PRIMARY KEY, incident_id TEXT, team_name TEXT, status TEXT, notes TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS audit_events (id TEXT PRIMARY KEY, event_type TEXT, target_id TEXT, metadata TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS location_samples (id TEXT PRIMARY KEY, incident_id TEXT, device_id TEXT, latitude REAL NOT NULL, longitude REAL NOT NULL, accuracy_m REAL, captured_at TEXT NOT NULL, created_at TEXT NOT NULL, CHECK (incident_id IS NOT NULL OR device_id IS NOT NULL));
        CREATE INDEX IF NOT EXISTS idx_location_samples_incident_captured ON location_samples (incident_id, captured_at);""")
init_db()
def triage(category, description):
    text = f"{category} {description}".lower()
    for priority, words in [(Priority.critical, ["trapped", "not breathing", "severe bleeding", "occupied building", "collapse"]), (Priority.urgent, ["flood", "isolated", "fire", "medical", "injured", "blocked"])]:
        evidence = [w for w in words if w in text]
        if evidence: return priority, evidence
    return Priority.normal, []
def row_dict(row):
    item = dict(row); item["urgent_needs"] = json.loads(item["urgent_needs"]); return item

class Hub:
    clients = set()
    async def broadcast(self, event):
        for client in list(self.clients):
            try: await client.send_json(event)
            except Exception: self.clients.discard(client)
hub = Hub()

@app.on_event("startup")
def startup(): init_db()
@app.get("/health")
def health(): return {"status":"ok", "service":"sentinel-ops-api", "database":"sqlite"}
@app.get("/api/v1/system/status")
def system_status():
    with connect() as db: count = db.execute("SELECT COUNT(*) FROM incidents WHERE status NOT IN ('RESOLVED','CLOSED')").fetchone()[0]
    return {"mode":"LOCAL_DEVELOPMENT", "open_incidents":count, "last_sync":None, "transport":"UNSUPPORTED"}

@app.post("/api/v1/incidents", status_code=201)
async def create_incident(payload: IncidentCreate):
    priority, evidence = triage(payload.category, payload.description); incident_id, timestamp = str(uuid4()), now()
    with connect() as db:
        db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (incident_id, payload.category, payload.description, priority.value, "AWAITING_VERIFICATION", "ACKNOWLEDGED", payload.latitude, payload.longitude, payload.accuracy_m, payload.people_affected, json.dumps(payload.urgent_needs), "", timestamp, timestamp))
        db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (str(uuid4()), "INCIDENT_CREATED", incident_id, json.dumps({"triage_evidence":evidence}), timestamp))
    result = {"id":incident_id,"priority":priority.value,"triage_evidence":evidence,"delivery_state":"ACKNOWLEDGED","status":"AWAITING_VERIFICATION","created_at":timestamp}; await hub.broadcast({"type":"incident.created","data":result}); return result

@app.get("/api/v1/incidents")
def list_incidents(search: str = "", status: Optional[str] = None, priority: Optional[Priority] = None, limit: int = Query(50, ge=1, le=200)):
    query = "SELECT * FROM incidents WHERE (category LIKE ? OR description LIKE ?)"; params = [f"%{search}%", f"%{search}%"]
    if status: query += " AND status = ?"; params.append(status)
    if priority: query += " AND priority = ?"; params.append(priority.value)
    query += " ORDER BY CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'URGENT' THEN 1 ELSE 2 END, created_at DESC LIMIT ?"; params.append(limit)
    with connect() as db: items = [row_dict(r) for r in db.execute(query, params).fetchall()]
    return {"items":items,"count":len(items)}
@app.get("/api/v1/incidents/{incident_id}")
def get_incident(incident_id: UUID):
    with connect() as db: row = db.execute("SELECT * FROM incidents WHERE id=?", (str(incident_id),)).fetchone()
    if not row: raise HTTPException(404, "Incident not found")
    return row_dict(row)
@app.patch("/api/v1/incidents/{incident_id}")
async def update_incident(incident_id: UUID, patch: IncidentPatch, _: UserRole = Depends(require_coordinator)):
    current = get_incident(incident_id); allowed = {"AWAITING_VERIFICATION":["VERIFIED","CLOSED"],"VERIFIED":["DISPATCHED","RESOLVED"],"DISPATCHED":["RESOLVED"]}
    allowed_next = allowed.get(current["status"], [])
    if patch.status and patch.status != current["status"] and patch.status not in allowed_next:
        options = ", ".join(allowed_next) if allowed_next else "none"
        raise HTTPException(409, f"Invalid status transition from {current['status']} to {patch.status}. Allowed next statuses: {options}.")
    timestamp = now(); values = (patch.status or current["status"], patch.priority.value if patch.priority else current["priority"], patch.coordinator_note if patch.coordinator_note is not None else current["coordinator_note"], timestamp, str(incident_id))
    with connect() as db: db.execute("UPDATE incidents SET status=?,priority=?,coordinator_note=?,updated_at=? WHERE id=?", values); db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?)", (str(uuid4()),"INCIDENT_UPDATED",str(incident_id),json.dumps({"status":values[0]}),timestamp))
    result = get_incident(incident_id); await hub.broadcast({"type":"incident.updated","data":result}); return result
@app.post("/api/v1/assignments", status_code=201)
async def create_assignment(payload: AssignmentCreate, _: UserRole = Depends(require_coordinator)):
    get_incident(payload.incident_id); assignment = {"id":str(uuid4()),"incident_id":str(payload.incident_id),"team_name":payload.team_name,"status":"PENDING","notes":payload.notes,"created_at":now()}
    with connect() as db: db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)", tuple(assignment.values())); db.execute("UPDATE incidents SET status='DISPATCHED',updated_at=? WHERE id=?", (assignment["created_at"], assignment["incident_id"]))
    await hub.broadcast({"type":"assignment.created","data":assignment}); return assignment
@app.get("/api/v1/assignments")
def list_assignments(_: UserRole = Depends(require_coordinator)):
    with connect() as db: rows = [dict(r) for r in db.execute("SELECT * FROM assignments ORDER BY created_at DESC").fetchall()]
    return {"items":rows,"count":len(rows)}
@app.post("/api/v1/locations", status_code=201)
async def create_location(payload: LocationSampleCreate, _: UserRole = Depends(development_role)):
    if payload.incident_id is not None:
        get_incident(payload.incident_id)
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
    await hub.broadcast({"type": "location.updated", "data": sample})
    return sample

@app.get("/api/v1/incidents/{incident_id}/locations")
def list_incident_locations(incident_id: UUID, _: UserRole = Depends(require_coordinator)):
    get_incident(incident_id)
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM location_samples WHERE incident_id=? ORDER BY captured_at ASC, created_at ASC",
            (str(incident_id),),
        ).fetchall()]
    return {"items": rows, "count": len(rows)}
@app.websocket("/ws/events")
async def events(websocket: WebSocket):
    await websocket.accept(); hub.clients.add(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: hub.clients.discard(websocket)
