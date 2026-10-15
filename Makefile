PYTHON ?= python3
test:
	$(PYTHON) -m pytest backend/tests -q
run-backend:
	$(PYTHON) -m uvicorn backend.app.main:app --reload --port 8000
install-dashboard:
	cd apps/dashboard && npm install
run-dashboard:
	cd apps/dashboard && npm run dev
build-dashboard:
	cd apps/dashboard && npm run build
