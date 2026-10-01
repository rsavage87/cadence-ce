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
2. **Code that runs before a tenant is set never touches a tenant-scoped table**: loading the signed-in user
   (`backends.get_user`), signed-out pages, and management commands before `tenant_context()`. No `select_related("role")`
   there: under row-level security the row is hidden or the query fails, and SQLite tests cannot tell (`tests/test_rls_paths.py`
   stands in for the policy; add new signed-out paths to it).
3. **Never write `queryset = Model.objects.all()` at class level** (admin, DRF, forms): it is evaluated at import time
   with no tenant in context and stays empty. Resolve querysets inside the request (`get_queryset`).
4. **Permissions are server-side.** Web views use `@web_view(Module.X, Level.Y)` (apps/web/decorators.py, which wraps
   `require_level` with `login_required` and the no-tenant guard); API viewsets set `module` and rely on `ModulePermission`.
   Per-action levels live next to the services (`apps/workorders/permissions.py`, `apps/recalls/permissions.py`). Hiding a button
   is not access control.
5. **State changes go through services** (`apps/workorders/services.py`, `apps/pm/services.py`, `apps/contracts/services.py`,
   `apps/accounts/services.py`, `apps/credentials/services.py`, `apps/recalls/services.py`, `apps/facility/services.py`, `apps/equipment/services.py`), never by setting fields in a
   view or calling model helpers like
   `Contract.add_assets` directly. Services validate, write status history, and keep the asset in sync.
6. **No PHI by design.** The portal never asks for patient identifiers. Don't add free-text fields that invite them.
7. **Migrations are generated, never hand-edited**, and committed with the change. After adding a tenant-scoped model,
   run `manage.py enable_rls --database=migrate` in the deploy step (docker-compose already does).
8. **Tests for every slice:** a tenant-isolation test for each new model, and a service test for each rule.
   `pytest` must be green before a slice is done.

## Commands
```
python manage.py makemigrations && python manage.py migrate        # first run creates all migrations
python manage.py bootstrap_tenant --name "Riverside" --slug riverside --admin-email you@example.com
python manage.py bootstrap_tenant --name "Riverside" --slug riverside --admin-email you@example.com --invite   # email the director a set-password link
python manage.py seed_demo                                         # small fictional dataset, login kim@riverside.example / DemoPass-2026
python manage.py generate_pm                                       # PM work-order generation; the scheduler runs it daily
python manage.py import_assets --tenant riverside inventory.csv --dry-run
python manage.py import_openfda --days 30                         # FDA recall import; the scheduler runs it daily
python manage.py run_daily_jobs                                    # both daily jobs, each at most once per local day (cron-safe)
python manage.py scheduler                                         # long-running: runs them at SCHEDULER_DAILY_AT (docker-compose `scheduler`)
python manage.py enable_rls --database=migrate                     # Postgres only, run after migrate
pytest
```

