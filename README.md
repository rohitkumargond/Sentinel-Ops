# Sentinel-Ops

Offline-first emergency coordination MVP. It currently implements a real FastAPI incident/triage/dispatch API backed by SQLite, a responsive command dashboard, WebSocket incident events, audit records, and local-first incident capture in the dashboard. Unsupported transports are reported honestly rather than fabricated.

## Quick start

Requires Python 3.11+ and Node 20+. PostgreSQL, Android SDK, and physical devices are not required for this implemented slice.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
make test
make run-backend
```

In another terminal: `cd apps/dashboard && npm install && npm run dev`. Open http://localhost:5173. API: http://localhost:8000, OpenAPI: http://localhost:8000/docs. Build with `make build-dashboard`.

## Citizen phone reporting view

Open `http://localhost:5173/#/report` or select **OPEN CITIZEN VIEW**. This is a responsive phone-friendly reporting surface with emergency categories, description validation, people affected, browser GPS permission handling, accuracy display, local-first API submission, and a clear emergency-services warning. The dashboard remains a separate coordinator surface.

## Google Maps

Set `VITE_GOOGLE_MAPS_API_KEY` in the dashboard environment when you have an authorized Google Maps Embed API key. The map iframe then uses Google Maps centered on the first incident with coordinates. Without a key, the app uses Google's public embed URL as a development fallback; production deployments should configure a restricted key, billing, attribution, and the required Google Maps terms. Incident coordinates are never fabricated by the API.

## Implemented

- Incident form validates and saves to local SQLite immediately.
- Rule-based transparent triage returns priority and evidence.
- Searchable queue, live WebSocket refresh across connected dashboard clients, audit events.
- Server-side status transition validation and dispatch creation.
- Responsive dark command interface with operational states and navigation foundation.

## Known limits

This MVP does not yet include production authentication/RBAC, PostgreSQL/PostGIS migrations, rate limiting, encrypted message envelopes, or the React Native Android client. GPS in the citizen view uses browser permission and is not background tracking. Offline maps, Wi-Fi Direct, and physical-device testing are not claimed. A locally saved report is not confirmation that emergency services received it. Configure secrets through `.env` using `.env.example`; never commit credentials.

## Next steps

1. Add JWT authentication and central permission dependencies.
2. Add PostgreSQL/PostGIS and Alembic migrations.
3. Add React Native Android SQLite/location/offline-map shell and Kotlin Wi-Fi Direct transport.
4. Run the documented two-device transfer plan on supported hardware.
