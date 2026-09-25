# Cadence CE

Multi-tenant CMMS for hospital Clinical Engineering: equipment inventory, work orders, preventive maintenance,
service contracts, recall matching, technician credentials, a public service request portal, and KPIs.

This is the starter codebase generated from the interactive mock in `spec/`. It contains the data model, tenancy,
permissions, lifecycle services, importer, PM engine, KPI math, REST API, admin, portal, and tests, plus the HTMX
web UI for Overview, Equipment, and Work orders. The remaining screens land slice by slice (see `spec/BUILD_PLAN.md`).

## Run locally (Docker)
```
cp .env.example .env
docker compose up --build
# in another terminal:
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate --database=migrate
docker compose exec web python manage.py enable_rls --database=migrate
docker compose exec web python manage.py seed_demo
```
Open http://localhost:8000/ and sign in as `kim@riverside.example` / `DemoPass-2026`.
Portal: http://localhost:8000/r/riverside/ · API: http://localhost:8000/api/v1/ · Admin: http://localhost:8000/admin/

## Run locally (no Docker)
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # point DATABASE_URL at your Postgres, or leave TESTING to use SQLite for tests only
python manage.py makemigrations && python manage.py migrate
python manage.py seed_demo && python manage.py runserver
pytest
```

## Security model
- One tenant per hospital. Every row carries `tenant_id`; the ORM scopes queries through `TenantManager`, and
  PostgreSQL row-level security enforces the same rule at the database (`manage.py enable_rls`) when the app
  connects as the non-owner role `cadence_app`.
- Roles map modules to levels (None/View/Request/Edit/Approve/Full); checks are server-side.
- django-simple-history records every change to assets, work orders, contracts, credentials, and roles.
- The portal asks for no patient information and is rate-limited per IP.

## First things to do in Claude Code
1. `pip install -r requirements-dev.txt`, then `pytest`.
2. Start slice 5 (`spec/BUILD_PLAN.md`): Contracts and Users and access → Credentials, as new screens in `apps/web`.