## Conventions
- Dates for business events are `DateField`s (`opened_on`, `due_on`, `completed_on`); timestamps only for audit.
- Money is `DecimalField(12, 2)`. KPI math converts to float at the edges, not in models.
- Choices are `TextChoices` with stable slugs (`in_service`, not "In service"). Display labels can change; slugs cannot.
- Sequences (`WO-26-0042`, `SR-00017`) come from `apps.core.models.Sequence.next`, never from `max(id)+1`.
- Keep views thin: parse input, call a service, render. Reports live in `apps/reports/services.py`.
- Every CSV goes through `apps.web.exports.csv_response` (streams, UTF-8 with BOM, formula-looking text kept as text). Printable pages
  extend `web/print_base.html` (light, no shell, new tab; the browser's print dialog makes the PDF). Links that must carry a list's
  filters use `data-act="with-filters" data-base="<url>"` (cadence.js reads the address at click time).
- Ruff (`ruff.toml`: E, F, W, I; line length 160; migrations excluded). CI runs `ruff check .` and `pytest` on every push.

## Where things are
- `apps/tenants` tenant model, context var, middleware, `enable_rls`; `tenant_context()` sets both the ORM scope and the Postgres
  `app.tenant_id` that RLS reads, so the public portal and management commands see the same rows under RLS
- `apps/core` TenantModel, TenantManager, Sequence, TenantModelAdmin
- `apps/accounts` User, Role, RolePermission, default roles, `require_level`; `services.py` for invites, role changes, (de)activation, role matrix edits;
  `invitations.py` (signed set-password links: `send_invitation`, `is_pending`; a resend replaces the link), `signin.py` (sign-in lockouts and
  password-reset requests, never revealing whether an address has an account), `backends.py` (username or email in any case; a deactivated
  facility cannot sign in), `emails.py` (the one place account emails are sent; a failure returns False, never raises). Links in emails start
  with `APP_BASE_URL`, never the request's Host
- `apps/equipment` Department, DeviceModel, Asset (tags carry no spaces or slashes: they are URL segments, and never change once a device is
  added), CSV importer; `services.py` fleet queries plus adding devices, models, and departments, editing, and status changes
  (`STATUS_CHANGES`; retiring cancels open PMs), `permissions.py` (Edit to add, edit, tag out; Approve to retire or reinstate)
- `apps/contracts` Contract with add/remove device operations and cost allocation; `services.py` for create/update/renew/delete, status, filters, KPI summary
- `apps/workorders` WorkOrder and lines, ServiceRequest, lifecycle services
- `apps/pm` PmProcedure, PM generation, on-time math, month helpers; `schedule.py` the PM schedule's read models (month calendar,
  a day's devices, suggested technicians, 30-day outlook, 7-day workload, PM library); `services.create_pm_work_orders_for_day`;
  `permissions.py` (View to see, Approve to create a day's work orders)
- `apps/recalls` Alert (global), AlertMatch (per tenant), matching, openFDA importer; `services.py` dispositions and recall work-order batches,
  `permissions.py` (review at Edit, close/reopen at Approve). ECRI import is deferred (license).
- `apps/credentials` Technician, Credential, qualification and coverage services, credential add/renew/sign-off/remove
- `apps/portal` public request form (`/r/<tenant-slug>/`)
- `apps/facility` Settings: `FacilitySettings` (one row per tenant: portal callback and hotline, the eight maintenance-policy texts,
  KPI targets and the monthly repair budget); `services.py` reads (`get_settings`, defaults until first saved), `update_settings`,
  `reset_policy`, `kpi_targets`, `compliance_targets`, `portal_url`, the integration list, risk bands; `permissions.py` (View to see,
  Edit to change). Named `facility` so it never reads like `django.conf.settings`.
- `apps/reports` overview KPIs, the Overview bundle (`overview_page`), attention list, nav counts, `cost_of_service`; the eight Reports
  (`REPORTS` catalog and `run_report` in `services.py`; the numbers in `cost.py`, `fleet.py`, `operations.py`, read-only, `today` passed in)
- `apps/web` HTMX UI: one views/urls/forms module per screen (`views.py` Overview, Equipment, Work orders; `views_contracts.py`;
  `views_users.py` Users and Roles tabs; `views_account.py` sign-in, password reset and change; `views_invite.py` accepting an invitation; `views_credentials.py`; `views_recalls.py`; `views_reports.py` with the CSV download; `views_settings.py`; `views_pm.py` with `pm_panels.py` for its lower panels; `views_exports.py` the list CSVs; `views_print.py` asset labels and the
  work-order print, with `qr.py`; `views_print_sheets.py` PM route sheets and report PDFs), templates,
  `charts.py` (SVG geometry: line, stacked bars, hbars with a benchmark marker, donut), `overview.py` and `reports_*.py` (chart geometry
  and display values for the Overview and the Reports; services never import them), `htmx.py` helpers, shell
  context processor. Drawers and modals are partials swapped into `#drawer` / `#modal-card`; the same URLs render a full page
  when opened directly. List wrappers that re-fetch themselves carry `hx-disinherit="hx-swap"` (a test enforces it).
- `apps/api` DRF viewsets under `/api/v1/`
- `apps/jobs` the daily jobs (`services.DAILY_JOBS`, `run_daily_jobs`, `is_due`), `JobRun` (a system table, not tenant-scoped: one row per
  job per local day is the lock against double runs), and the `scheduler` / `run_daily_jobs` commands
- `apps/demo` seed data
