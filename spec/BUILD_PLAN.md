# Build plan

The mock (`cadence-ce-cmms-mock.html`) is the spec. Each slice below is shippable on its own and ends with green tests.
Slices 0 to 22 are built: every screen in the mock exists, people can be invited and sign in on their own, the lists
export and print, devices are added and changed in the product, reports and request confirmations go out by email, each
device model's PM program (risk score, procedure, AEM interval) is kept in the product, and the work itself (time, parts,
what was done, a PM's results) is recorded on the work order; vendors and clinical requesters see only their own share; facilities
build their own reports, and a label opens its device with Scan tag; the API covers what the screens do, by session or token; every record's changes and every access change can be read back;
staff get the emails they choose about their own work; each facility works on its own clock; and one person signs in once to every facility they work at. What each slice deferred is noted in its row and below.

| # | Slice | Mock screen(s) | Code | Status |
|---|-------|----------------|------|--------|
| 0 | Tenancy, auth, roles, audit | Users and access → Roles | `tenants`, `core`, `accounts` | done (needs `makemigrations`) |
| 1 | Catalog + assets + importer | Equipment table, device drawer (Overview tab) | `equipment`, `import_assets` | done (API + admin; no HTMX UI yet) |
| 2 | Work orders + portal | Work orders list/board, WO drawer, request portal | `workorders`, `portal` | done (services, API, portal HTML) |
| 3 | PM engine + KPIs | PM schedule, Overview KPIs | `pm`, `reports` | done (services, API, plain home page) |
| 4 | Web UI shell (HTMX) | Nav, topbar, Overview, Equipment, Work orders | new `apps/web` | done (device drawer: Overview + Work orders tabs) |
| 5 | Contracts UI + credentials UI | Contracts section, Users and access (Users, Roles, Credentials tabs) | `contracts`, `accounts`, `credentials` + `web` | done (invitation email added in slice 10) |
| 6 | Recalls UI + ECRI importer | Recalls and alerts | `recalls` + `web` | done (openFDA feed; ECRI importer deferred, it needs a license) |
| 7 | Reports | Reports (COSR, PM compliance, MTBF, replacement, spend, contract vs in-house, technician productivity, recall log) | `reports` + `web` | done (CSV download and JSON API; PDF came in slice 11, Schedule in 13, Custom report in 18) |
| 8 | Settings | Integrations, portal settings, editable policy, risk scoring | `facility` + `web` | done (connectors, paging, photo upload, and email or text confirmation deferred) |
| 9 | PM schedule UI | PM schedule (calendar, create work orders for a day) | `pm` + `web` | done (Route sheets came in slice 11 and Auto-assign week in slice 14; OEM library sync deferred) |
| 10 | Sign-in and invitations | Users and access (Invite user, Resend invite), sign-in page | `accounts` + `web` | done (shared lockout counters across workers need Redis; email is sent in the request) |
| 11 | Exports and printing | Export (CSV) on Equipment, Work orders, Contracts, a contract's Device list; Label, Print, Route sheets, report PDF, Response log, Overview Export | `web` | done (PDFs come from the browser's print dialog; Add device, Schedule, Auto-assign week, Check feeds, Scan tag, and Custom report came later) |
| 12 | Device management | Equipment (Add device), device drawer (Edit details, Tag out of service, Return to service, PM schedule and Costs tabs) | `equipment` + `web` + `api` | done (the model catalog and AEM approval came in slice 14) |
| 13 | Email notifications | Reports (Schedule), Settings (portal confirmation to the requester), the request portal | `reports` + `facility` + `portal` + `web` | done (report emails are self-service; text messages and paging still need a provider) |
| 14 | The PM program | PM schedule (Auto-assign week, the PM library), a model's PM program (AEM, procedure, risk score) | `equipment` + `pm` + `web` | done (their API came in slice 19; OEM library sync needs a library) |
| 15 | Recording the work | Work order drawer (Cost, Mark completed), device PM history, Settings (labor rates), Recalls (Check feeds) | `workorders` + `recalls` + `web` + `api` | done (vendor accounts were limited to their company's work orders in slice 16) |
| 16 | Who sees what | Users and access (company and unit on users, scope on roles), every screen for the vendor technician and the clinical requester | `accounts` + `workorders` + `web` + `api` | done (a custom role chooses its scope; the default vendor and requester scopes are fixed) |
| 17 | The suite on PostgreSQL | (none: CI) | `tests` + CI | done (CI's test-postgres job runs every test on PostgreSQL 16 and the row-level security tests as the runtime role) |
| 18 | Custom reports, Scan tag, the CMS rule | Reports (Custom report), Equipment (Scan tag), a model's PM program (AEM) | `reports` + `equipment` + `pm` + `portal` + `web` | done (custom reports, Scan, and Check FDA feed reached the API in slice 19) |
| 19 | The API, complete | (none: `/api/v1/` for every screen's actions) | `api` + `accounts` | done (token sign-in resolves the facility; every API write goes through the services) |
| 20 | History and notifications | Every drawer (History), Users and access (Change log), the account menu (Notifications) | `core` + `accounts` + `notifications` + `web` + `api` | done (times read in the server's TIME_ZONE; a facility's own time zone is not used yet) |
| 21 | Facility time zones | Settings (Time zone); every screen's today, times, and prints | `tenants` + `jobs` + every app | done (the server's zone is a new facility's default and the FDA import's clock) |
| 22 | Several facilities | The top bar's facility menu (each facility, "All facilities"), the account menu, an invitation's join page | `accounts` + `reports` + `notifications` + `web` + `api` | done (one account per facility, linked per person; existing separate accounts are not linked, only invitations link) |

## KPI definitions (from the mock's `computeKpis`)
- **PM completion on time** for a month: PM work orders with `due_on` in the month and (already past due, or completed), of which `completed_on <= due_on`. Current month uses today as the period end.
- **Life-support PM completion**: same, filtered to `risk_class = life_support`.
- **Fleet uptime**: `100 - downtime_days / (active_devices * days_in_period) * 100`, downtime = turnaround days of repairs completed in the period.
- **Open work orders**: opened on or before the period end and not completed by then.
- **MTTR**: mean `completed_on - opened_on` of repairs completed in the period.
- **Repair spend**: labor lines + part lines of repairs completed in the period.
- **Cost of service ratio**: (work-order cost of the trailing 182 days × 365/182 + annual cost of active contracts) / acquisition value of active devices.
- **Recall alerts received**: alert matches whose alert was published in the period.

Operations: `apps/jobs` runs `generate_pm` and `import_openfda` once a day (the docker-compose `scheduler` service, at
`SCHEDULER_DAILY_AT`), each at most once per local day and recorded in Admin under Scheduled jobs.

The mock's export and print buttons work since slice 11, Add device since slice 12, and Scan tag and Custom report since slice 18.

## Screen → view map (slices 4 to 13)
- Overview: `reports.services.overview_kpis(year, month)` + `pm.services.pm_on_time_series` for the 12-month chart; attention list = life-support overdue PMs, alerts needing action, unassigned portal requests, expired/expiring contracts, critical open WOs, WOs awaiting parts > 7 days.
- Equipment: `Asset.objects.select_related(...)` with the same filters as the mock's toolbar (category, status, risk, department, support, overdue-only, bucket). Fleet buckets: retired / out of service / in repair / open recall / PM overdue / PM due ≤ 30 d / compliant, each device counted once in that order.
- Work orders: list and board; status buttons call `workorders.services.change_status`; assignment dropdown lists `credentials.services.qualified_technicians(asset)` first.
- PM schedule: `pm.schedule` over active devices' `next_pm_on` (the mock's pmByDay). A Sunday-first month calendar with a life-support
  and a high-risk dot and a count per day (red on past days: those devices are overdue), and the selected day's devices, most critical
  first, with hours from their PM procedure and the technician the schedule suggests: among technicians credentialed for the device,
  the least loaded (open work-order hours, plus what earlier devices in the plan were given), ties by name, where the mock hashed.
  For the next 7 days one week plan (`pm.schedule.week_plan`, day by day in date order) is the single source for the day panel, the
  create action, and the workload, so they always name the same technician. A device whose open PM work order is unassigned (the
  nightly `generate_pm` makes those) or held by a deactivated technician is still planned for the suggested technician; a vendor PM
  is nobody's. "Create N PM work orders" calls `pm.services.create_pm_work_orders_for_day`: one per device without an open PM work
  order, due on the PM date (today for a past day), assigned to the suggested technician only when the user may assign work
  orders. It needs PM Approve, as the API's nightly `generate` always has. The day list says who an open PM work order is with
  (a technician, the vendor, or unassigned). Below: the 30-day outlook by category (today through day 30), each technician's
  next-7-day load (today through day 6: PM hours as planned, plus other open work such as repairs and recalls) against weekly
  capacity, and the PM library (AEM only where approved, never for life support). The nav badge counts overdue devices; the
  Overview's PM tile links here only for PM viewers. Every web response varies on HX-Request and HX-Target, because pushed URLs
  answer with a fragment or a full page. Route sheets print since slice 11 and Auto-assign week works since slice 14; the
  mock's OEM library Sync only toasts and is deferred. `/api/v1/pm/calendar/`, `/api/v1/pm/day/`, `/api/v1/pm/create-for-day/`.
- Contracts: table from `contracts.services.filter_contracts` + `contracts_summary`; the drawer calls `add_asset` / `add_model` / `remove_asset` /
  `renew_contract` / `delete_contract` (`create_contract` / `update_contract` behind the forms); the device drawer's support editor posts to
  `/contracts/assets/<tag>/support/`.
- Recalls and alerts: one card per `AlertMatch` (alert × matched device model) from `recalls.services.filter_matches`; dispositions through
  `set_status` (Edit to review, Approve to close or reopen; `recalls/permissions.py`), `create_recall_work_orders` makes one recall work order
  per active device (`WorkOrder.alert` links them; `progress` drives the bar), `rematch` re-runs matching for new device models. Unmatched feed
  alerts are not listed per tenant (matching is automatic), so the mock's "No fleet match / Dismiss" cards have no equivalent, and "Close,
  no action needed" records `not_affected` (the model's distinct disposition) rather than the mock's single Closed state.
- Users and access: Users tab over `accounts.services` (`invite_user`, `set_user_role`, `deactivate_user` / `reactivate_user`); Roles matrix
  through `set_role_level` / `create_role`; Technician credentials through `credentials.services` (`add_credential`, `renew_credential`,
  `sign_off_credential`, `remove_credential`) with the coverage table from `coverage_by_category`.
- Reports: one page, the mock's eight reports listed on the left and the selected one on the right (`/reports/<key>/`; the list swaps
  `#rep-body`). `reports.services.REPORTS` is the catalog and `run_report(key, today)` dispatches to `reports/cost.py` (cost of service
  ratio by category, repair spend trend, contract vs in-house), `reports/fleet.py` (PM compliance summary, reliability by model,
  replacement planning), and `reports/operations.py` (technician productivity, recall response log); chart geometry is in
  `web/reports_*.py`. Every report returns `columns` and `rows`, which `/reports/<key>.csv` downloads and `/api/v1/reports/<key>/`
  serves as JSON. All reports are as of today and need Reports View. Contract cost is real (annual cost of contracts that have not
  ended, allocated to devices by acquisition cost) where the mock modeled 7% and 4% of acquisition value; the cost figures share
  `cost_of_service` with the Overview tile, and the Overview's uptime, MTTR, spend, and cost-of-service tiles link to their reports.
  Rules settled in review: the contract-vs-in-house rows group devices by their live contract (a device whose contract has ended counts
  as in-house and is called out); a category with no recorded acquisition value has no ratio (listed in the hint, blank in the CSV);
  PM compliance counts a device marked missing as non-compliant and only counts PMs on active devices; the reliability rate uses the
  mock's ×2 factor (two 182-day halves, the same base as its MTBF); a work order cannot start or complete before it was opened; asset
  tags cannot contain spaces or slashes (they live in URLs). The mock's PDF and Schedule buttons came in slices 11 and 13, its Custom
  report in slice 18.
