# Build plan

The mock (`cadence-ce-cmms-mock.html`) is the spec. Each slice below is shippable on its own and ends with green tests.
Slices 0 to 7 are built; 8 onward are the next work.

| # | Slice | Mock screen(s) | Code | Status |
|---|-------|----------------|------|--------|
| 0 | Tenancy, auth, roles, audit | Users and access → Roles | `tenants`, `core`, `accounts` | done (needs `makemigrations`) |
| 1 | Catalog + assets + importer | Equipment table, device drawer (Overview tab) | `equipment`, `import_assets` | done (API + admin; no HTMX UI yet) |
| 2 | Work orders + portal | Work orders list/board, WO drawer, request portal | `workorders`, `portal` | done (services, API, portal HTML) |
| 3 | PM engine + KPIs | PM schedule, Overview KPIs | `pm`, `reports` | done (services, API, plain home page) |
| 4 | Web UI shell (HTMX) | Nav, topbar, Overview, Equipment, Work orders | new `apps/web` | done (device drawer: Overview + Work orders tabs) |
| 5 | Contracts UI + credentials UI | Contracts section, Users and access (Users, Roles, Credentials tabs) | `contracts`, `accounts`, `credentials` + `web` | done (no invitation email yet) |
| 6 | Recalls UI + ECRI importer | Recalls and alerts | `recalls` + `web` | done (openFDA feed; ECRI importer deferred, it needs a license) |
| 7 | Reports | Reports (COSR, PM compliance, MTBF, replacement, spend, contract vs in-house, technician productivity, recall log) | `reports` + `web` | done (CSV download and JSON API; PDF, Schedule, and Custom report deferred) |
| 8 | Settings | Integrations, portal settings, editable policy, risk scoring | `settings` app | next |

## KPI definitions (from the mock's `computeKpis`)
- **PM completion on time** for a month: PM work orders with `due_on` in the month and (already past due, or completed), of which `completed_on <= due_on`. Current month uses today as the period end.
- **Life-support PM completion**: same, filtered to `risk_class = life_support`.
- **Fleet uptime**: `100 - downtime_days / (active_devices * days_in_period) * 100`, downtime = turnaround days of repairs completed in the period.
- **Open work orders**: opened on or before the period end and not completed by then.
- **MTTR**: mean `completed_on - opened_on` of repairs completed in the period.
- **Repair spend**: labor lines + part lines of repairs completed in the period.
- **Cost of service ratio**: (work-order cost of the trailing 182 days × 365/182 + annual cost of active contracts) / acquisition value of active devices.
- **Recall alerts received**: alert matches whose alert was published in the period.

The mock's toast-only buttons (Device list, Label, Print, Scan tag, and Export CSV outside Reports) are deferred until an export feature exists; Reports downloads each report as CSV.

## Screen → view map (slices 4 to 7)
- Overview: `reports.services.overview_kpis(year, month)` + `pm.services.pm_on_time_series` for the 12-month chart; attention list = life-support overdue PMs, alerts needing action, unassigned portal requests, expired/expiring contracts, critical open WOs, WOs awaiting parts > 7 days.
- Equipment: `Asset.objects.select_related(...)` with the same filters as the mock's toolbar (category, status, risk, department, support, overdue-only, bucket). Fleet buckets: retired / out of service / in repair / open recall / PM overdue / PM due ≤ 30 d / compliant, each device counted once in that order.
- Work orders: list and board; status buttons call `workorders.services.change_status`; assignment dropdown lists `credentials.services.qualified_technicians(asset)` first.
- PM schedule: calendar from `Asset.next_pm_on`; "create work orders for this day" calls `pm.services.generate_pm_work_orders` with a one-day horizon per asset.
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
  The mock's Custom report, PDF, and Schedule buttons are deferred (they need a report builder and an export service).
