# Cadence CE

Multi-tenant CMMS for hospital Clinical Engineering: equipment inventory, work orders, preventive maintenance,
service contracts, recall matching, technician credentials, a public service request portal, and KPIs.

This is the starter codebase generated from the interactive mock in `spec/`. It contains the data model, tenancy,
permissions, lifecycle services, importer, PM engine, KPI math, REST API, admin, portal, and tests, plus the HTMX
web UI for Overview, Equipment, Work orders, Contracts, Recalls and alerts, Reports (eight survey-ready reports with CSV
download, and the facility's own custom reports), Users and access (Users, Roles, Technician credentials), and Settings
(portal options, maintenance policy, KPI targets, integrations, risk scoring), and the PM schedule (calendar, a day's
devices, create that day's PM work orders).
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
Open http://localhost:8000/ and sign in as `kim@riverside.example` / `DemoPass-2026`. Kim directs two demo facilities, Riverside
Regional and Riverside North Campus: the facility menu at the top switches between them, and "All facilities" puts them side by side.
Portal: http://localhost:8000/r/riverside/ · API: http://localhost:8000/api/v1/ · Admin: http://localhost:8000/admin/

## Run locally (no Docker)
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # point DATABASE_URL at your Postgres, or leave TESTING to use SQLite for tests only
python manage.py makemigrations && python manage.py migrate
python manage.py seed_demo && python manage.py runserver
pytest                                                              # on SQLite
CADENCE_TEST_DATABASE_URL=postgres://cadence:cadence@localhost:5432/cadence pytest   # on PostgreSQL, with the row-level security tests
```

## Daily jobs
Four jobs run once a day: `generate_pm` (PM work orders coming due within `PM_LEAD_DAYS`, 21 by default, for every tenant),
`import_openfda` (the last 30 days of FDA device recalls, matched to every tenant's inventory), `send_report_emails` (scheduled
reports), and `send_staff_notifications` (the daily digest and contract reminders). `docker compose up` starts a `scheduler` service
that runs them at `SCHEDULER_DAILY_AT` (default 02:30): each facility's PM work orders, report emails, and staff emails at that hour
on the facility's own clock (its time zone, in Settings), the openFDA import at that hour on the server's (`DJANGO_TIME_ZONE`). If
it starts after that time and a day's jobs have not run, it runs them at once.

Each job runs at most once per local day (per facility for the facility's jobs), recorded in Admin under Scheduled jobs with what it
printed. A second scheduler or a restart does not repeat a job, and one job or facility failing does not stop the others. Elsewhere,
run one `python manage.py scheduler` process, or call `python manage.py run_daily_jobs` from a platform cron or a Kubernetes CronJob
every 15 minutes (it runs only what is due, so facilities in different time zones each get their jobs at their own hour); it is safe
to call as often as you like. `--tenant` and `--date` run one facility, or a missed day. `run_daily_jobs --force` runs a finished job again today; it never starts a second copy of a job that is still
running. A run killed partway (the container stopped, the database dropped) is recorded as stopped, or, if the process died
outright, taken over by the scheduler six hours after it started.

## Time zones
Each facility works on its own clock: its time zone (Settings, Time zone; the director by default) decides its today (what is due or
overdue, report dates, CSV names), the times its screens and prints show, and when its daily jobs and emails run. A change takes
effect at once for the screens and from the next day for the jobs, and is recorded in the change log. A facility that has not
chosen one works in the server's `DJANGO_TIME_ZONE`, which is also the openFDA import's clock.

## Several facilities
A person who works at several facilities (a health system's hospitals, a CE department covering two campuses) signs in once with
one email and one password. They keep a separate account in each facility, with that facility's role, unit or company, API tokens,
notification choices, and history, so each facility manages its own people exactly as before. Inviting someone whose address
already has an account at another facility on this server adds that facility to their account. They get an email saying so, and
join from the link or from the facility menu at the top of any page (on a phone, in the account menu). Nothing tells the inviting
facility that the address was already in use. `bootstrap_tenant` with a director's existing address does the same.
- Sign-in opens the facility the person used last. Switching facility signs the browser in to the person's account there (a browser
  works in one facility at a time: a tab left showing another facility goes to the same screen in the new one when next used, never
  to a record with the same number). Being deactivated in one facility
  leaves the others as they were.
- The password is the person's: a change or reset applies in every facility, and a reset request sends one link.
- Staff emails link with the facility's name in the address (`?facility=<slug>`), since record numbers repeat across facilities: a
  link for another facility offers to switch there, never the record with the same number here.
- All facilities (the facility menu, `/overview/all/`) shows the Overview's figures for each facility the person has joined, each
  on its own today and month and read with the person's access there, with totals (rates worked out from the summed parts, a
  recall shared by several facilities counted once).
- An API token is one account's, so it reads one facility; `/api/v1/facilities/` and `/api/v1/overview/all-facilities/` answer only
  a signed-in session. `drf_create_token` takes the account's username, which for a second facility's account is `<email>@<slug>`.

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
own address and stops if they lose access to Reports. The daily jobs send them (`send_report_emails`, at `SCHEDULER_DAILY_AT`);
a Monday that failed or was missed is caught up by the next day's run, and no report is sent twice for one Monday.

In Settings, the request portal can confirm requests by email: requesters give an optional work email and get a confirmation,
then a notice when the work is done. Only addresses at the facility's listed work email domains are accepted, so the public
form cannot send mail anywhere else, at most 3 an hour to one address and 60 an hour from one facility's portal, and the
emails never repeat anything the requester typed.

## Adding and changing devices
Add device on the Equipment screen adds a device, with a new model or department in the same form when the catalog lacks
them; its first PM is one interval after its last PM or install date (or today, if that has passed with no PM on record).
The device drawer edits its details (not the tag, which is on the label and in links), tags it out of service and returns it,
lends it out, marks it missing or found, and retires or reinstates it. Retiring cancels its open PM work orders and is refused
while repair or recall work is open; it needs Equipment Approve (the director by default). A completed repair returns a device
to service only when that repair tagged it out. The drawer's PM schedule tab shows
the device's maintenance strategy, procedure, next PMs, and PM history, and its Costs tab the service cost by year.

## Onboarding imports
A facility moving off another CMMS brings its records over from Settings, Import data (`/settings/import/`), one CSV file at a
time, in this order: devices, service contracts (one row per contract, or one per covered device with the contract repeated),
technicians (current and former), then work order history and open work (one row per work order, with its hours and costs). Each
kind needs the level of the screen that does the same thing: Equipment Edit, Contracts Edit, Users Edit, Work orders Approve.
- Upload the file (a template for each kind is on the page), confirm which of its columns feeds each value (Cadence guesses by
  name and shows sample values), and read the check: what the import will add, update, leave alone, and skip, why, and totals to
  compare with the old system. Nothing is saved until you press Import.
- A value it cannot read is reported, never quietly defaulted (an unreadable date stays blank with a note); a row it cannot take is
  skipped with its reason; records already here are found by their key (tag, contract reference, name, previous work order number),
  so a corrected file can be imported again without doubling anything.
- Files of up to 5 MB and 20,000 rows run a few hundred rows at a time; an import that stops (a closed tab) continues where it was.
  A file's rows are kept only until it is imported or discarded (or 14 days). For larger loads, `python manage.py import_data
  --tenant <slug> --kind <devices|contracts|technicians|work_orders> file.csv [--dry-run]` runs the same check and import with no limit.
- No free text comes over: imported work orders read "Imported from the previous system (work order <number>)", keep their old
  number (searchable, shown in the drawer), and count in the reports by their own dates and costs. Cancelled work and open PMs are
  not imported (each device's next PM date schedules its PMs).
- A proxy in front of the app must accept request bodies of 6 MB (nginx: `client_max_body_size 6m`); larger uploads are refused.

## The PM program
Each row of the PM library (PM schedule) opens that device model's PM program; so does a device's model name in its drawer. It
shows the model's details, its risk score, the interval it is maintained on, its PM procedure, and its devices. Add model and
Edit details need Equipment Edit. Scoring a model with the Settings rubric needs Equipment Approve (the director by default):
the score's band sets the risk class, and the score is reviewed yearly. Technicians write and revise PM procedures and their
checklists, and choose each model's (PM Edit).

A longer maintenance interval than the manufacturer's (AEM) is proposed with the model's three-year failure history, which the
screen gathers from the facility's own records. The Equipment Management Committee then approves or rejects it, recorded by a
PM Approve holder who did not propose it (the CE manager by default). Life-support models never go on AEM, and a model scored
into life support leaves it. Nor do imaging, radiologic, or medical laser models, which CMS keeps on the manufacturer's schedule:
an Equipment Approve holder marks them when adding the model or in its Edit details (or with the asset importer's "OEM schedule
required" column, for models it adds), and marking one ends its AEM and brings its PMs in. Ending an AEM, or approving a
shorter interval, brings devices' next PMs in. Auto-assign week puts every PM due in the next 7 days on a credentialed
technician's plate, as the schedule's plan suggests; it needs PM Approve and the right to assign work orders.

## Recording the work
A work order's drawer records the time spent (Log time: who, when, hours, at the Settings labor rate) and the parts used (Add part),
and its Cost section adds them up; reports and the Overview use the same figures. Mark completed asks what was done; on a PM it
records each checklist step as pass, fail, or not applicable, with the readings the procedure asks for, and the overall result.
A failed PM opens a repair work order and keeps the device out of service until that repair is done. The work-order print and the
device's PM history show the recorded results. Recalls' Check FDA feed fetches new FDA recall notices on demand (at most every 15
minutes) and matches them to your inventory.

## Custom reports
Reports' "+ Custom report" (Reports Edit: the director and Finance and quality by default) builds a report from work orders, devices,
labor, or parts: pick the columns, filter by type, status, department, category, technician, or a date range (a rolling period such
as the last 90 days, or fixed dates), and optionally group by a column or a month to get counts, totals, and averages. Preview shows
the first rows before saving. Saved reports are listed under the eight standard ones for everyone with Reports View, and download,
print, and email on a schedule like them. A report listing work orders also needs Work orders View, one listing devices Equipment
View. Only the columns the builder offers can be asked for, and it never offers what a requester typed (the problem, notes, who
asked, where), because reports are emailed.

## Scan tag
Scan tag on Equipment opens a device from its label: a handheld barcode scanner types into the field, a camera reads the code where
the browser can (Chrome and Edge on Android and desktop; on an iPhone, the camera app opens the label's link), or type the tag.
It reads a label's QR code link, a device link, or a plain tag, and opens only devices the user may see. Someone signed in to
Cadence who opens a label's link on their phone gets an "Open in Cadence" link on the request page.

## History and the change log
Every device, work order (with its labor and parts), contract, and device model has a History tab or section: each change, newest
first, with when, who, each field's before and after, and the reason the product recorded. Users and access has a Change log tab
across the facility (devices, models, work orders, contracts, PM procedures and AEM decisions, credentials, roles, settings, custom
reports, and every access change: invitations, role changes, deactivations, a role's levels and what it sees), filtered by area,
dates, and person, downloadable as CSV and printable; the API serves it at `/api/v1/change-log/`. Each person sees only the areas
their role can view, and vendor technicians and clinical requesters see no history at all.

## Notifications
The account menu's Notifications page chooses the emails Cadence sends you: when a work order is assigned to you (on by default;
a batch, such as a day's PMs or Auto-assign week, sends one email listing them all), a morning digest of your work due or overdue
and your PMs this week (off by default), and reminders 90, 30, and 7 days before a service contract ends and once after (for people
who can edit contracts). The digest and reminders go out with the daily jobs; nothing is ever sent twice. These emails never carry
what a requester typed (the problem, who asked, where): open the work order in Cadence to read it.

## API
`/api/v1/` serves what the screens do, with the same permissions, refusals, and services behind them, by the web session or a
token (`Authorization: Token <key>`; create one with `python manage.py drf_create_token <username>`). Every endpoint is documented in
its module's docstring under `apps/api/`, and the browsable API (open `/api/v1/` signed in) lists them:
- devices, device models, departments, work orders (`views.py`), and a work order's labor, parts, and notes (`views_work.py`);
- contracts and the devices they cover (`views_contracts.py`);
- the PM schedule, Auto-assign week, PM procedures, a model's procedure, risk score, and AEM cases and decisions (`views_pm.py`);
- the Overview, the eight reports, custom reports (build and run), and your own report emails (`views_reports.py`);
- recall alert matches, recall work orders, and Check FDA feed (`views_recalls.py`); Scan (`views_scan.py`);
- users, roles, technicians, and credentials (`views_users.py`); Settings;
- the signed-in person's facilities and All facilities (`views_facilities.py`, `views_all_facilities.py`; session only).

Vendor technicians and clinical requesters reach only their own devices and work orders (and Scan), as on the screens. Nobody gives
a role more access than their own role has, on the Users tab or the API, and a facility always keeps a director who can sign in.
Deactivating a user deletes their API tokens.

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
- The API takes the web session or a token (`Authorization: Token <key>`; `manage.py drf_create_token <username>`). The tenant
  middleware runs before DRF checks a token, so every API view sets the tenant itself (ORM scope and `app.tenant_id`) once DRF has
  authenticated, from the token's user, and restores it after the response. A token whose user or facility is deactivated is refused,
  as signing in is.
- Some roles see only part of a facility: a vendor technician only the work orders assigned to their company (and those devices),
  a clinical requester only their own unit's devices and work orders. Every screen, export, print, and API endpoint refuses them
  unless it narrows what it shows to that share; a custom role can be given either scope on the Roles tab.
- A person in several facilities has one account in each (`User.person` links them): no account ever reads another facility's
  rows. Moving between them signs in to the other account; All facilities reads each facility inside it, with that account's
  access. Only an invitation links accounts, on the exact address another facility's account stores.
- django-simple-history records every change to assets, work orders, contracts, credentials, and roles.
- The portal asks for no patient information and is rate-limited per IP.
- Invitation and password-reset links are signed, single-use, and expire; the reset request never says whether an address has an account.

## First things to do in Claude Code
1. `pip install -r requirements-dev.txt`, then `pytest`.
2. Pick up a deferred item from `spec/BUILD_PLAN.md` (exports and printing, connectors, the ECRI importer).
