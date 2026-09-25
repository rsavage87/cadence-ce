# Build plan

The mock (`cadence-ce-cmms-mock.html`) is the spec. Each slice below is shippable on its own and ends with green tests.
Slices 0 to 4 are built; 5 onward are the next work.

| # | Slice | Mock screen(s) | Code | Status |
|---|-------|----------------|------|--------|
| 0 | Tenancy, auth, roles, audit | Users and access → Roles | `tenants`, `core`, `accounts` | done (needs `makemigrations`) |
| 1 | Catalog + assets + importer | Equipment table, device drawer (Overview tab) | `equipment`, `import_assets` | done (API + admin; no HTMX UI yet) |
| 2 | Work orders + portal | Work orders list/board, WO drawer, request portal | `workorders`, `portal` | done (services, API, portal HTML) |
| 3 | PM engine + KPIs | PM schedule, Overview KPIs | `pm`, `reports` | done (services, API, plain home page) |
| 4 | Web UI shell (HTMX) | Nav, topbar, Overview, Equipment, Work orders | new `apps/web` | done (device drawer: Overview + Work orders tabs) |
| 5 | Contracts UI + credentials UI | Contracts section, Users and access → Credentials | `contracts`, `credentials` + `web` | next |
| 6 | Recalls UI + ECRI importer | Recalls and alerts | `recalls` | later (ECRI needs a license) |
| 7 | Reports | Reports (COSR, MTBF, replacement, spend, contract vs in-house, tech productivity) | `reports` | later |
| 8 | Settings | Integrations, portal settings, editable policy, risk scoring | `settings` app | later |

## KPI definitions (from the mock's `computeKpis`)
- **PM completion on time** for a month: PM work orders with `due_on` in the month and (already past due, or completed), of which `completed_on <= due_on`. Current month uses today as the period end.
- **Life-support PM completion**: same, filtered to `risk_class = life_support`.
- **Fleet uptime**: `100 - downtime_days / (active_devices * days_in_period) * 100`, downtime = turnaround days of repairs completed in the period.
- **Open work orders**: opened on or before the period end and not completed by then.
- **MTTR**: mean `completed_on - opened_on` of repairs completed in the period.
- **Repair spend**: labor lines + part lines of repairs completed in the period.
- **Cost of service ratio**: (work-order cost of the trailing 182 days × 365/182 + annual cost of active contracts) / acquisition value of active devices.
- **Recall alerts received**: alert matches whose alert was published in the period.

## Screen → view map for slice 4
- Overview: `reports.services.overview_kpis(year, month)` + `pm.services.pm_on_time_series` for the 12-month chart; attention list = life-support overdue PMs, alerts needing action, unassigned portal requests, expired/expiring contracts, critical open WOs, WOs awaiting parts > 7 days.
- Equipment: `Asset.objects.select_related(...)` with the same filters as the mock's toolbar (category, status, risk, department, support, overdue-only, bucket). Fleet buckets: retired / out of service / in repair / open recall / PM overdue / PM due ≤ 30 d / compliant, each device counted once in that order.
- Work orders: list and board; status buttons call `workorders.services.change_status`; assignment dropdown lists `credentials.services.qualified_technicians(asset)` first.
- PM schedule: calendar from `Asset.next_pm_on`; "create work orders for this day" calls `pm.services.generate_pm_work_orders` with a one-day horizon per asset.
- Contracts: table from `Contract.objects`, drawer with `add_assets` / `remove_asset`.
- Users and access: `Role` matrix editing `RolePermission.level`; technician credentials CRUD; coverage table from `credentials.services.coverage_by_category`.
