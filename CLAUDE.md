# Cadence CE — working agreement for Claude Code

Cadence CE is a multi-tenant CMMS for hospital Clinical Engineering departments, sold as a standalone product.
The interactive mock in `spec/cadence-ce-cmms-mock.html` is the product spec: screens, fields, KPI math, and
workflows come from there. `spec/BUILD_PLAN.md` maps each screen to code and lists the build order.

## Stack
Python 3.12, Django 5, Django REST Framework, PostgreSQL 16 (row-level security), django-simple-history for audit,
HTMX for the web UI (`apps/web`, via django-htmx), pytest. Tests run on SQLite automatically (`TESTING` in settings).

## Non-negotiables
1. **Every business table inherits `apps.core.models.TenantModel`.** Query through `Model.objects` (tenant-scoped).
   `Model.unscoped` is only for tenant bootstrap, cross-tenant jobs, and tests, and each use gets a comment saying why.
2. **Never write `queryset = Model.objects.all()` at class level** (admin, DRF, forms): it is evaluated at import time
   with no tenant in context and stays empty. Resolve querysets inside the request (`get_queryset`).
3. **Permissions are server-side.** Views use `@require_level(module, Level.X)`; API viewsets set `module` and rely on
   `ModulePermission`. Hiding a button is not access control.
4. **State changes go through services** (`apps/workorders/services.py`, `apps/pm/services.py`, `Contract.add_assets`),
   never by setting status fields in a view. Services write status history and keep the asset in sync.
5. **No PHI by design.** The portal never asks for patient identifiers. Don't add free-text fields that invite them.
6. **Migrations are generated, never hand-edited**, and committed with the change. After adding a tenant-scoped model,
   run `manage.py enable_rls --database=migrate` in the deploy step (docker-compose already does).
7. **Tests for every slice:** a tenant-isolation test for each new model, and a service test for each rule.
   `pytest` must be green before a slice is done.

## Commands
```
python manage.py makemigrations && python manage.py migrate        # first run creates all migrations
python manage.py bootstrap_tenant --name "Riverside" --slug riverside --admin-email you@example.com
python manage.py seed_demo                                         # small fictional dataset, login kim@riverside.example / DemoPass-2026
python manage.py generate_pm                                       # nightly PM work-order generation (cron / scheduler)
python manage.py import_assets --tenant riverside inventory.csv --dry-run
python manage.py import_openfda --days 30
python manage.py enable_rls --database=migrate                     # Postgres only, run after migrate
pytest
```

## Conventions
- Dates for business events are `DateField`s (`opened_on`, `due_on`, `completed_on`); timestamps only for audit.
- Money is `DecimalField(12, 2)`. KPI math converts to float at the edges, not in models.
- Choices are `TextChoices` with stable slugs (`in_service`, not "In service"). Display labels can change; slugs cannot.
- Sequences (`WO-26-0042`, `SR-00017`) come from `apps.core.models.Sequence.next`, never from `max(id)+1`.
- Keep views thin: parse input, call a service, render. Reports live in `apps/reports/services.py`.
- Ruff (`ruff.toml`: E, F, W, I; line length 160; migrations excluded). CI runs `ruff check .` and `pytest` on every push.

## Where things are
- `apps/tenants` tenant model, context var, middleware, `enable_rls`
- `apps/core` TenantModel, TenantManager, Sequence, TenantModelAdmin
- `apps/accounts` User, Role, RolePermission, default roles, `require_level`
- `apps/equipment` Department, DeviceModel, Asset, CSV importer
- `apps/contracts` Contract with add/remove device operations and cost allocation
- `apps/workorders` WorkOrder and lines, ServiceRequest, lifecycle services
- `apps/pm` PmProcedure, PM generation, on-time math, month helpers
- `apps/recalls` Alert (global), AlertMatch (per tenant), matching, openFDA importer
- `apps/credentials` Technician, Credential, qualification and coverage services
- `apps/portal` public request form (`/r/<tenant-slug>/`)
- `apps/reports` overview KPIs, the Overview bundle (`overview_page`), attention list, nav counts
- `apps/web` HTMX UI: views, templates, `charts.py` (SVG geometry), shell context processor. Drawers and the new work order
  modal are partials swapped into `#drawer` / `#modal-card`; the same URLs render a full page when opened directly
- `apps/api` DRF viewsets under `/api/v1/`
- `apps/demo` seed data
