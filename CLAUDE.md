# Cadence CE — working agreement for Claude Code

Cadence CE is a multi-tenant CMMS for hospital Clinical Engineering departments, sold as a standalone product.
The interactive mock in `spec/cadence-ce-cmms-mock.html` is the product spec: screens, fields, KPI math, and
workflows come from there. `spec/BUILD_PLAN.md` maps each screen to code and lists the build order.

## Stack
Python 3.12, Django 5, Django REST Framework, PostgreSQL 16 (row-level security), django-simple-history for audit,
HTMX for the web UI (`apps/web`, via django-htmx), pytest. Tests run on SQLite automatically (`TESTING` in settings), and on
PostgreSQL 16 when `CADENCE_TEST_DATABASE_URL` points at a server (CI runs both; the Postgres run includes the real row-level
security tests, `tests/test_postgres_rls.py`).

## Non-negotiables
1. **Every business table inherits `apps.core.models.TenantModel`.** Query through `Model.objects` (tenant-scoped).
   `Model.unscoped` is only for tenant bootstrap, cross-tenant jobs, and tests, and each use gets a comment saying why.
2. **Code that runs before a tenant is set never touches a tenant-scoped table**: loading the signed-in user
   (`backends.get_user`), signed-out pages, and management commands before `tenant_context()`. No `select_related("role")`
   there: under row-level security the row is hidden or the query fails, and SQLite tests cannot tell (`tests/test_rls_paths.py`
   stands in for the policy on SQLite, `tests/test_postgres_rls.py` runs the real policies as the runtime role on PostgreSQL; add
   new signed-out paths and commands to both).
3. **Never write `queryset = Model.objects.all()` at class level** (admin, DRF, forms): it is evaluated at import time
   with no tenant in context and stays empty. Resolve querysets inside the request (`get_queryset`).
4. **Permissions are server-side.** Web views use `@web_view(Module.X, Level.Y)` (apps/web/decorators.py, which wraps
   `require_level` with `login_required` and the no-tenant guard); API viewsets set `module` and rely on `ModulePermission`.
   Per-action levels live next to the services (`apps/workorders/permissions.py`, `apps/recalls/permissions.py`). Hiding a button
   is not access control. Some roles see only their share of a facility (`apps/workorders/scoping.py`: a vendor technician their
   company's work orders, a clinical requester their unit's); every web view and API action refuses them unless it opts in
   (`web_view(..., scoped=True)`, a viewset's `scoped_actions`) and narrows what it shows through `scoping.work_orders` / `assets`.
5. **State changes go through services** (`apps/workorders/services.py`, `apps/pm/services.py`, `apps/contracts/services.py`,
   `apps/accounts/services.py`, `apps/credentials/services.py`, `apps/recalls/services.py`, `apps/facility/services.py`, `apps/equipment/services.py`,
   `apps/pm/aem.py`, `apps/pm/procedures.py`, `apps/workorders/costs.py`, `apps/workorders/completion.py`), never by setting fields in a
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
CADENCE_TEST_DATABASE_URL=postgres://cadence:cadence@localhost:5432/cadence pytest   # the suite on PostgreSQL as a superuser, with RLS tests
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
- `apps/accounts` User, Role, RolePermission, default roles, `require_level`; `DataScope`, `Role.scope` (blank: the default by slug, vendor
  company and requester department) and `User.company`; `services.py` for invites, role changes, (de)activation, role matrix edits, a
  user's company or department (`set_user_scope`, `set_user_access`) and a custom role's scope;
  `invitations.py` (signed set-password links: `send_invitation`, `is_pending`; a resend replaces the link), `signin.py` (sign-in lockouts and
  password-reset requests, never revealing whether an address has an account), `backends.py` (username or email in any case; a deactivated
  facility cannot sign in), `emails.py` (the one place account emails are sent; a failure returns False, never raises). Links in emails start
  with `APP_BASE_URL`, never the request's Host
- `apps/equipment` Department, DeviceModel, Asset (tags carry no spaces or slashes: they are URL segments, and never change once a device is
  added), CSV importer; `services.py` fleet queries plus adding devices, models, and departments, editing (`update_asset`,
  `update_device_model`, `rename_department`: names unique in any letter case), and status changes
  (`STATUS_CHANGES`; retiring cancels open PMs), risk scoring (`set_risk_score`, `clear_risk_score`, `RISK_SCORE_BANDS`; every model
  change goes through `_save_model`, which locks the row and tells `apps.pm.aem.model_changed`), `permissions.py` (Edit to add, edit,
  tag out, and add or edit models; Approve to retire or reinstate, and to score a model or change its risk class)
- `apps/contracts` Contract with add/remove device operations and cost allocation; `services.py` for create/update/renew/delete, status, filters, KPI summary
- `apps/workorders` WorkOrder and lines, ServiceRequest, lifecycle services; `scoping.py` who sees which devices and work orders inside a
  facility (scope_of, work_orders, assets, can_see_*, and shown_text, which masks other work orders' numbers); `costs.py` the only writer of labor and part lines (rates from
  Settings, a different rate at Approve, nothing on a closed or cancelled work order); `completion.py` completing with the resolution
  and a PM's step results (`complete_work_order`: the screen's Mark completed and the API's transition to completed both use it; a
  failed PM opens or names its repair and holds the device out until that repair is done); a line's cost is hours × rate or
  quantity × unit cost to the cent, half up (`LABOR_AMOUNT`, `PART_AMOUNT`, `line_cents` in `models.py`), wherever lines are added up;
  `permissions.py` (Edit to record labor, parts, and completion; Approve to assign, close, or charge a different rate)
