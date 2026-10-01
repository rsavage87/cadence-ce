# Build plan

The mock (`cadence-ce-cmms-mock.html`) is the spec. Each slice below is shippable on its own and ends with green tests.
Slices 0 to 11 are built: every screen in the mock exists, people can be invited and sign in on their own, and the lists
export and print. What each slice deferred is noted in its row and below.

| # | Slice | Mock screen(s) | Code | Status |
|---|-------|----------------|------|--------|
| 0 | Tenancy, auth, roles, audit | Users and access → Roles | `tenants`, `core`, `accounts` | done (needs `makemigrations`) |
| 1 | Catalog + assets + importer | Equipment table, device drawer (Overview tab) | `equipment`, `import_assets` | done (API + admin; no HTMX UI yet) |
| 2 | Work orders + portal | Work orders list/board, WO drawer, request portal | `workorders`, `portal` | done (services, API, portal HTML) |
| 3 | PM engine + KPIs | PM schedule, Overview KPIs | `pm`, `reports` | done (services, API, plain home page) |
| 4 | Web UI shell (HTMX) | Nav, topbar, Overview, Equipment, Work orders | new `apps/web` | done (device drawer: Overview + Work orders tabs) |
| 5 | Contracts UI + credentials UI | Contracts section, Users and access (Users, Roles, Credentials tabs) | `contracts`, `accounts`, `credentials` + `web` | done (invitation email added in slice 10) |
| 6 | Recalls UI + ECRI importer | Recalls and alerts | `recalls` + `web` | done (openFDA feed; ECRI importer deferred, it needs a license) |
| 7 | Reports | Reports (COSR, PM compliance, MTBF, replacement, spend, contract vs in-house, technician productivity, recall log) | `reports` + `web` | done (CSV download and JSON API; PDF, Schedule, and Custom report deferred) |
| 8 | Settings | Integrations, portal settings, editable policy, risk scoring | `facility` + `web` | done (connectors, paging, photo upload, and email or text confirmation deferred) |
| 9 | PM schedule UI | PM schedule (calendar, create work orders for a day) | `pm` + `web` | done (Auto-assign week, Route sheets, and OEM library sync deferred) |
| 10 | Sign-in and invitations | Users and access (Invite user, Resend invite), sign-in page | `accounts` + `web` | done (shared lockout counters across workers need Redis; email is sent in the request) |
| 11 | Exports and printing | Export (CSV) on Equipment, Work orders, Contracts, a contract's Device list; Label, Print, Route sheets, report PDF, Response log, Overview Export | `web` | done (PDFs come from the browser's print dialog; Scan tag, Add device, Auto-assign week, Check feeds, Custom report, and Schedule still deferred) |
| 12 | Device management | Equipment (Add device), device drawer (Edit, Tag out of service, Return to service, PM schedule and Costs tabs) | `equipment` + `web` + `api` | in progress |

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

The mock's export and print buttons work since slice 11; its Scan tag (a mobile app's camera) and Add device (not in the mock either) are still left out.

## Screen → view map (slices 4 to 11)
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
  answer with a fragment or a full page. The mock's Auto-assign week, Route sheets, and OEM library Sync
  only toast and are deferred. `/api/v1/pm/calendar/`, `/api/v1/pm/day/`, `/api/v1/pm/create-for-day/`.
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
  tags cannot contain spaces or slashes (they live in URLs). The mock's Custom report, PDF, and Schedule buttons are deferred (they
  need a report builder and an export service).
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
  per-model scoring waits for a catalog editor. View to see, Edit to change
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
