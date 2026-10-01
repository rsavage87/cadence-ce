# Cadence CE

Multi-tenant CMMS for hospital Clinical Engineering: equipment inventory, work orders, preventive maintenance,
service contracts, recall matching, technician credentials, a public service request portal, and KPIs.

This is the starter codebase generated from the interactive mock in `spec/`. It contains the data model, tenancy,
permissions, lifecycle services, importer, PM engine, KPI math, REST API, admin, portal, and tests, plus the HTMX
web UI for Overview, Equipment, Work orders, Contracts, Recalls and alerts, Reports (eight survey-ready reports with CSV
download), Users and access (Users, Roles, Technician credentials), and Settings (portal options, maintenance policy, KPI
targets, integrations, risk scoring), and the PM schedule (calendar, a day's devices, create that day's PM work orders).
Every screen in the mock is built; `spec/BUILD_PLAN.md` lists what each slice deferred.

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

## Daily jobs
Two jobs run once a day: `generate_pm` (PM work orders coming due within `PM_LEAD_DAYS`, 21 by default, for every tenant) and
`import_openfda` (the last 30 days of FDA device recalls, matched to every tenant's inventory). `docker compose up` starts a
`scheduler` service that runs both at `SCHEDULER_DAILY_AT` (default 02:30, local time in `DJANGO_TIME_ZONE`). If it starts after
that time and today's jobs have not run, it runs them at once.

Each job runs at most once per local day, recorded in Admin under Scheduled jobs with what it printed. A second scheduler or a
restart does not repeat a job, and one job failing does not stop the other. Elsewhere, run one `python manage.py scheduler`
process, or call `python manage.py run_daily_jobs` from a platform cron or a Kubernetes CronJob; it is safe to call more than
once a day. `run_daily_jobs --force` runs a finished job again today; it never starts a second copy of a job that is still
running. A run killed partway (the container stopped, the database dropped) is recorded as stopped, or, if the process died
outright, taken over by the scheduler six hours after it started.

## Email and sign-in
Inviting a user from Users and access emails them a link to set their password (valid for 7 days; Resend invite replaces
it). "Forgot your password?" on the sign-in page emails a reset link (valid for 2 hours), or a fresh invitation to someone
who never set a password. Signed-in users change their password from the account menu. With `DJANGO_DEBUG=1` the emails
are printed to the server's console; for real delivery set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` (or `EMAIL_USE_SSL` for port 465), and `DEFAULT_FROM_EMAIL`, and set `APP_BASE_URL` to the address people use to reach
the app (links in emails start with it). Check delivery with `python manage.py sendtestemail you@example.com`. A failed send
never undoes the invitation: the account stays Invited and the toast says so.

People sign in with their email or username in any letter case. Ten failed sign-ins for one login within 15 minutes lock
that login for the rest of the window (the lock is on the typed login, so it says nothing about whether the account exists).
`bootstrap_tenant --invite` emails the first director an invitation instead of printing a password. The lockout and
rate-limit counters live in each web process's memory, so with several gunicorn workers each counts on its own and a
restart clears them; a shared cache (Redis) would make them exact.

## Report emails and request confirmations
Anyone who can view Reports can have a report emailed to themselves every Monday or on the first Monday of each month
(Schedule, on the report). The email carries the report's CSV and a link to its printable page; it goes only to the user's
own address and stops if they lose access to Reports. The daily jobs send them (`send_report_emails`, at `SCHEDULER_DAILY_AT`).

In Settings, the request portal can confirm requests by email: requesters give an optional work email and get a confirmation,
then a notice when the work is done. Only addresses at the facility's listed work email domains are accepted, so the public
form cannot send mail anywhere else, and the emails never repeat what the requester typed about the problem.

## Adding and changing devices
Add device on the Equipment screen adds a device, with a new model or department in the same form when the catalog lacks
them; its first PM is one interval after its last PM or install date (or today, if that has passed with no PM on record).
The device drawer edits its details (not the tag, which is on the label and in links), tags it out of service and returns it,
lends it out, marks it missing or found, and retires or reinstates it. Retiring cancels its open PM work orders and is refused
while repair or recall work is open; it needs Equipment Approve (the director by default). A completed repair returns a device
to service only when that repair tagged it out. The drawer's PM schedule tab shows
the device's maintenance strategy, procedure, next PMs, and PM history, and its Costs tab the service cost by year.

## Exports and printing
Equipment, Work orders, and Contracts download as CSV with the filters on screen (Export), and a contract's covered devices
from its drawer (Device list); every report downloads as CSV too. The files open cleanly in Excel (UTF-8, ISO dates, plain
numbers), and text that a spreadsheet would run as a formula is kept as text. Printable pages open in a new tab and print, or
save as PDF from the browser's print dialog: a work order with its PM checklist (Print in its drawer), asset labels with a QR
code that opens the device's request form (Label in the device drawer, or Labels on Equipment for the filtered list, on a
thermal label printer or US Letter label sheets), each technician's PM route for a day or the week (Route sheets), every
report (PDF), and the recall response log (Recalls). The Overview prints as it is (Export).

## Security model
- One tenant per hospital. Every row carries `tenant_id`; the ORM scopes queries through `TenantManager`, and
  PostgreSQL row-level security enforces the same rule at the database (`manage.py enable_rls`) when the app
  connects as the non-owner role `cadence_app`.
- Roles map modules to levels (None/View/Request/Edit/Approve/Full); checks are server-side.
- django-simple-history records every change to assets, work orders, contracts, credentials, and roles.
- The portal asks for no patient information and is rate-limited per IP.
- Invitation and password-reset links are signed, single-use, and expire; the reset request never says whether an address has an account.

## First things to do in Claude Code
1. `pip install -r requirements-dev.txt`, then `pytest`.
2. Pick up a deferred item from `spec/BUILD_PLAN.md` (exports and printing, connectors, the ECRI importer).
