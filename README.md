# Sentinel-Ops

Offline-first emergency coordination MVP. The workspace contains a FastAPI + SQLite command API and a Vite/React dashboard. The implemented slice includes incident intake and triage, admin dispatch and field-officer assignments, real GPS sample storage, WebSocket events, audit records, and browser-local offline queues.

## Setup

Pinned runtimes are recorded in `.python-version` (Python 3.11) and `.nvmrc` (Node 20). PostgreSQL, Android tooling, and physical devices are not required for this local build.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.txt
make install-dashboard
```

The repository root now has a workspace dependency lockfile at `package-lock.json`, and the dashboard also retains `apps/dashboard/package-lock.json` for standalone use. Use `npm ci` at the repository root for a clean workspace install, or `npm ci` in `apps/dashboard` for a dashboard-only install. Keep credentials in a local `.env` based on `.env.example`; never commit secrets.

## Run

Start the API and dashboard in separate terminals:

```bash
make run-backend                         # http://localhost:8000
make run-dashboard                       # http://localhost:5173
```

Useful endpoints are `/health` and `/docs`. Build the dashboard with `make build-dashboard`. The Docker image runs the API on port 8000:

```bash
docker build -t sentinel-ops .
docker run --rm -p 8000:8000 sentinel-ops
```

## Test

The single offline test command is:

```bash
make test
```

It runs the FastAPI tests from `backend/tests`. Dashboard production compilation is available separately with `make build-dashboard`.

## Local demo entry

Open `http://localhost:5173/#/role-select`. The local entry screen provides:

- **Admin sign-in:** use the quick-fill button or email `admin@sentinel.local` and password `sentinel-demo`. This is browser-local demo validation only.
- **Field officer:** choose **Continue without login · Local demo access**. The field workflow uses a seeded local field identity and works from its cached briefing while offline.
- **Citizen:** choose **Continue without login · Local demo access**. Citizen reports and GPS preferences remain scoped to the browser device.

The three local demo roles are:

- **Admin** is the command role. It can review and update incidents, assign field-officer tasks with instructions, read assignments, and access admin location APIs.
- **Field officer** is separate from admin. It can read its assigned briefing, advance its own assignment through `PENDING → EN_ROUTE → ON_SCENE → COMPLETED` (or cancel it), and submit assignment-scoped GPS samples.
- **Citizen** can submit reports, share device GPS, and view only its device-scoped report status plus admin-published regional incidents.

Citizen and Field Officer sessions remain available from their browser-local cache when the API is offline, but each session is guarded to its own role route and persistent device scope. Direct links to another role's dashboard are redirected back to the current role's landing view. To choose another local role, open `#/role-select`; Citizen and Field Officer dashboards intentionally do not expose a workspace or switch-role control. The Field Officer header shows its persistent device identifier in masked form (`Device ···xxxxxx`). Admin retains the workspace and logout controls.

The demo endpoint is `POST /api/v1/auth/demo-login` with `{"role":"admin"}`, `{"role":"field officer"}`, or `{"role":"citizen"}`. `coordinator` remains accepted as a legacy login alias and resolves to the Admin identity so older local clients do not create a second command role. Older `X-Dev-Role: COORDINATOR` headers are also normalized to `ADMIN`; new clients should send `X-Dev-Role: ADMIN`.

These identities and `X-Dev-Role`/`X-Demo-User` headers are local development selectors only. Responses include `production_auth: false`. This project does not claim production authentication, token verification, or a production RBAC security boundary.

Citizen navigation is route-backed: `#/report`, `#/map`, `#/status`, `#/profile`, and `#/home` reopen the selected citizen surface after a browser refresh. The citizen Home uses the desktop workspace layout on wide screens and the compact app-like navigation on mobile widths. Role routes are guarded in the browser: cached Citizen and Field Officer sessions cannot cross into each other or Admin routes without returning through the local entry screen.

On narrow Admin screens, incident details open as a bottom-sheet drawer: the detail body scrolls inside the viewport, the close control remains available, and the update actions stay reachable at the bottom. The operations map uses visible markers derived from stored incident/GPS coordinates. Incidents without coordinates remain in the list but show an explicit no-coordinate state and cannot be routed to until a real destination exists.

## Offline behavior

The dashboard stays usable when the API or browser network is unavailable:

- Citizen reports are written to browser `localStorage` and retried when connectivity returns.
- The field-officer briefing is cached; status updates and GPS samples queue locally and retry later.
- The dashboard displays `OFFLINE · LOCAL`, stale, or last-known state rather than inventing delivery, positions, or directions.
- SQLite is the local command-node database. Offline saves are not confirmation that emergency services received a report.
- Admin Settings includes **Clear server demo data**. It requires the Admin development role and a confirmation, then clears persisted incidents, assignments, GPS samples, messages (if present), and audit/history records through `POST /api/v1/admin/demo-data/clear`. It does not delete citizen report queues, field-officer status/location queues, or cached briefings in browser `localStorage`; those remain device-local until the user clears that browser storage.

This is browser-local offline support, not background native sync, peer-to-peer mesh, or offline map tiles. Browser GPS requires explicit permission and an open page.

## Map configuration

Copy `.env.example` to `.env` and set `VITE_GOOGLE_MAPS_API_KEY` when using an authorized Google Maps Embed API key. The dashboard centers the map on real incident or GPS coordinates returned by the API and renders markers only for coordinates that exist. Missing coordinates show an explicit fallback state; no map position is invented. The incident location details provide a **Route to incident** link using the stored destination (and the coordinator's real GPS position when available) through an external Google Maps URL. Without a key it uses Google's public development embed URL. Production deployments must configure a restricted key, billing, attribution, and the applicable Google Maps terms.

## Implemented limits

The MVP does not include JWT authentication, PostgreSQL/PostGIS migrations, rate limiting, encrypted message envelopes, native Android, background tracking, offline map data, Wi-Fi Direct, or physical-device validation. Safe-location entries are a small demo catalog and must be verified on arrival. Configure secrets through `.env` using `.env.example`.