- Settings: `facility.services` over one `FacilitySettings` row per tenant (unsaved defaults until the first save; every save is
  audited). The portal panel auto-saves the callback requirement and the hotline (the public form and its confirmation page read
  them); the department-links modal builds `/r/<slug>/?dept=` links. The eight maintenance-policy texts save together or reset to the
  mock's defaults; the assignment text heads the work-order drawer's Assignment section and the portal text ends the work-orders
  screen's unassigned-requests note. KPI targets (PM completion for medium and low risk, uptime, MTTR, monthly repair budget) drive
  the Overview tiles, the PM trend line, the compliance report (decided in exact arithmetic, so a target hit exactly is met), and the
  technician report's PM on-time colour; life support and high risk stay at 100%. Targets keep the column's precision (more decimals
  are refused, never rounded). Integrations show real state (only the openFDA import exists, and it counts as connected only once a
  real notice arrives: the demo's sample alerts do not; ECRI needs a license; the rest are not built), so the mock's Sync and Connect buttons, paging,
  photo upload (photos can capture patients), and email or text confirmation are shown as unavailable with a reason. Risk scoring shows
  the mock's rubric and bands with active device counts, each linking to Equipment's new "Active (not retired)" status filter;
  each model is scored in its drawer since slice 14 (the panel counts the models scored and the reviews due). View to see, Edit to change
  (the director by default; the manager sees it read-only). `/api/v1/settings/` (GET, PATCH) and `/api/v1/settings/reset-policy/`.
- Sign-in and invitations (slice 10): Invite user creates the Invited account (`accounts.services.invite_user`) and emails a link to
  set the first password (`accounts.invitations.send_invitation`): a signed token over the account's state and `invited_at`,
  valid for `INVITATION_VALID_DAYS` (7), used up by setting the password, replaced by Resend invite (the mock's button, on rows
  still pending), and killed for good by deactivating the account. A failed send changes nothing (an earlier link still works)
  and the modal stays open to say so. `/invite/<uid>/<token>/` sets the password and signs in. "Forgot your password?" emails a
  reset link valid for 2 hours, or a fresh invitation to an account that never set a password; the answer is the same for any
  address, and requests are limited per address and per caller. Signed-in users change their password at `/account/password/`
  (other sessions are signed out). Sign-in takes the username or email in any case; 10 failures for one typed login within 15
  minutes lock that login (in the backend, so Admin's sign-in is covered too), a reset lifts it, and accounts of an inactive
  facility cannot sign in. Links in emails start with `APP_BASE_URL`; signed-out pages send a same-origin Referer only (no-referrer
  would blank the Origin header and fail CSRF). Rules settled in review: nothing that runs before the tenant is set joins a
  tenant-scoped table (the signed-in user loads without its role; `tests/test_rls_paths.py` stands in for row-level security on
  SQLite), the policy reads an empty `app.tenant_id` as no tenant (`NULLIF`, re-applied by `enable_rls` on deploy), default
  roles are created inside their tenant, and lockout keys fold case the way Postgres does (`upper()`, so a dotless i shares kim's
  counter) in a cache of their own that junk logins cannot flush. `bootstrap_tenant --invite` emails the first director.
- Exports and printing (slice 11): Export on Equipment, Work orders, and Contracts downloads the list as CSV with the filters on
  screen (`/export/...csv`, `views_exports.py`; Equipment View, Work orders View, Contracts View), and a contract's drawer has
  Device list. Every CSV goes through `web/exports.csv_response`: streamed inside the request's tenant (the rows are read after
  the view returns, when the middleware has already left it), UTF-8 with a BOM, ISO dates, plain numbers, and text that a
  spreadsheet would run kept as text (an apostrophe before a leading = + - @, and after a semicolon, tab, or line break inside
  text, since a semicolon-separated Excel splits there whatever the quoting). Printable pages open in a new tab on
  `web/print_base.html` and print or save as PDF from the browser: the work order with its PM checklist (`/print/work-orders/<n>/`,
  line costs to the cent so the columns add up), asset labels with a QR code of the device's request link
  (`/print/labels/?tag=` or the Equipment filters, at most 600, a 2.25 x 1.25 in thermal label or Letter sheets of 2 x 4 in; the
  printed URL shrinks or is left off rather than push the hotline off the label), PM route sheets for the day the PM screen's
  panel shows or the week (one sheet per technician: the open PM work order's holder, the vendor, the suggested technician, or
  nobody credentialed; outside the week the waiting devices are balanced against the day's other suggestions), and every report
  with its charts (`/print/reports/<key>/`; Recalls' Response log prints the recall report). The Overview's Export prints the page
  itself (a print section in cadence.css hides the shell and keeps the light theme). Export and print links carry the list's
  filters: cadence.js keeps their href equal to the address as HTMX changes it, so a middle-click or saved link gets them too.
- Device management (slice 12): Add device on Equipment (`/equipment/new/`, Equipment Edit) adds a device, with a new model or
  department in the same form; the first PM is one interval after the last PM or install date, or today when that has passed
  with no PM on record; tags are unique in any letter case, never change, and "new", ".", ".." are reserved. The drawer edits
  details (`update_asset`: not the tag, status, or contract; a moved next PM takes the open PM work order with it; the next PM
  stays within 10 years) and changes status along `STATUS_CHANGES` (the mock's Tag out of service and Return to service, plus
  lend, missing and found, retire and reinstate). Retiring and reinstating need Equipment Approve; retiring refuses while repair,
  recall, or in-progress work is open, cancels open PMs (which then do not count as missed PMs), and clears the next PM;
  reinstating puts a PM due today. A completed repair returns a device to service only when that repair tagged it out (or it
  was in repair). The drawer's PM schedule tab (PM View) shows the strategy, procedure, next PMs with the technician the PM
  screen plans (`pm.schedule.planned_technicians`, shared with the route sheets), and the PM history (work-order details only
  with Work orders View); its Costs tab (Reports View) shows service cost by year, the share of acquisition cost, the contract
  share (the contract's price only with Contracts View), and the replacement outlook. The API's device, model, and department
  writes go through the same services (`update_device_model`, `rename_department` keep names unique in any case), and the CSV
  importer matches tags in any case, skips reserved tags, and changes an existing device's status only through `set_status`.
- Email notifications (slice 13): each report's Schedule button (Reports View, an email address) emails that report to the user
  themselves every Monday or on the first Monday of each month (`reports.subscriptions`, `ReportSubscription`), with its CSV
  attached byte for byte and a link to its printable page; the daily job `send_report_emails` sends what is due inside each
  facility, re-checks access at sending time, catches up a Monday whose run failed or never happened on the next daily run,
  claims each email before sending (two runs at once send it once), and a schedule turned on after a Monday's run starts with
  the next sending day. Settings' "Confirmation to the requester" offers "On screen and by email" once the facility lists its
  work email domains; the portal then takes an optional work email at those domains only and sends a confirmation and a done
  notice (`portal.notifications`, after commit, inside the request's facility, once each, recorded as status-history notes),
  never with anything the requester typed (not the problem, nor the room), capped per address and per facility each hour (the
  form's per-IP limit can be forged). The confirmation page names the address only to the browser that sent that request at
  that facility. Each Settings portal control saves only itself, so a typo or a stale tab never writes another row.
- The PM program (slice 14): each PM library row opens the device model's drawer (`/pm/models/<id>/`, PM View; also from the
  device drawer's Model). PM program tab (`views_models`): details, the risk score with its four parts and the yearly review
  (`equipment.services.set_risk_score` / `clear_risk_score`, Equipment Approve: the class follows the score's band, and a scored
  model's class cannot be set otherwise), the interval in force, and the devices; Add model and Edit details at Equipment Edit
  (an OEM interval equal to the AEM interval in force is refused: End AEM first). Procedure tab (`pm.procedures`, PM Edit):
  write or revise a procedure (code unique in any case, 0.1 to 40 hours, 1 to 60 checklist lines, `text | what to record`) and
  choose a model's; a printed PM work order shows the current checklist, an open one keeps its hours. AEM tab (`pm.aem`): the
  failure history of the last three years (devices, device-years, corrective repairs per device-year, PMs on time, recall work),
  propose (PM Edit; never life support; a device installed three years ago; differs from the OEM and the interval in force,
  except to ratify one on file without a recorded approval), approve or reject (PM Approve, not the proposer, a committee date
  and minutes; refused if the OEM interval changed since the proposal), withdraw, and end. A longer interval moves no PM; a
  shorter one or an end pulls next PMs in (never later, the open PM work order with them); a model scored into life support
  leaves AEM. Auto-assign week (`pm.services.assign_week`, PM Approve and work-order assign): every PM due today through day 6
  goes to the week plan's technician, new work orders and open ones on nobody's plate alike; held ones are left, devices
  nobody is credentialed for stay unassigned, overdue ones are pointed at (Create on their day, or Work orders). Create and
  Auto-assign take one planner lock per facility, so a double click does nothing twice. The API's model PATCH needs Equipment
  Approve for a new risk class and PM Edit for a new procedure.
- Recording the work (slice 15): the work order drawer's Cost section lists the labor lines (date, who, hours, rate) and part lines
  (description, part number, quantity, unit cost, PO): Log time and Add part at Work orders Edit (`workorders.costs`), each line
  priced to the cent, the rate from Settings (in-house $82, vendor $215 by default) unless an Approve user sets another; nothing is
  added to or removed from a closed or cancelled work order, and one technician logs at most 24 hours a day. Mark completed asks
  what was done (`workorders.completion`); for a PM it records every checklist step (pass, fail, N/A, a reading where the
  procedure asks for one) as the checklist was then, and the result (Pass, Pass with minor repair, Fail) must agree with the
  steps. A failed PM opens a repair (high priority for life support and high risk, with the PM's technician when credentialed)
  or names the repair already open, and holds the device out of service until that repair is done: a device goes back into
  service only when no open repair holds it. The drawer, the print, and the device's PM history show the results; the API's
  transition to completed takes the same fields, and a completed or closed work order cannot be rewritten through the API.
  Reports credit each labor line to the technician on it (vendor time names none). Check FDA feed on Recalls (Recalls Edit)
  fetches the last 30 days from openFDA at most once per 15 minutes for every facility and matches the notices to this facility.
- Who sees what (slice 16): a role's data scope (`DataScope`: the whole facility; only work orders assigned to the user's company;
  only the user's unit) narrows every list, drawer, search, count, export, print, and API endpoint through `workorders.scoping`.
  The vendor technician (company) sees the work orders under their company's name (a contract's vendor, or "<maker> field service")
  and those devices; the clinical requester (department) sees the devices in their unit and those devices' work orders; a scoped
  user with no company or unit sees nothing. Closed by default: a web view or API action refuses scoped users unless it opts in
  and narrows (Equipment and Work orders lists and drawers, search, the work-order lifecycle, CSVs, labels, the work-order print;
  the API reads devices and work orders and moves work orders along, nothing else). The number of a work order outside their share
  reads "another work order" in texts, and the facility's technician roster, report emails, and every facility-wide screen stay
  closed to them. Users and access sets the company or unit (required for scoped roles; units are the facility's departments) and a
  custom role's scope. A vendor's failed PM sends its repair to the same vendor when their contract covers repairs.
- The suite on PostgreSQL (slice 17): `CADENCE_TEST_DATABASE_URL` runs the tests on PostgreSQL 16 (CI's test-postgres job, a
  postgres:16 service): the same suite, connected as a superuser, so row locks, numeric rounding, and constraints behave as in
  production, and the test database carries the row-level security policies (tests/conftest.py). `tests/test_postgres_rls.py`
  switches to the runtime role (SET LOCAL ROLE cadence_app) and signs in, resets a password, accepts an invitation, uses the
  portal, renders every screen and report (CSV and print), calls the API (session sign-in), writes, uses the Django admin, and runs
  bootstrap_tenant, seed_demo, generate_pm, import_openfda, send_report_emails, run_daily_jobs, import_assets, and scheduler --once
  under the policies, checking that the jobs did their work (a job reading no rows would still finish). The policy check reads
  every table with a tenant_id from the database (accounts_user is the one exception). Rules settled in review: a request with a
  NUL character is a 400 (PostgreSQL text cannot hold one); text composed from several fields is cut to its column, the importer
  skips and reports a value too long for its column, and the API answers 400 for an over-long note or vendor name; the nightly
  PM generation takes the planner lock and a recall batch locks its match, so overlapping runs never create duplicates; the
  admin refuses to delete a user, a recall notice, or a facility (several facilities' rows point at them; deactivate instead);
  the portal's case-insensitive department match picks by code point, so every collation agrees. The API with token
  authentication did not resolve the facility then; slice 19 fixed it and tests it as the runtime role.
- Custom reports, Scan tag, and the CMS rule (slice 18): Reports' "+ Custom report" (Reports Edit: the director and Finance and
  quality by default) builds a report from one source (work orders, devices, labor lines, part lines): columns, filters (choices,
  departments, categories, technicians, yes/no, and one date range, a relative period resolved against the run's day so a scheduled
  report rolls forward, or fixed dates), an optional grouping (a column or a date's month: a count, sums of money, hours, quantities,
  and counts, averages of days, age, and condition, the share of PMs on time), and a sort. `reports/custom.py` declares what each
  source offers and nothing else can be asked for: no free text a requester typed (problem, notes, who asked, where), no contract
  prices, nothing about users beyond a technician's name. Money is each line to the cent as the work order drawer adds it, every
  figure aggregates in the database, and at most 10,000 rows are listed (500 on screen). Saved reports are listed under the eight for
  everyone with Reports View and download, print, and email on a schedule like them (`services.find_report` / `run_any`; the API
  keeps serving the eight); running one also needs Work orders View, or Equipment View for devices, and the emails skip those
  without it. Scan tag on Equipment (Equipment View, scoped users narrowed to their share) opens a device from a handheld scanner, the
  camera where the browser reads codes (BarcodeDetector), or the keyboard: a label's request link from any host (another facility's
  label says only that), a device link, or a plain tag in any letter case; a device outside the user's share reads like one that
  does not exist. A label's link opened by a signed-in member who can see the device shows "Open <tag> in Cadence" on the portal,
  and nobody else sees any difference. `DeviceModel.oem_schedule_required` marks imaging, radiologic, and medical laser models, which
  CMS (S&C 14-07) keeps on the manufacturer's schedule: never on AEM (proposals refused, an open one cannot be approved), and marking
  one ends its AEM and brings PMs in, as scoring into life support does; Equipment Approve sets or clears it (Edit details, Add model,
  Add device's new model, the API PATCH, the importer's optional column for models it adds; the admin shows it read-only). Rules
  settled in review: a custom report's PM on time counts a PM cancelled on a device still in use as missed, as the PM completion KPI
  does; blank text and blank coded values sort last either way; editing a report keeps a category filter no model has any more
  (shown checked), so a save never widens it; Edit is not offered to a builder who cannot see the report's source; a scanned link's
  tag is decoded once more than the request's NUL check sees, so one with a control character is no tag; going back to a scanned
  device's pushed URL renders the whole page.
- The API, complete (slice 19): token sign-in resolves the facility (`apps/api/tenancy.py` TenantAPIMixin sets the ORM scope and
  `app.tenant_id` after DRF authenticates, before the permission check reads the role, and restores them once the response is
  rendered; a deactivated user's or facility's token is refused); before, a token request saw no facility and on PostgreSQL every one
  failed. `/api/v1/` now covers what the screens do, one module per area under `apps/api/` (each registers its routes): a work order's
  labor, parts, and notes; contracts through their services (renew, add and remove devices, delete); the PM schedule's Auto-assign
  week, PM procedures, a model's procedure, risk score, AEM cases and decisions; custom reports (build and run) and the user's own
  report emails; Scan; Check FDA feed; users, roles, technicians (read only: Invite user adds them), and credentials. Each endpoint has
  the door of the screen that does the same thing and calls the same service; writes that used to save rows directly (contracts,
  technicians, credentials) go through the services. Vendor technicians and clinical requesters reach only labor, parts, notes, and
  Scan beyond what slice 16 opened. The account services now refuse handing out more access than one's own role has (inviting,
  changing a user's role, copying a role, raising a module's level, widening what a role sees, reactivating an account), moving or
  deactivating an account with more access than one's own, and leaving a facility without a director who can sign in; deactivating
  deletes the user's API tokens. Rules settled in review: every API view but the root refuses a user with no facility (a token can
  never pick one) with "Pick a tenant first", and refuses a body holding a NUL or a lone surrogate (PostgreSQL stores neither); the
  last-director check locks the facility's director rows, so two requests at once cannot both remove one; a pending director
  invitation can always be withdrawn; a risk-score part too large for a number and a custom report's date period that is not text
  are 400s, not 500s. The API never imports apps.web: helpers both doors share live in the services.
- History and notifications (slice 20): `apps/core/history.py` reads django-simple-history's tables into words (each field's before
  and after, labels and values as the screens show them, a foreign key by the record it names or its last name "(deleted)", the
  reason a service recorded, who: the request's user or "Cadence" for a job) for the History tab or section of the device, work
  order (with its labor and part lines), contract, and device model (with its AEM cases) drawers, and the Change log tab on Users and
  access (Users View: every area the reader's role can view, plus every access change, which `apps.accounts.services` now records as
  `AccessEvent` rows: invitations, role and unit changes, deactivations, a role's levels and scope), with filters, CSV, print, and
  `/api/v1/change-log/`. Scoped users see no history. `apps/notifications`: each user chooses on the account menu's Notifications
  page (and `/api/v1/notification-preferences/`) whether to get an email when a work order is assigned to them (one per batch, after
  the commit, never twice), a morning digest of their due and overdue work and PMs this week, and contract reminders (90, 30, 7 days
  before the end and once after, with catch-up; Contracts Edit); `send_staff_notifications` is the fourth daily job. No email carries
  what a requester typed. Rules settled in review: history never names someone of another facility (a signed-in user of another
  facility who sends this facility's public form is recorded as nobody, and any such name reads "Someone outside this facility");
  pages continue after the last entry shown (a cursor in the URL and the API's `next`), so changes saved between pages never repeat
  or skip entries, and saves that change no shown field are read past so a page is never empty while older entries exist; a device
  model's history keeps the AEM end reason off (it stays on the decision, which needs PM View; ending an interval that had no recorded
  approval records it as an ended case, so its reason is kept too); a link that needs more than the
  area's View (a device model opens in the PM program's drawer) is left off for a reader without it; AEM cases of a deleted model are
  named from its last record; everyone who can be sent an email can open Notifications and turn it off (a Contracts Edit user without
  Work orders View sees only contract reminders); the daily job reopens a connection that broke before the next facility.
- Facility time zones (slice 21): each facility works on its own clock. Inside a facility (tenant_context, the middleware, the API
  after authentication) its time zone is active, so `timezone.localdate()` is its today: what is overdue or due, contract days left,
  report periods and "as of" dates, CSV names, the PM calendar, a work order's opened date and a labor line's day, and every time
  shown or printed (print footers name the zone). No code reads the server's clock (`date.today()`, `datetime.now()`: a test fails
  on them). Settings' Time zone (Settings Edit; `facility.services.set_time_zone`, `/api/v1/settings/` time_zone) chooses it from the
  zones the server knows (US first, with offsets; `tzdata` ships with the app); it is audited (Tenant history, the change log's
  Facility area). The daily jobs run per facility: PM generation, report emails, and staff emails once per facility and its local day
  at SCHEDULER_DAILY_AT on its clock (JobRun.facility), the openFDA import once per server day; the scheduler and run_daily_jobs run
  whatever is due (cron every 15 minutes), the commands take --tenant and --date. Rules settled in review: a facility starts with no
  zone of its own and works in the server's (DJANGO_TIME_ZONE) until it chooses one, so an upgrade keeps every facility on the clock
  it already used (the column's old, never-read default was dropped); a run of the previous day left running is taken over at any
  hour, not only before the hour.
- Several facilities (slice 22): the mock's facility menu. A person keeps one account in each facility they work at (its role, unit or
  company, tokens, preferences, history), linked by `User.person`; they sign in once, with one password (a change or reset applies
  everywhere), and land in the facility used last. Inviting an address that another facility's account stores exactly adds the
  facility to that person (`accounts.services.add_account`, which bootstrap_tenant uses too); they join from the email's join page
  or the facility menu, and nothing tells the inviting facility the address was in use (no username in the API's user rows; names
  fall back to the email). Switching signs the browser in to the other account (a tab left in the old facility reloads); staff email
  links name their facility (`?facility=`), and a link for another facility offers the switch instead of opening the record with the
  same number here; sign-in follows `next` for a person with several facilities only when it names its facility. All facilities
  (`/overview/all/`, `/api/v1/overview/all-facilities/`, session only) shows each joined facility's Overview figures, read inside it
  with the person's account there, with totals from summed parts (a shared recall counted once). The demo seeds Riverside North Campus
  with Kim linked.