- `apps/pm` PmProcedure, PM generation, on-time math, month helpers; `schedule.py` the PM schedule's read models (month calendar,
  a day's devices, suggested technicians, `planned_technicians` (who does each device due on a day: the day panel, route sheets, and
  the device drawer's PM tab share it), 30-day outlook, 7-day workload, PM library); `services.create_pm_work_orders_for_day` and
  `assign_week` / `week_assignment_preview` (Auto-assign week; both take `lock_planner()` first); `aem.py` the AEM program
  (`AemDecision`: evidence, propose, approve, reject, withdraw, end, the pull-in of next PMs; the only writer of
  `DeviceModel.aem_interval_months`); `procedures.py` (write and revise procedures, the checklist line format, `set_model_procedure`);
  `permissions.py` (View to see; Edit for procedures and AEM proposals; Approve to create a day's work orders and to decide or end
  AEM; Auto-assign week also needs work-order assign)
- `apps/recalls` Alert (global), AlertMatch (per tenant), matching, openFDA importer; `services.py` dispositions and recall work-order batches,
  `permissions.py` (review at Edit, close/reopen at Approve); `feeds.py` the openFDA fetch shared by `import_openfda` and the Recalls
  screen's Check FDA feed (one fetch per 15 minutes for everyone, kept in apps.jobs' `JobRun`; a check matches only its own facility).
  ECRI import is deferred (license).
- `apps/credentials` Technician, Credential, qualification and coverage services, credential add/renew/sign-off/remove
- `apps/portal` public request form (`/r/<tenant-slug>/`); `notifications.py` the requester's confirmation and done emails (only at the
  facility's work email domains, never the problem text)
- `apps/facility` Settings: `FacilitySettings` (one row per tenant: portal callback and hotline, the eight maintenance-policy texts,
  KPI targets and the monthly repair budget); `services.py` reads (`get_settings`, defaults until first saved), `update_settings`,
  `reset_policy`, `kpi_targets`, `compliance_targets`, `portal_url`, the integration list, risk bands; `permissions.py` (View to see,
  Edit to change). Named `facility` so it never reads like `django.conf.settings`.
- `apps/reports` report emails (`ReportSubscription`, `subscriptions.py`, the daily `send_report_emails`; self-service only), overview KPIs, the Overview bundle (`overview_page`), attention list, nav counts, `cost_of_service`; the eight Reports
  (`REPORTS` catalog and `run_report` in `services.py`; the numbers in `cost.py`, `fleet.py`, `operations.py`, read-only, `today` passed in)
- `apps/web` HTMX UI: one views/urls/forms module per screen (`views.py` Overview, Equipment, Work orders; `views_contracts.py`;
  `views_users.py` Users and Roles tabs; `views_account.py` sign-in, password reset and change; `views_invite.py` accepting an invitation; `views_credentials.py`; `views_recalls.py`; `views_reports.py` with the CSV download; `views_settings.py`; `views_pm.py` with `pm_panels.py` for its lower panels; `views_pm_week.py` Auto-assign week; `views_wo_costs.py` and `views_wo_complete.py` the work order drawer's labor and parts and its Mark completed; `views_models.py` the device model drawer (PM program tab, Add model, Edit details, risk score) with `views_procedures.py` and `views_aem.py` for its Procedure and AEM tabs; `views_exports.py` the list CSVs; `views_print.py` asset labels and the
  work-order print, with `qr.py`; `views_print_sheets.py` PM route sheets and report PDFs), templates,
  `charts.py` (SVG geometry: line, stacked bars, hbars with a benchmark marker, donut), `overview.py` and `reports_*.py` (chart geometry
  and display values for the Overview and the Reports; services never import them), `htmx.py` helpers, shell
  context processor. Drawers and modals are partials swapped into `#drawer` / `#modal-card`; the same URLs render a full page
  when opened directly. List wrappers that re-fetch themselves carry `hx-disinherit="hx-swap"` (a test enforces it).
- `apps/api` DRF viewsets under `/api/v1/`
- `apps/jobs` the daily jobs (`services.DAILY_JOBS`, `run_daily_jobs`, `is_due`), `JobRun` (a system table, not tenant-scoped: one row per
  job per local day is the lock against double runs), and the `scheduler` / `run_daily_jobs` commands
- `apps/demo` seed data
