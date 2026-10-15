from fastapi.testclient import TestClient
from backend.app.main import app, DB_PATH, init_db
client = TestClient(app)
def setup_function():
    if DB_PATH.exists(): DB_PATH.unlink()
    init_db()
def test_health(): assert client.get('/health').json()['status'] == 'ok'
def test_incident_triage_and_transition():
    created = client.post('/api/v1/incidents', json={'category':'Fire','description':'Person trapped in occupied building','people_affected':2}).json(); assert created['priority']=='CRITICAL'
    assert client.get(f"/api/v1/incidents/{created['id']}").status_code == 200
    assert client.patch(f"/api/v1/incidents/{created['id']}", json={'status':'RESOLVED'}).status_code == 409
def test_dispatch():
    incident = client.post('/api/v1/incidents', json={'category':'Flood','description':'Road blocked'}).json(); assert client.post('/api/v1/assignments', json={'incident_id':incident['id'],'team_name':'Alpha Rescue'}).status_code == 201; assert client.get('/api/v1/assignments').json()['count'] == 1
