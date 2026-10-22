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
   (`backends.get_user`), API token authentication, signed-out pages, and management commands before `tenant_context()`. No
   `select_related("role")` there: under row-level security the row is hidden or the query fails, and SQLite tests cannot tell (`tests/test_rls_paths.py`
   stands in for the policy on SQLite, `tests/test_postgres_rls.py` runs the real policies as the runtime role on PostgreSQL; add
   new signed-out paths and commands to both).
3. **Never write `queryset = Model.objects.all()` at class level** (admin, DRF, forms): it is evaluated at import time
   with no tenant in context and stays empty. Resolve querysets inside the request (`get_queryset`).
4. **Permissions are server-side.** Web views use `@web_view(Module.X, Level.Y)` (apps/web/decorators.py, which wraps
   `require_level` with `login_required` and the no-tenant guard); API viewsets set `module` and rely on `ModulePermission`.
   Every API view inherits `TenantAPIMixin` (apps/api/tenancy.py): the middleware runs before DRF checks a token, so the API sets
   the tenant itself once DRF has authenticated, before the permission check (tests/test_api_tokens.py walks the API URLs).
   Per-action levels live next to the services (`apps/workorders/permissions.py`, `apps/recalls/permissions.py`). Hiding a button
   is not access control. Some roles see only their share of a facility (`apps/workorders/scoping.py`: a vendor technician their
   company's work orders, a clinical requester their unit's); every web view and API action refuses them unless it opts in
   (`web_view(..., scoped=True)`, a viewset's `scoped_actions`) and narrows what it shows through `scoping.work_orders` / `assets`.
5. **State changes go through services** (`apps/workorders/services.py`, `apps/pm/services.py`, `apps/contracts/services.py`,
   `apps/accounts/services.py`, `apps/credentials/services.py`, `apps/recalls/services.py`, `apps/facility/services.py`, `apps/equipment/services.py`,
   `apps/pm/aem.py`, `apps/pm/procedures.py`, `apps/workorders/costs.py`, `apps/workorders/completion.py`, `apps/workorders/legacy.py`, `apps/reports/custom.py`,
   `apps/imports/services.py`, `apps/incidents/services.py`, `apps/vendors/services.py`, `apps/evaluations/services.py`,
   `apps/testequipment/services.py`, `apps/cybersecurity/services.py`, `apps/cybersecurity/updates.py`, `apps/cybersecurity/decisions.py`; slice 36's model support through `apps/equipment/services.py`), never by setting fields in a
   view or calling model helpers like
   `Contract.add_assets` directly. Services validate, write status history, and keep the asset in sync.
6. **No PHI by design.** The portal never asks for patient identifiers. Don't add free-text fields that invite them, and never offer
   text a requester typed (a work order's problem, notes, requester, location) in anything emailed, such as a custom report's columns.
   Device incidents (slice 28) are the one area holding facts about a health event of an identifiable person (the day, the outcome, a
   device on a named unit): no free text on an incident (the narrative and deliberations stay in the facility's event report, which
   `event_reference` points to), outcome and who was affected only for Incidents View, never for a scoped user, never a custom report
   source or a report email, and incident emails carry only the number, the device's tag, and the due date.
7. **Migrations are generated, never hand-edited**, and committed with the change. After adding a tenant-scoped model,
   run `manage.py enable_rls --database=migrate` in the deploy step (docker-compose already does).
8. **Tests for every slice:** a tenant-isolation test for each new model, and a service test for each rule.
   `pytest` must be green before a slice is done.
9. **One account per facility, even for one person** (slice 22). A person in several facilities has an account in each, linked by
   `User.person` (`apps/accounts/people.py`); nothing ever lets an account read another facility. Code that needs a person's other
   facility (All facilities, the switch's landing screen, a role name) enters `people.in_facility(account)` and returns plain values;
   outside it, read only User and Tenant (another facility's Role and rows are hidden under row-level security). Only an invitation links
   accounts (`services.add_account`, on the exact stored email), and the inviting facility must never be able to tell. Links in staff
   emails carry `?facility=<slug>` (`people.with_facility`): record numbers repeat across facilities. Person-level API endpoints are
   session only (`PersonPermission`): a token is one facility's.

## Commands
```
python manage.py makemigrations && python manage.py migrate        # first run creates all migrations
python manage.py bootstrap_tenant --name "Riverside" --slug riverside --admin-email you@example.com
python manage.py bootstrap_tenant --name "Riverside" --slug riverside --admin-email you@example.com --invite   # email the director a set-password link
python manage.py seed_demo                                         # small fictional dataset, login kim@riverside.example / DemoPass-2026 (two facilities)
python manage.py generate_pm                                       # PM work-order generation; the scheduler runs it daily
python manage.py import_assets --tenant riverside inventory.csv --dry-run
python manage.py import_data --tenant riverside --kind work_orders history.csv --dry-run   # any kind; the screen is Settings, Import data
python manage.py import_openfda --days 30                         # FDA recall import; the scheduler runs it daily
python manage.py send_staff_notifications                          # the daily digest and contract reminders; the scheduler runs it daily
python manage.py run_daily_jobs                                    # whatever is due (each facility's jobs on its clock); cron every 15 min
python manage.py scheduler                                         # long-running: runs them at SCHEDULER_DAILY_AT (docker-compose `scheduler`)
python manage.py enable_rls --database=migrate                     # Postgres only, run after migrate
pytest
CADENCE_TEST_DATABASE_URL=postgres://cadence:cadence@localhost:5432/cadence pytest   # the suite on PostgreSQL as a superuser, with RLS tests
```

## Conventions
- Dates for business events are `DateField`s (`opened_on`, `due_on`, `completed_on`); timestamps only for audit.
- Today is the facility's: `timezone.localdate()` (inside a facility its time zone is active: tenant_context, the middleware, the
  API), never `date.today()`, `datetime.now()`, or `datetime.today()` (the server's clock; a test fails on them). A function that
  takes `today` reads the clock once and passes it down.
- Money is `DecimalField(12, 2)`. KPI math converts to float at the edges, not in models.
- Choices are `TextChoices` with stable slugs (`in_service`, not "In service"). Display labels can change; slugs cannot.
- Sequences (`WO-26-0042`, `SR-00017`) come from `apps.core.models.Sequence.next`, never from `max(id)+1`.
- Keep views thin: parse input, call a service, render. Reports live in `apps/reports/services.py`.
- Every CSV goes through `apps.web.exports.csv_response` (streams, UTF-8 with BOM, formula-looking text kept as text). Printable pages
  extend `web/print_base.html` (light, no shell, new tab; the browser's print dialog makes the PDF). Links that must carry a list's
  filters use `data-act="with-filters" data-base="<url>"` (cadence.js reads the address at click time).
- Ruff (`ruff.toml`: E, F, W, I; line length 160; migrations excluded). CI runs `ruff check .` and `pytest` on every push.

## Where things are
- `apps/tenants` tenant model (with its time zone), context var, middleware, `enable_rls`; `tenant_context()` sets the facility's time
  zone (`zone_of`, `zone_override`), the ORM scope, and the Postgres
  `app.tenant_id` that RLS reads, so the public portal and management commands see the same rows under RLS
- `apps/core` TenantModel, TenantManager, Sequence, TenantModelAdmin; `validators.reference_token` (slice 30: a reference number as a
  token, never a name: the incidents' event reference, a rental's agreement number, a vendor's service report); `workdays.py` (slice 28: 21 CFR 803.3 work days, weekdays minus
  Federal holidays as observed: `add_work_days`, `work_days_between`); `expressions.DayNumber` (a date as a day number, the same on
  SQLite and PostgreSQL); `days.local_day` (a timestamp's facility day: read audit
  timestamps and history dates inside the facility, never with .date()); `history.py` a record's changes in words from django-simple-history
  (`record_history`, `entries_for_rows`; `who(rec)` names whoever made a historical row's change as this facility may read it) and the facility's change log (`change_log`) across the areas a role can view (`can_read`,
  `readable_areas`: none for scoped users; an area may need View on more than its module, `Area.also`: a vendor review's, Contracts View
  too); staff-written text (`PRIVATE_TEXT`: an evaluation's narrative, a vendor review's summary) reads in the change log, its CSV and
  print, and the API's only as edited, the words only in the record's own History. The historical tables' managers are not tenant-scoped,
  so every query there filters on the current tenant (`_rows`)
- `apps/accounts` User, Role, RolePermission, default roles, `require_level`; `Role.levels()` (slice 28: the levels in force; a module a
  role has no row for, added after it was made, takes FULL on a system role and DEFAULT_LEVELS on a default role, None on a custom one;
  level_for, the role matrix, and the API's roles read it); `DataScope`, `Role.scope` (blank: the default by slug, vendor
  company and requester department) and `User.company`; `AccessEvent` (one row per change to who may do what, written by the
  services through `record_access_event`, in the change's transaction); `services.py` for invites, role changes, (de)activation (deleting the user's API
  tokens), role matrix edits (nobody grants a role more access than their own; a facility keeps a director who can sign in), a
  user's company or department (`set_user_scope`, `set_user_access`) and a custom role's scope;
  `invitations.py` (signed set-password links: `send_invitation`, `is_pending`; a resend replaces the link), `signin.py` (sign-in lockouts and
  password-reset requests, never revealing whether an address has an account), `backends.py` (username or email in any case; a deactivated
  facility cannot sign in), `emails.py` (the one place account emails are sent; a failure returns False, never raises). Links in emails start
  with `APP_BASE_URL`, never the request's Host. Slice 22, one person in several facilities: `people.py` (which of a person's accounts they
  may open: `can_enter`, `is_pending`, `is_joined`, `landing_account` (sign-in opens the one used last), `facility_menu`, `switch_target`,
  `join` (a pending invitation takes the session's password in one conditional UPDATE), `in_facility`, `with_facility`); `User.person` with
  one account per person and facility; `models.share_password` (a password set on one account is the person's everywhere; their pending
  invitations' links die); `services.add_account` (invite_user's and bootstrap_tenant's account: an address another facility's account
  stores exactly joins that person, with the username `<email>@<slug>`); invitations send a person who already signs in the "added to"
  email with the join page's link; a reset sends a person one link; lockouts also count per person; names show the full name, else the
  email (`str(user)`), never a username
- `apps/equipment` Department, DeviceModel, Asset (tags carry no spaces or slashes: they are URL segments, and never change once a device is
  added; slice 25: `added_as`, how it came to be in Cadence: new, already in use here, or imported, blank before slice 25; only
  `create_asset(added_as=)` sets it; slice 26: `awaiting_inspection`, new and not yet through its incoming inspection: no next PM, the
  hold (`set_status` never puts it in service or on loan; Found and Reinstate bring it back out of service), `use_before_inspection`
  (Approve, a `UseBeforeInspection` reason), `pass_incoming_inspection` (its one writer, on the locked row), `status_label` ("Awaiting
  inspection"), and update_asset and set_status work on the device's row as it is, locked (`_locked_row`), never a caller's older copy;
  slice 28: `incident_hold`, held as evidence by an open incident, written only by `set_incident_hold` / `clear_incident_hold`;
  set_status refuses every move while it is set, status_label reads "Held for incident" first, and `_locked_row` takes the device's open
  work orders in number order before its row: `workorders.services.lock_work`, the lock order every writer of a device's work keeps;
  slice 29: `ownership` (`Ownership`: ours, rental, vendor loaner, demo) with a temporary device's stay on its own record (owner,
  `owner_reference` a token, arrived, due back, the owner's PM date, returned, kept, `stands_in_for`, the return's cleaning and patient
  data), written only by create_asset and `add_temporary_device` / `update_temporary` / `return_to_owner` (the shared `_retire` body; slice 38: retiring or returning cancels open and awaiting-parts software updates, `views_temporary.CANCELLABLE` in step, the status
  toast counting them) /
  `keep_temporary_device` (Approve); its owner maintains it (`owner_maintains`: no next PM, no PM work orders, SupportType.OWNER); a
  returned one is RETIRED and reads "Returned to owner"; `OWNED` / `WORK_ORDER_OWNED` are the counting rule (CE-program and fleet figures
  count ours only; work, recalls, and incidents count every device); `service_vendor` names a work order's vendor (a temporary device's
  owner first); one record per arrival, `matching_returned` finds an earlier stay by model and serial; `loaner_device_back` / `loaner_back_q`
  the one rule for a loaner's device being back: in service or retired with no vendor repair open),
  CSV importer; `services.py` fleet queries plus adding devices, models, and departments, editing (`update_asset`,
  `update_device_model`, `rename_department`: names unique in any letter case), and status changes
  (`STATUS_CHANGES`; retiring cancels open PMs), risk scoring (`set_risk_score`, `clear_risk_score`, `RISK_SCORE_BANDS`; every model
  change goes through `_save_model`, which locks the row and tells `apps.pm.aem.model_changed`), `permissions.py` (Edit to add, edit,
  tag out, and add or edit models; Approve to retire or reinstate, to score a model or change its risk class, and to set or clear
  `oem_schedule_required`, the CMS rule that keeps imaging, radiologic, and medical laser equipment on the manufacturer's schedule:
  `DeviceModel.aem_excluded` covers it and life support); slice 35: `lock_tags` (the facility's tag lock: devices and test equipment
  share one sticker space, unique in any letter case across both tables; create_asset, add_test_equipment, update_test_equipment's tag
  and unit changes, and set_status's moves to in use take it first) and
  `is_test_equipment_tag`, `RESERVED_TAGS` with export.csv; slice 36: a model's `life_source` (`LifeSource`) and the manufacturer's
  support (`sale_ended_on` its first day, `support_ends_on` and `parts_end_on` their last days, `support_notice` a token,
  `support_checked_on`), written only by `update_device_model` (`SUPPORT_FIELDS`, `UPDATE_MODEL_FIELDS`, `_support_fields`: milestones
  from 1950 and within 50 years, the check from 2000 and not after today; never create_device_model, Add model, or Edit details; read
  only in the admin) and worded by `support_status(dm, today)` (`SupportStatus`: parts ended, support ended, sale ended, announced,
  supported (checked in the last 12 calendar months), unknown); `scan.py` which device a scanned or typed code names (a label's request link
  with any host, a device link, or a plain tag in any letter case, among the devices the user may see)
- `apps/contracts` Contract with add/remove device operations and cost allocation; `services.py` for create/update/renew/delete, status, filters, KPI summary
- `apps/workorders` WorkOrder and lines, ServiceRequest, lifecycle services; `scoping.py` who sees which devices and work orders inside a
  facility (scope_of, work_orders, assets, can_see_*, and shown_text, which masks other work orders' numbers); `costs.py` the only writer of labor and part lines (rates from
  Settings, a different rate at Approve, nothing on a closed or cancelled work order); `completion.py` completing with the resolution
  and a PM's step results (`complete_work_order`: the screen's Mark completed and the API's transition to completed both use it; a
  failed PM opens or names its repair and holds the device out until that repair is done; slice 24: an open PM is started as it is
  completed, both moves in its history (`starts_on_completion`; a repair still needs Start), and optional hours are logged for the
  default technician in the same transaction); a line's cost is hours × rate or
  quantity × unit cost to the cent, half up (`LABOR_AMOUNT`, `PART_AMOUNT`, `line_cents` in `models.py`), wherever lines are added up;
  `permissions.py` (Edit to record labor, parts, and completion; Approve to assign, close, or charge a different rate; Edit to take
  unassigned work, `can_take`); `my_work.py` the technician's own work (slice 24: whose work, scope first, a vendor's company, never a
  requester's, else the user's active technician profile, `credentials.services.technician_of`; the groups; `due`, the one "due" of the
  page, the nav badge, and the digest; `takeable`; `scan_target`, what a scanned label opens); `services.take` (a technician assigns
  themselves unassigned in-house work they are credentialed for, while the facility allows it); slice 25: `WorkOrder.late_reason`
  (`LateReason`, choices only) why a PM missed its due date, written only by `services.set_late_reason` on `pm.services.missed_pms`
  (Edit; Approve once closed: `permissions.can_set_late_reason`), from the drawer's Why late row, Mark completed, and the API;
  slice 26, `inspections.py` incoming inspections (`state`, `open_inspection`, the one opener `open_for`, `banner` for the drawer,
  `uses_before`, `waiting_for_inspector`, `lock_open` (the lock order: a device's open inspections, then its row), `pass_cleared_flag`,
  the `INCOMING` checklist); `WorkOrder.inspection_result` (`InspectionResult`), written by completion: an inspection has a checklist
  (the model's PM procedure or `INCOMING`) and a result (required while its device waits; a failed step needs Failed), completes in one
  visit, and a fail opens or reuses a re-inspection (never a repair); `create_work_order` refuses a PM or a second open inspection on a
  waiting device; `legacy.py` work
  orders imported from another system (slice 23: `legacy_number`, the previous number, unique per facility; `Source.IMPORTED`; created in
  their final state with backdated status rows, never emailing or changing the device; their lines through costs.py's import-only writers,
  in-house hours that name no technician here as an in-house cost line, never vendor time; imported labor never fills a technician's live day);
  slice 30: every repair records how it ended (`WorkOrder.repair_check`, `RepairCheck`: checked and ready for use on
  `repair_checks.POST_REPAIR` (visual and function never N/A; readings, never limits), checked by the vendor with `checks_reference` (a
  token, vendor service only, never for a scoped user), or not returned to use (the device stays out)), written only by
  `completion.complete_work_order`; `completion.keep_open` (a failed check: noted, the device held out); `services.returns_device`;
  `repair_checks.checked_by` names who checked (the vendor, else the technician assigned); equipment's set_status refuses in service or
  on loan while a repair holds the device (`holding_repair`, `repair_hold_message`), and an incident's Return to use waits for its
  investigation's checks;
  slice 31: `WorkOrder.vendor_engineer` (the vendor's engineer responsible, as their report names them; optional), written only by
  `complete_work_order` (vendor_engineer / engineer_name, KEEP when omitted) and `services.set_vendor_engineer` (Edit; Approve once
  closed; never scoped: `permissions.can_set_vendor_engineer`); the vendor's report number (`checks_reference`) on any vendor job (kept
  when omitted, refused in-house); `assign` only on open work orders, clearing the engineer when the work moves (by vendor_key);
  `repair_checks.checked_by(wo, user)` names the engineer;
  slice 33: `WorkOrder.repair_finding` (`RepairFinding`: what a repair found, a choice; "use error", never "user error"), written only by
  `complete_work_order` (required when nothing is stored, KEEP when omitted) and `services.set_repair_finding` (Edit; Approve once closed;
  never scoped; "Recorded later" on one completed before findings); `findings.py` the one rule (groups partitioning every value; failures
  are reliability findings, unknown ones, and every open repair: never use error, damage, an accessory, power or room conditions, or no
  problem found; `choices_for(wo, user)` every door offers and the service enforces: a scoped user only observations, an investigation
  never use error, a failed PM's repair never "PM could have caught it"; a scoped user never replaces the facility's finding while the
  repair's limits allow it (`FACILITY_RECORDED`); `failure_q`, `recorded_q`); assign clears not stated when work leaves the vendor; an
  incident's adoption clears a use error (`services.clear_use_error`);
  slice 35: the test equipment a work order used (`apps.testequipment` TestEquipmentUse), written only by `complete_work_order(test_equipment=
  KEEP)` and `services.set_test_equipment` (completed or closed; Edit, Approve once closed, never scoped:
  `permissions.can_set_test_equipment`), checked by testequipment.services.check_uses (the items' rows locked after lock_work and the
  work order's row); each use has its own day (`TestEquipmentUse.used_on`, the day of the work it was recorded on, written once by
  `apply_uses`: a re-completion never moves it), and every reader judges it on that day; `assign` (work moving to a vendor), a completion
  as checked by the vendor (a list there may name only kept ones), and a cancel (taking no item lock) take them off, never a use the
  kept-use rule keeps (a calibration dated on or after the use's day is on file); keep_open never records any;
  slice 38: `WoType.SOFTWARE_UPDATE` ("Software update", short "Update"; the Overview's "other" series), opened only by
  apps.cybersecurity.updates (`create_work_order(_register_opener=True)`; otherwise refused, keyed type, `UPDATE_OPENED_ELSEWHERE`; never
  on New work order, whose type field keeps its refusals per form (`web.forms._TypeField`); the legacy import refuses an open one,
  `OPEN_UPDATE`); started on completion (`starts_on_completion`), never a
  failure, a finding, a re-check, or a PM tab entry; `complete_work_order(software=KEEP)` with the default resolution when an outcome is
  recorded and none typed;
  slice 29: no PM on a temporary device (`no_pm_message`), its incoming inspection on `inspections.TEMPORARY_INCOMING` and refused a
  pass while its owner's PM date is past (`owner_pm_refusal`), recall batches give its recall work order to its owner;
  slice 28, a device held as evidence (Asset.incident_hold): `change_status` refuses a start or completion (`HOLD_MOVES`) of any of its
  work orders but the investigation of an open incident holding it (by identity: incidents.services.investigation_of), read on the
  device's row locked after its work (`_lock_for_hold`, `lock_work`: a device's open work orders in number order, then its row, the
  order complete_work_order, equipment's `_locked_row`, and the incident services keep); `held_work_message`, `hold_blocker` (what a
  screen offers), `with_held` (My work's `held`); `_on_completed` never returns a held device to service;
  the facility's lock order: the tag lock (equipment's `lock_tags`, where a writer takes it), then devices' work (`lock_work`; a writer of
  several devices' work takes all of it first, in one number order: `lock_devices_work`), then the work order numbering (`lock_numbering`,
  before any device's row: a batch numbers first and takes each device at its COMMIT), then device rows; a model's row is locked FOR NO KEY
  UPDATE (a deferred foreign key takes it FOR KEY SHARE at COMMIT; a rename's save holds it FOR UPDATE, so a devices import chunk takes
  the models its rows name FOR KEY SHARE before its devices' work)
- `apps/pm` PmProcedure, PM generation, on-time math (`pm_due_queryset`; `RETIRED_AND_CANCELLED` excuses a cancelled PM only when its
  device was retired on the PM's due date, from the device's history; `missed_pms`, the PMs not on time as of a
  day; slice 27, `windows.py` the PM completion window, the one rule every compliance figure uses: `Window` / `Windows` from Settings'
  `pm_window_high` / `pm_window_other` (`PmWindow`), `end`, `cutoff`, positive conditions only (`on_time_q`, `not_on_time_q`,
  `past_window_q`, `inside_window_q`: never negate one), read once per figure and passed down as `w`; `pm_pending`, `assets_past_window`;
  the schedule (`overdue_assets`, the badge, Auto-assign week, the calendar, Equipment's buckets, My work) keeps the due date), month helpers; `schedule.py` the PM schedule's read models (month calendar,
  a day's devices, suggested technicians, `planned_technicians` (who does each device due on a day: the day panel, route sheets, and
  the device drawer's PM tab share it), 30-day outlook, 7-day workload, PM library); `services.create_pm_work_orders_for_day` and
  `assign_week` / `week_assignment_preview` (Auto-assign week; both take `lock_planner()` first; slice 28: the planner suggests nobody
  for a device held as evidence, `WeekAssignment.on_hold`, and its PMs are still opened, only never started); `aem.py` the AEM program
  (`AemDecision`: evidence (slice 33: failures, PM-related, found on PM, and how many repairs had a finding, beside repairs: read with
  `finding_figures`, coverage before any PM-related rate; evidence from before reads "not recorded then"; the AEM tab's "Record what was
  found" list), propose, approve, reject, withdraw, end, the pull-in of next PMs; the only writer of
  `DeviceModel.aem_interval_months`; `exclusion()` says why a model never goes on AEM: life support, or the CMS mark); `procedures.py` (write and revise procedures, the checklist line format, `set_model_procedure`);
  `permissions.py` (View to see; Edit for procedures and AEM proposals; Approve to create a day's work orders and to decide or end
  AEM; Auto-assign week also needs work-order assign)
- `apps/incidents` device incidents (slice 28, beyond the mock; 21 CFR 803): `Incident` (number IN-<yy>-<nnnn>, the suspect device,
  the investigation work order (an open repair adopted, or a tagged-out repair the incident opens), occurred and "clinical staff first
  knew" days, Outcome (unknown counts as serious until decided), Affected (a category, never who), event_reference (the facility's event
  report: the narrative and deliberations live there; no free text here), report_due_on (stored, written only through
  `services.due_date`: the 10th work day after aware_on for death, serious, or unknown of a patient or staff on duty), the decision
  (reportable, Basis, decided_on, DecidedBy, recorded_by), the reports (FDA and manufacturer days, the 803.3(x) report number), the
  device evaluation's Finding, accessories, event log, status open / closed / in_error) and `IncidentHold` (a device held by an
  incident: status before, sent to and back from the manufacturer, the release); `services.py` every write (record, hold another device,
  facts, decide (outcome and who was affected change only here once decided), reports, finding (a failure clears a "does not suggest"
  decision), open investigation, manufacturer trips, release (return to use waits for the investigation, the decision, the reports, and
  a PM not past due; the device goes in service only when nothing else holds it: `release_note`), close, reopen, recorded in error (needs
  Equipment Edit to put a device back in use)) and the read helpers (`clock`, `needing_action`, `badge`, `hold_words`,
  `investigation_of`); `permissions.py` (Edit records and holds, also Equipment Edit; Approve decides, reports, releases, closes; scoped
  users never); `notify.py` (the incidents emails to Incidents Approve: recorded, 3 work days left, overdue; number, tag, due date only)
- `apps/vendors` contracted service and vendor staff (slice 31, beyond the mock; CMS A-0724 asks how the facility assures contractors use
  qualified people, never each engineer's certificate): `VendorEngineer` (a name and a company, as the vendor's reports name them; one per
  vendor and name by the stored `company_key` / `name_key`; never deleted, deactivated; the company fixed once a work order names them),
  `EngineerTraining` and `VendorAttestation` (the vendor's written statement: one or more vendor names, a token reference, received, valid
  from and through), each covering device models (ids), a manufacturer's product category, or (an attestation) every device the vendor
  services, and each judged by its own dates whenever entered (a renewal is a new record; removed only as entered in error); `rules.py`
  the one rule (`vendor_key`: casefold, spaces collapsed, one trailing " field service" dropped; the name rule: letters and . ' - , only;
  `evidence_for` a vendor job on its completed day: the named engineer's training, else an attestation from the work order's vendor or
  the engineer's company, else none; the words: "training on file", "attestation on file", never "trained" or "qualified"); `services.py`
  every write and the doors' helpers (`engineer_choice` / `KEEP`, `engineer_options`, `may_name`: a scoped vendor account only its own
  company's engineers, an id outside its choices answering as unknown; `engineer_shown`: "Another company's engineer" for a scoped
  reader; `engineer_named` creates on the unique key, re-read after a race); `permissions.py` (Contracts View sees, Edit changes; scoped
  users never). No email, phone, files, notes, health, or background data on an engineer (badges and immunizations are the vendor
  credentialing system's); never in an email, a digest, or a custom report
- `apps/testequipment` the CE department's own test equipment and its calibration (slice 35, beyond the mock; DNV surveyors apply ISO
  9001's control of measuring equipment, no US rule names it; Cadence never says a standard requires it, never "calibrated", "in date",
  or "uncalibrated" as a status, never "instrument"): never an equipment.Asset (no fleet, PM, report, recall, portal, contract, or
  evaluation figure counts it). `TestEquipment` (tag shared with the devices', `tag_key`; kind; make, model, serial; `Program`
  calibration or verification; interval; status with its `Reason` and `status_since` (the day of the change) and `held_since` (out of
  service: the day a passed certificate must be dated on or after to release it, the one release rule); held_by), `Calibration` (an outside provider's
  certificate, judged by its own dates whenever entered; due_on null exactly when failed; one live record per item and day; entered in
  error, never deleted), `ToleranceReview` (the decision on the work done with an item later found out of tolerance or failed: its basis,
  its window and work orders frozen), `TestEquipmentUse` (with `used_on`, its own day); `rules.py` the one rule (`standing_on` and its
  words, the entered-after disclosure, `window` / `window_q` on used_on, `counted_work_q` / `counted_uses_q` positive only, `offered`,
  `usable_today`, `may_remove` / `kept` the kept-use rule on each use's day, `judge_uses` and the batch readers); `services.py` every write (add with an optional first certificate, update, set_status
  by level, record_calibration with its due default and cap and status moves, mark_entered_in_error, decide_review on the list shown:
  REVIEW_CHANGED) and the work order's side (check_uses / apply_uses, picker, judged_for); `permissions.py` (Equipment View sees; Edit adds,
  edits, moves everyday status, records certificates; Decide: Work orders Approve with Equipment View, the review, entered in error, the
  interval once a certificate is on file; Equipment Approve retires and reinstates; scoped users never; a work order's test equipment is
  shown only with Equipment View)
- `apps/evaluations` the medical equipment program's annual evaluation (slice 32, beyond the mock; The Joint Commission dropped the
  hospital requirement in 2026, DNV and ACHC still expect one: Cadence never says which accreditor requires it, and Met is the facility's
  own target): `ProgramEvaluation` (EV-<yy>-<nnnn> and a version; a 12-month period that has ended; draft -> ready ("Sent to committee":
  frozen) -> approved -> superseded by its amendment; discarded; live periods never overlap), the nine staff-written NARRATIVE fields
  (length-limited, never emailed, never a CSV or custom report, never a scoped user's), `EvaluationSnapshot` (immutable figures, no history),
  `EvaluationObjective` (this period's reviewed, carried from the previous approved evaluation; next period's), `EvaluationApproval` (a
  committee's approval from its minutes; the first approves); `indicators.py` (INDICATORS with stable keys, `snapshot()` for a period by the
  targets and PM windows in force at its end, the previous period beside each (approved, else counted), the AEM table, the binder's gap
  counts; `cut(data, user)` for every reader: never render a snapshot without it; refreshing needs View on every area); `services.py` every
  write and the read helpers (`current`, `previous_approved`, `next_due`, `suggested_start`); `permissions.py` (Module.EVALUATIONS:
  View reads, Edit drafts and writes, Approve records approvals and amends; the figures also need Reports View); slice 34, the review of
  each contracted service vendor (standalone models: `VendorReview` (VR-<yy>-<nnnn>; one or more vendor names and their keys; a 12-month
  period ending on any day that has ended; draft -> signed off; withdrawn frees its period; discarded; no key in two live reviews with
  overlapping periods; never ending on Feb 28 of a leap year, `leap_feb_28`, so each next period starts the day after), `VendorReviewSnapshot`,
  `VendorReviewReport` (a committee it was reported to)); `vendor_reviews.py` every write (create with prefill from the previous
  signed-off review, refresh, update, sign off on the snapshot shown and the reviewer's fields shown, `fields_version` / REVIEW_CHANGED,
  plan result, withdraw, discard, committee reports for several reviews at once) and `vendors_list` (each vendor's last review and next due);
  `vendor_figures.py` the figures from Cadence's records of the vendor's work (contracts from their history, time with the vendor from
  the handoff, PMs assigned to it, repeat repairs that were real failures, documentation by who completed, staff evidence, cost; never
  uptime or response from Cadence's dates; imported work orders only in jobs_imported and cost_imported), `can_see` / `cut` all or nothing (Evaluations, Contracts, Reports, Work orders View); the module's
  label is "Evaluations"; seeing a review also needs Contracts View; never a scoped user
- `apps/capital` replacement priority (slice 36, beyond the mock; no US rule requires replacement planning, AAMI EQ56 recommends it):
  `priority.py` the one rule the Replacement planning report (its screen, CSV, print, email, API rows) and the device drawer read
  (`score_assets(qs, today, start)` in a fixed number of queries, `QUERIES`; `for_asset`; `rank`): our devices in use (OWNED,
  ACTIVE_STATUSES), five components (`WEIGHTS`: age against life 30, support 25, failures 20 (findings.failure_q over the window),
  service cost a year against the estimate 15 (every labor and parts line recorded, never recall work, plus the share of the device's
  contract not ended, as cost_of_service reads them but recall work (which it counts): which past lines a contract paid for cannot be told from the records; a share that
  cannot be allocated is unknown unless the lines alone reach the top), a confirmed recall not done here 10),
  times `MULTIPLIERS` by risk class; an unknown component is never 0 nor reweighted (`points_unknown`); the window 36 months clipped to
  the install date and the facility's first work order, under 12 months "too little history" (`window_of`, `enough_history_on`);
  `Estimate` (list price and the day it was set from history, else acquisition cost; no markup); flags (`MUST_ACT`, `REVIEW`) apart
  from the score; `GAPS`; `suggested_fy` ("suggested", never "due"); `version_words` printed wherever a score is shown (the CSV's last column "v2").
  No models yet (later slices: requests, decisions, budgets, the plan; replaced_by, retire reasons). Never incidents, status
  words, or requester text
- `apps/cybersecurity` the device security register (slice 37, beyond the mock; HHS 405(d) HICP 2023 recommends keeping the medical
  device inventory, with major version information, in the CMMS; no rule requires a per-device record, and Cadence never says one does):
  `Module.CYBERSECURITY` "Device security" (never "security": that word means row-level security here; analyst, requester, vendor None;
  scoped users never); `SoftwareRelease` (a model's release as the manufacturer writes it, `version_key`, or its versionless PROFILE; the
  support, MDS2, and disclosure groups (a release overrides the profile by group, never field by field) and the profile-only facility
  group (who installs updates here, segment); entered in error marked, never deleted, restored by adding the version again) and
  `DeviceSecurity` (one device's versioned release, since, network fact when it differs from the disclosure, checked); `rules.py` every
  door's words and readers (`facts`, `connected`, `release_standing` (slice 36's model support when the release has no date) and
  `os_standing` (two lines, never merged; last days), `standings`, `fleet`, `changes_for_it` (devices added, missing, retired, returned,
  from equipment's history), `QUESTIONS`, `NEVER_WORDS`; every device on site, ours and temporary; fixed queries); `services.py` every
  write (level checks inside; locks: the facility's register lock (`lock_register`), a release, the devices FOR KEY SHARE in tag order
  (as contracts and Recorded in error keep them; an import chunk locks in its file's order: the bulk set then answers RACE), then
  security rows; `model_changed` from equipment.update_asset, never taking the register lock,
  clears a release of the old model); `filters.py` the Fleet's filters (`FleetFilters`, `parse`, `parse_strict` for the API's 400s, `within` for a head figure's link) shared by the screen and the API; `services.running` / `removal_refusal` count devices on site only (a retired device keeps naming a release entered in error). Never kept: credentials, network
  identifiers, SBOM contents, the MDS2 file, advisory text, free text; never in an email, a custom report, the device's own CSV or API
  record, the portal, labels, or Scan. Slice 40: advisories (advisory decisions, the advisory batch, the multi-need Record decision);
  slice 41: register upkeep and readers (the version at every completion, the import, hardening facts, the binder section, evaluation
  indicators, the capital line); slice 42: the CISA CSAF feed.
  Slice 38, software updates (FDA 2016: a routine update is "not considered a repair"; no rule requires any of it): `SoftwareUpdate`
  (one per update work order, made when it is opened, holding its target: a release of the model (`target_release`) or the
  manufacturer's patches (`target_reference`, a token); or an install recorded by hand (no work order, `installed_by`); `outcome`
  installed / patches / already (blank: pending or not confirmed, as its work order's status says), `release_after` (never a "before":
  the records cannot say what a device ran on a past day once corrections and backdated days mix, so an update states only what the
  person recording it says it did; the device's History shows each change of its release), `reference`, `basis`
  (`UpdateBasis`: validated_list for patches only), `applied_on`, `later_kept`, `on_vendor_service` / `vendor_report` (where the outcome
  came from, as the work order was when it was recorded, never as it is later); a hand row entered in error is marked, never deleted;
  CheckConstraints for the two shapes); `updates.py` every write (level checks inside, refusals keyed by field, never
  "required"): `open_update` / `open_updates` (the batch: one release's devices on site from chosen releases or none recorded, optionally a
  department, at most `UPDATE_BATCH_MAX`, due on a day or each next PM; held, already open, and model-changed devices skipped with
  words; assigned only at Work orders Approve, by the profile's `updates_by`: CE to the technician chosen, the manufacturer and a
  temporary device to vendor service, IT and the department unassigned), the fixed problems `UPDATE_PROBLEM` / `PATCHES_PROBLEM` (no
  version or reference), `choice_from` / `UpdateChoice` / `NOT_CONFIRMED`, `check_choice` and `record_at_completion` (complete_work_order's
  `software`), `confirm` (Confirm version, dated the completion), `record` (Record update; a duplicate of a live record refused,
  `DUPLICATE`), `remove` (Entered in error: a hand row marked, a work order's outcome cleared; the facts never touched), the facts never
  backwards (`later_kept`); no outcome is judged against the device's register record (installed and already name a release of its
  model, already on a release target names the target; patches never take a release: they name the recorded one when the facts are
  written); the facts written: installed moves the release since that day, already a release the record does not name (found), patches
  only the day checked; `open_number` (an open update work order, with its
  record or not: a reopened imported one counts); the lock contract in its docstring (register, lock_work, numbering, releases, devices,
  security rows, update rows; nothing takes the register after a work order's or a device's row; a completion takes the register only
  when its choice records). Django creates PostgreSQL foreign keys DEFERRABLE INITIALLY DEFERRED: an INSERT naming a row locks nothing
  until COMMIT, so the openers take the device explicitly FOR KEY SHARE after numbering (`services._locked_devices`) and read its model,
  status, and hold there; an IntegrityError at commit (a device deleted meanwhile) answers RACE; `rules.py` `release_offer` / `offer_or_why`
  (the one predicate: an update with its record, the device on site, can_edit_devices; None is the system), `update_state` (open,
  not_confirmed, done, cancelled, removed) and `STATE_LABELS`, `update_words` (the sentences, behind can_view_devices only; never
  "updated from X to Y"), `release_name` ("(entered in error since)"), `updates_of` and `campaigns` / `campaign_words` (one query each);
  `permissions.can_open_updates` (Edit, Equipment View, Work orders Edit) and `can_correct_updates` (Approve, Equipment View); the
  history Area `software_updates` (Device security View and Equipment View; never in a work order drawer's History); `remove_release`
  refused while an open update is to install the release. Never a version, reference, target, or sentence outside can_view_devices.
  Slice 39, security decisions for devices past support (HHS 405(d) HICP 2023 recommends documenting mitigating controls and a timeline
  through the organization's risk acceptance or exception process when a system past its support stays in use; no rule requires any of
  it): `DeviceSecurityDecision` (SD-<yy>-<nnnn> by the year decided, `Sequence` "decision-<yy>"; `applies_to` support_ended, keyed on by
  every constraint and rule (slice 40 adds advisory); one scope (CheckConstraint): a device, a model's devices of ours on one release, or
  every device of ours of a model, FKs PROTECT; one live (not ended) decision per scope, three conditional UniqueConstraints (never one over
  a nullable column); `reasons` kinds (security_updates, os_support: ended or announced, never states); `response` keep_with_controls /
  keep_without_added_controls / take_out_of_use; `controls` slugs (`rules.CONTROLS`; some exactly to keep with controls; a support
  decision never network_removed: Record facts' "Not connected here" needs none) and `controls_reference` (with recorded_elsewhere only);
  `plan`, `plan_by` (a replacement's fiscal year worded at read time, never stored), `plan_owner`; `accepted_by` a role, never a name;
  `exception_reference` IT's number (IT's record stays the system of record); `decided_on`, `review_by` (default and cap product choices:
  `review_default`, `review_cap`); `governed_ids` (the devices it governed when recorded, never one under a more specific live decision:
joined since, by identity); `supersedes`; `ended_on` / `ended_by` / `end_reason`
  (superseded, no_longer_needed, entered_in_error); immutable but its end; admin read only); `decisions.py` every write
  (`record_decision`, superseding the live one of its scope in one transaction (Review and renew), `end_decision` (no longer needed),
  `mark_entered_in_error`; levels inside, `permissions.can_decide` (Approve and Equipment View, never scoped; None is the system);
  refusals keyed by field, every field problem at once before any lock, worded as recorded; each token never a network identifier
  (`network_identifier`: an address, with a port, range, or prefix too; a MAC in any layout, within a token too, the digit-friendly
  layouts needing a hex letter and a digit, a UUID's layout exempt; a VLAN; a port); `refusal_for` the same scope checks read without
  locks, in the same order (the modal says why before it is tried; outside any transaction, so no FOR UPDATE), a `Refusal(words, kind,
  live)` (KIND_* in_force, ended, gone, not_needed, unknown; `ends_it`: End is offered only for gone and not_needed, never for a fact to
  record first: `rules.need_unknown(standing, reasons)`, judged against the reasons of the decision renewed (OS_SUPPORT_NOT_RECORDED,
  NO_RELEASE_OS), SCOPE_NOT_RECORDED; a release's or model's reasons read from the devices it would govern (`decisions.would_govern`,
  `_scope_read`; `rules.holding_lines` / `under` for "What holds now"), GOVERNED_ELSEWHERE / NEEDED_ELSEWHERE refusing a scope whose
  devices (needing one) all have a more specific decision; a release's or model's updates continuing in EXTENDED's words); the lock order: the register, the decision superseded or ended FOR UPDATE of its own row, the
  release (release scope), the number, the device FOR KEY SHARE (device scope: its model, status, and ownership read there), the insert;
  an IntegrityError or a lock conflict answers RACE; history reasons fixed sentences with Cadence numbers only; equipment's writers never
  write here nor take the register lock: the readers re-derive); `rules.py`'s slice 39 section the one place for needs and words:
  `support_need` (`Need`: NEEDS, on site and connected or able to, the release's security updates or the model's support ended, or the
  OS's while the release's updates are not announced to continue; ENDING, an end announced within 12 months, decidable; EXTENDED,
  information only), `governing_decisions` (the most specific live one: its own, else, for a device of ours, its recorded release's,
  else its model's; a temporary device only its own; past review it still governs and covers nothing: never a fallback) and `covers`,
  `update_responses` (an open update to a supported release (`supported_target`), an update done with the version not confirmed: its
  own state; never updates.open_number alone; read with .get(pk, [])), `device_lines` (`DeviceLines`: the Security tab's), `decision_words`
  ("<response>; <review>. <plan>. <who>.": "Review due since ..." read first once past review; "risk accepted until" only there),
  `scope_words`, `plan_words`, `who_words`, `control_words`, `reason_words`, `review_words`, `end_words`, `decision_state` (IN_FORCE,
  REVIEW_SOON within 30 days, REVIEW_DUE, ENDED; `DECISION_STATE_WORDS`), `scope_devices` / `decision_devices` (`DecisionDevice.words`)
  and `decision_checks` (`Check`, CHECK_*: a support or OS end that holds now on a decidable need and it does not name, worded as known, the model's record now on no separate segment while
  it relies on one, "Governs no device here now: end it", devices joined since, "The plan's date (...) has passed", take out of use still in service,
  the plan owner deactivated; information, never refusals), `needs_attention` (`Attention` of `Heading`s (`ATTENTION_HEADINGS`, empty
  `ATTENTION_EMPTY`) and `AttentionRow`s; links as data, `Link` by URL name: `URLS`, the doors reverse them; Record decision and End only
  for can_decide, a work order's link only with Work orders View) and `badge` (one aggregate query: live decisions past review whose
  scope still holds a device on site, `holds_device_q`, the review headings' condition and their list's `?here=1`; `BADGE_TITLE`; a
  decision to review offers Review and renew while a device it governs has a decidable need, End only when none does and no device's
  need is unknown, else neither; a row links to the model panel only where the panel, with the link's filters, lists just the row's
  devices, any release (`_group_link`)); `decisions_now` / `in_scope` the API's page
  readers; two readings named apart: in scope (the badge, past review, govern no device) and
  governed (responses, joined since, the other Checks); fixed queries as decisions and devices grow. Words: `DECISION_NEVER_WORDS`
  (NEVER_WORDS, which slice 39 gave exempt, waiver, hardened, and deadline, plus "overdue") on every decision door (pages, modals, the
  print, CSVs, toasts, API bodies), a whole device drawer with NEVER_WORDS (it carries Equipment's "overdue for PM"); never "requires" or
  "must"; the who part says the day decided of the decision ("Accepted by ... on <day>, as recorded by <name>"). Release refusals
(`removal_refusal(release, by=)`, `update_release`) name the decision only to its readers, and say "end it first" only to who may end
it (DECISION_LIVE_READ otherwise). Reading any decision (the Decisions tab, a decision's page, print, and CSVs, the API, Needs attention, the badge, the change
  log's Area `device_security_decisions`, also Equipment View) needs can_view_devices; recording, Review and renew, End, and Entered in
  error need can_decide. The nav badge (`reports.services._cyber_badge`, nav_counts' `cybersecurity` / `cybersecurity_hot`) never on the
  Overview's attention list or in an email. `services.removal_refusal` refuses a release with a live decision (`DECISION_LIVE`);
  `update_release` refuses a version change while any decision names it, live or ended (`DECISION_NAMES`). Never in an email, a digest, a
  custom report, the portal, labels, or Scan
- `apps/recalls` Alert (global), AlertMatch (per tenant), matching, openFDA importer; `services.py` dispositions and recall work-order batches,
  `permissions.py` (review at Edit, close/reopen at Approve); `feeds.py` the openFDA fetch shared by `import_openfda` and the Recalls
  screen's Check FDA feed (one fetch per 15 minutes for everyone, kept in apps.jobs' `JobRun`; a check matches only its own facility).
  ECRI import is deferred (license).
- `apps/credentials` Technician, Credential, qualification and coverage services, credential add/renew/sign-off/remove,
  `create_technician` / `update_technician` (the technicians import's), `technician_name` / `name_key` (one rule for matching a name
  written "Last, First" or with a suffix or credential), `add_account_technician` (Invite user's profile: links the imported technician of
  that name rather than adding a second); the one match-and-expiry rule (`credential_names`, `credential_standing`) that
  `qualification` and the survey binder share, and a credential's state as of a past day from its history (`credential_versions`,
  `version_on`, `standings_on`: a renewal after a lapse leaves the days between uncovered)
- `apps/portal` public request form (`/r/<tenant-slug>/`); `notifications.py` the requester's confirmation and done emails (only at the
  facility's work email domains, never the problem text); a label's link opened by a signed-in member who can see that device shows
  "Open <tag> in Cadence" (the user's role is read only for a member, inside the facility's tenant_context), which follows
  `my_work.scan_target` (their one open work order on it, or the device: the iPhone Camera app's way into My work), and nothing else changes
- `apps/facility` Settings: `FacilitySettings` (one row per tenant: portal callback and hotline, the eight maintenance-policy texts,
  KPI targets and the monthly repair budget, whether technicians may take unassigned work, and slice 27's PM on-time window: a kind and
  days per group, checked on the saved row with the change applied, the default PM policy lines following it; slice 36's `fiscal_year_start_month`);
  `fiscal.py` (a fiscal year named by the calendar year it ends: `start_month`, `fiscal_year_of`, `current_fy`, `fiscal_bounds`,
  `fy_label`, `describe`; read the start once and pass it down; nothing stored holds a fiscal year); `services.py` reads (`get_settings`, defaults until first saved), `update_settings`,
  `reset_policy`, `kpi_targets`, `compliance_targets`, `portal_url`, the integration list, risk bands, `set_time_zone` (the facility's
  Tenant.timezone: its today, its clock, its jobs' hour; audited in Tenant history, the change log's "facility" area); `permissions.py`
  (View to see, Edit to change). Named `facility` so it never reads like `django.conf.settings`.
- `apps/reports` report emails (`ReportSubscription`, `subscriptions.py`, the daily `send_report_emails`; self-service only), overview KPIs, the Overview bundle (`overview_page`), attention list, nav counts, `cost_of_service`; the eight Reports
  (`REPORTS` catalog and `run_report` in `services.py`; the numbers in `cost.py`, `fleet.py` (slice 36: `report_replace` on
  apps.capital.priority, `REPLACE_COLUMNS`, `replace_row`: the first seven columns as before, the rest appended), `operations.py`, read-only, `today` passed in;
  the API serves only these); custom reports (`CustomReport`, key `custom-<id>`): `custom.py` declares each source's columns and filters
  (nothing else can be asked for), checks a definition (`clean_definition`), runs it in the database, and creates, changes, and deletes
  one; `permissions.py` (Reports View runs, Reports Edit builds; a source also needs Work orders or Equipment View). `services.find_report`,
  `run_any`, and `csv_filename` are the one lookup by key for the screen, CSV, print page, Schedule, and report emails; `all_facilities.py`
  All facilities (each joined facility's Overview figures read inside it with the person's account there, totals from summed parts);
  slice 25, `survey/` the survey binder (the contract in `__init__.py`: Period, Section, Table with lazy rows, Gap kinds gap / finding /
  check, `binder()`; one module per section: program (slice 32: the approved program evaluations overlapping the period, and a CHECK once
  one was approved and the next is overdue), inventory, maintenance, aem, inspections, recalls, vendors (slice 31, "Contracted
  service and vendor staff": each vendor with its contracts and the evidence on file, the period's vendor jobs judged by
  vendors.rules.evidence_for, a GAP per vendor with jobs nothing covers (never a person), a CHECK for manufacturer-schedule jobs naming no
  engineer; slice 34: each vendor's last review, and CHECKs for a review overdue, vendors under contract never reviewed, and a corrective
  plan past due; Reports, Work orders, Contracts, and Equipment View), incidents (slice 28, Incidents
  View: decisions and reports against the clock; a held device past its PM is a finding, explained), testequipment (slice 35, "Test
  equipment calibration", after repairs: the items, the period's certificates, the work done with test equipment past due, with none on
  file, failed, or found out of tolerance later; GAPs for an item in use past due or with none, a review missing, work added since a
  review; FINDINGs per item and stretch; CHECKs for work with readings naming none and as found not reported; Equipment and Work orders
  View), staff (slice 30: `repairs`, the checks after repair before return to use, by outcome, never a gap; slice 29: inventory lists
  temporary equipment on site with each owner's PM date: a past one is a gap; the other sections count ours); read only, a fixed number
  of queries, history through `core.history._rows`, never requester text; each section needs Reports View plus its areas' View,
  `permissions.SURVEY_NEEDS` / `survey_refusal`; never in REPORTS, so never emailed)
- `apps/web` HTMX UI: one views/urls/forms module per screen (`views.py` Overview, Equipment, Work orders; `views_contracts.py`;
  `views_users.py` Users and Roles tabs; `views_account.py` sign-in, password reset and change; `views_invite.py` accepting an invitation; `views_credentials.py`; `views_recalls.py`; `views_reports.py` with the CSV download and `views_custom_reports.py` the custom report builder (with `reports_custom.py`); `views_settings.py`; `views_pm.py` with `pm_panels.py` for its lower panels; `views_pm_week.py` Auto-assign week; `views_wo_costs.py` and `views_wo_complete.py` the work order drawer's labor and parts and its Mark completed; `views_models.py` the device model drawer (PM program tab, Add model, Edit details, risk score) with `views_procedures.py` and `views_aem.py` for its Procedure and AEM tabs; `views_exports.py` the list CSVs; `views_scan.py` Scan tag (with `static/web/scan.js`, the camera where the browser reads codes); `history_tabs.py` the
  History tab or section of the device, work order, contract, and model drawers; `views_change_log.py` Users and access's Change log
  (with its CSV and print); `views_notifications.py` the account menu's Notifications page; `views_facilities.py` the facility switch and
  an invitation's join page, `views_my_work.py` My work (slice 24: the cards, `wo_waiting`, `from=my_work` answers without the drawer), `views_settings.py`'s `settings_pm_window` the PM on-time window panel (slice 27), `views_wo_complete.wo_keep_open` a repair kept open after a failed check (slice 30, with
  `results_context`'s repair kind and `views_print.repair_checks_print`), `views_vendors.py` the Contracts screen's Vendor staff tab (slice 31, with
  `forms_vendors.py`: the vendors merged by vendor_key with their attestations and engineers' training, `?vendor=` (the binder's gaps link
  here), Add engineer, Change engineer, Add training for one or several of a vendor's engineers, Add attestation, the engineer drawer, the
  contract drawer's vendor line), `views_wo_engineer.py` the work order's Field engineer row, its Change, and the completion modal's
  "Who did the work" select (names only for a vendor's account), `views_wo_finding.py` the work order's "What was found" row and its Change
  (slice 33; the completion modal's finding select is `web/_wo_finding_select.html`, never shown to a scoped user), `views_evaluations.py` Reports' Program evaluation (slice 32, with
  `forms_evaluations.py` and `templatetags/evaluation_tags.py`: the list and next due, New evaluation, the page (figures cut to the
  reader, each narrative field saved on its own, objectives, approvals, actions by status and level, history), the committee's print),
  `views_vendor_reviews.py` the Contracts screen's Reviews tab (slice 34, with
  `forms_vendor_reviews.py`: the vendors and their last reviews, New review, the review's page, its print, the contracted services
  summary print and its batch committee report; the contract drawer's review line),
  `views_testequipment.py` the Equipment screen's Test equipment tab (slice 35, with `forms_testequipment.py`, `_equipment_tabs.html`,
  `test_equipment.html` and `_te_*.html`: the list and its CSV, Add, the item's drawer with its status moves, Record calibration, entered
  in error, the review box and Decide, Work done with it and its CSV, History), `views_wo_test_equipment.py` the work order's test
  equipment (the completion modal's picker, the drawer's section and its Change, the print's line; Equipment View),
  slice 36: `views_models.model_support` the model drawer's Life and support and Record support (with `forms_support.py`,
  `_model_support.html`), `views_settings.settings_fiscal` the Fiscal year panel, `reports_fleet.present_replace` and
  `templatetags/capital_tags.py` the report's panel, `asset_tabs.replacement_outlook` / `expected_life` the device drawer's Costs and
  Overview tabs;
  `views_cybersecurity.py` the Device security screen (slice 37, with `forms_cybersecurity.py` and `_cyber_*.html`: Fleet with its head
  figures and filters, a model's panel, Set devices to this release, Models with profiles and releases, Changes for IT, the CSVs),
  `asset_cyber.py` and `views_cyber_facts.py` the device drawer's Security tab and Record facts;
  slice 38: `views_cyber_updates.py` with `forms_cyber_updates.py` (Open software update, Record update, Confirm version, Entered in
  error, and a release row's Open updates to this release; fields set in `__init__`; one device's routing words `ASSIGN_NEEDS_ONE`), `asset_cyber_updates.py` and
  `_asset_cyber_updates.html` (the Security tab's "Software updates", fixed queries), `cyber_release_actions.py` and
  `_cyber_release_actions.html` (a release row's campaign counts), `views_wo_release.py` (the completion modal's "Software on the
  device", `_wo_release_picker.html`, offered only where rules.release_offer holds, the `shown_software_outcome` marker; the drawer's and
  print's "Software update" line for can_view_devices only);
  slice 39: `views_cyber_decisions.py` with `forms_cyber_decisions.py` and `urls_cyber_decisions.py` (`_cyber_decision*.html`,
  `cyber_decisions.html`, `print_cyber_decision.html`; the Record decision modal says why it cannot be recorded before it is tried with
  decisions.refusal_for, the service's own checks): the Device security screen's Decisions tab (`/security/decisions/`, can_view_devices:
  Needs attention from rules.needs_attention with the HICP sentence, the list by state (In force, Review within 30 days, Review due,
  Ended) filtered by scope, response, and exception number, its CSV), a decision's page (`web:security_decision`,
  `/security/decisions/<number>/` in any letter case; drawer partial and full page: the sentence, reasons, controls, plan, who, the
  devices it governs with their responses, Check lines, supersedes / superseded by, History; Review and renew, End
  (`web:security_decision_end`), Entered in error for can_decide), its print (`web/print_base.html`) and devices CSV, Record decision
  (`web:security_decision_new`, a modal: the scope fixed by the door, `?asset=<tag>` from the Security tab or a rentals row,
  `?release=<id>` from a release row, `?model=<id>` from a model's panel footer (offered only while a device of ours of the model on site waits for a decision:
  `cyber_release_actions._waiting`, the release rows' read on the panel's standings; its address carries the panel's filters) or Needs
  attention; the decision page's `renew_refusal` and `end_answers`, `?supersedes=<number>` for Review and renew; fields set in
  `__init__`; never network_removed: rules.NETWORK_REMOVED_HINT), the Security tab's need, responses, and governing decision
  (rules.device_lines), a release row's live decision or Record decision (`_cyber_release_actions.html`), and the nav item's badge
  (rules.BADGE_TITLE);
  `views_temporary.py` loaners and rentals (slice 29, with `forms_temporary.py`: Add rental or
  loaner, the drawer's temporary section (`asset_tabs.temporary_box`), Change details, Return to owner, Keep it, Vendor loaner arrived,
  Return the loaner; the Equipment "Whose" filter through `forms.equipment_assets`), `views_incidents.py` Incidents (slice 28, with
  `forms_incidents.py`: the list and its CSV, Record incident (from the page, the device drawer, or an open repair's drawer, which it
  adopts), the drawer and its actions; the hold's banner on the device and work order drawers through `asset_tabs.incident_box`, held
  work's chip on My work, "Held" on the PM day panel and Auto-assign week), `views_survey.py` the survey
  binder (slice 25: the page for a period, a CSV per section table and one of every gap, the printable binder), `views_wo_late.py` the work
  order drawer's Why late row (slice 26: Add device's intake choice and Put in use before inspection in `views_equipment.py`, the
  drawer's inspection banner through the `incoming_banner` tag, an inspection's checklist and result in `views_wo_complete.py`, its
  print through `views_print.inspector`), `views_imports.py` Settings' Import data (slice 23: upload, columns, the check and the import a chunk at a
  time by HTMX, the problems CSV), `views_all_facilities.py` All facilities (slice 22; the top bar's facility menu and the account menu's list
  come from the shell context processor; `htmx.FacilityTabMiddleware` reloads a tab left in another facility; `web_view` answers a link
  whose `?facility=` names another facility with a page that offers the switch); `views_print.py` asset labels and the
  work-order print, with `qr.py`; `views_print_sheets.py` PM route sheets and report PDFs), templates,
  `charts.py` (SVG geometry: line, stacked bars, hbars with a benchmark marker, donut), `overview.py` and `reports_*.py` (chart geometry
  and display values for the Overview and the Reports; services never import them), `htmx.py` helpers, shell
  context processor. Drawers and modals are partials swapped into `#drawer` / `#modal-card`; the same URLs render a full page
  when opened directly. List wrappers that re-fetch themselves carry `hx-disinherit="hx-swap"` (a test enforces it).
- `apps/api` DRF viewsets under `/api/v1/`, by session or token (`Authorization: Token <key>`), one module per area, each registering its
  routes (`urls.py` calls them): `base.py` (`TenantViewSet` for a model, `ApiViewSet` over services, `_via_service`: a ValidationError
  becomes a 400 keyed by field), `views.py` devices, models, departments, work orders, Settings; `views_work.py` a work order's labor,
  parts, notes; `views_contracts.py`; `views_pm.py` PM schedule, Auto-assign week, procedures, risk score, AEM; `views_reports.py` the
  Overview, reports, custom reports, report emails; `views_recalls.py` with Check FDA feed; `views_scan.py`; `views_users.py` users,
  roles, technicians, credentials; `views_facilities.py` and `views_all_facilities.py` the signed-in person's facilities and All
  facilities (session only: `permissions.PersonPermission`); `views_survey.py` the survey binder; a work order's late-reason action; slice 26: a device's
  incoming_inspection and inspection_due on create, awaiting_inspection, status_label, its open inspection, and use-before-inspection;
  a work order's inspection_result; slice 28: `views_incidents.py` / `serializers_incidents.py` device incidents (by id or number;
  every service action; no scoped actions), a device's incident_hold (read-only), a work order an incident keeps (never deleted, its
  device and type never changed), the week assignment's on_hold; slice 30: a work order's `repair_check`, `repair_check_label`,
  `checks_reference`, and `completion_checklist` (steps, signature, never-N/A steps, the outcomes this user may send), the transition's
  repair fields (`_repair_words` rewords a missing outcome or results as what to send); slice 31: `views_vendors.py` /
  `serializers_vendors.py` vendor engineers, trainings, attestations (Contracts module, no scoped actions), a work order's
  `vendor_engineer`, `vendor_engineer_name`, and `engineer_choices` (retrieve only), the transition's `vendor_engineer` /
  `engineer_name` / `checks_reference` (omitted keeps), the `vendor-engineer` action; slice 33: a work order's `repair_finding` and its
  label (null for a scoped user), `completion_checklist`'s `findings` / `found_on_pm` / `finding_required`, the transition's
  `repair_finding` (omitted keeps; a missing one names what to send), the `repair-finding` action; slice 32: `views_evaluations.py` /
  `serializers_evaluations.py` program evaluations, read only (the list without figures or text; by id or number, the figures cut to the
  reader); slice 34: `views_vendor_reviews.py` / `serializers_vendor_reviews.py` vendor reviews, read only; slice 35:
  `views_testequipment.py` / `serializers_testequipment.py` test equipment, read only, and a work order's `test_equipment` (Equipment View),
  the transition's `test_equipment` (omitted keeps), the `test-equipment` action, destroy refused with uses; slice 36: a device model's
  support fields by PATCH through update_device_model (refused on POST) and its read-only `support_status`; slice 37:
  `views_cybersecurity.py` / `serializers_cybersecurity.py` software releases and device security, read only (Device security View, and
  Equipment View for devices; never nested in the asset or model records); slice 38: a work order's `software_update` (its record in rules' words: target, state, outcome, sentence; null unless
  can_view_devices; a list's page in one query, `serializers_cybersecurity.updates_of`) and `software_choices` (a single open work order,
  for a user rules.release_offer offers and who may complete it), the transition's `software_outcome` / `software_release` /
  `software_reference` / `software_basis` (refused on every other move and on create; a missing one names what to send), an update
  work order keeps its device, type, and problem and is cancelled, never deleted; `views_cybersecurity.SoftwareUpdateViewSet`
  `/api/v1/software-updates/`, read only (Device security View and Equipment View; ?device= id or tag, ?release=, ?state=; entered in
  error left out; the work order's number, never a link); slice 39: `/api/v1/security-decisions/` (`views_cybersecurity.py` /
  `serializers_cybersecurity.py`), read only, by id or number (can_view_devices; no scoped actions; ?scope=device|release|model,
  ?state=in_force|review_soon|review_due|ended, ?device= id or tag; each decision's fields with their words, the sentence, plan words,
  governed device ids, and Check lines; fixed queries a page through rules.decisions_now), and a device's or a model's destroy refused while a decision names it, in words for
  the reader (a Device security reader reads the decision's number); serializers next to them (`serializers_*.py`). Each endpoint has the door of the screen that does the
  same thing and calls the same service; the API never imports apps.web. `tenancy.py` (`TenantAPIMixin`: the tenant from the session or
  token user, set after authentication and restored once the response is rendered), `authentication.py` (DRF's token check plus the
  deactivated-facility refusal sign-in has)
- `apps/jobs` the daily jobs (`services.DAILY_JOBS`, `FACILITY_JOBS`, `run_daily_jobs`, `is_due`): each facility's jobs run at
  SCHEDULER_DAILY_AT on its own clock, the openFDA import on the server's; `JobRun` (a system table, not tenant-scoped: one row per job
  and local day, per facility for a facility's job, is the lock against double runs; the column is `facility`, never `tenant_id`), the
  `scheduler` / `run_daily_jobs` commands, and `cli.py` (the facility jobs' shared `--tenant` / `--date` options)
- `apps/notifications` emails to staff about their own work: `NotificationPreference` (per user, audited; `services.preferences_for`,
  `wants`, `set_preferences`), `NotificationSent` (each email once: claimed before sending, released if the send failed),
  `assignments.py` (announce a technician's new work order after commit; `batch()` sends each technician one email for a batch,
  `quiet()` sends none, as the seed does), `daily.py` the digest and contract reminders (`send_staff_notifications`, a daily job;
  slice 28: also apps.incidents.notify's reminders, to the `incidents` preference, offered at Incidents Approve). No
  email carries free text a requester typed; every link names its facility (`?facility=`)
- `apps/imports` onboarding imports (slice 23): `ImportRun` (one file's run; its rows kept only until it ends; one import at a time per
  facility), `base.py` (reading a file: encodings, separators, workbooks and NUL refused; columns matched by alias; the `Importer` a kind
  fills in: its columns with their model lengths, a natural key, `apply` one row through the services), `parse.py` (dates, money,
  intervals, choices: an unreadable value raises, never defaults), `services.py` (upload, confirm_columns, process: a chunk at a time,
  the check rolling each back and the import committing each; start_import, discard, expire_stale), `permissions.py` (each kind at its
  screen's level), `kinds/` (devices, contracts, technicians, work_orders; slice 38: the work orders' type words take software and firmware updates and patches as software_update, never a bare "upgrade";
  done ones only). No free text comes over from another system. A pass acts as
  whoever started it (`ImportRun.pass_by`), whoever's browser moves it on; the check rolls each chunk back, so an importer whose rows depend
  on earlier rows reads them (`ctx.rows`) to check a later chunk as the import will; the daily job `expire_imports` ends unfinished runs
- `apps/demo` seed data (Riverside Regional, and Riverside North Campus with Kim linked: the demo shows the facility menu; slice 25: a few
  deliberate survey binder items, such as a late life-support PM with no reason, a new device in service with no incoming inspection, and
  work done during a credential's lapse; slice 28: two device incidents through the services, one closed, one open with its device held
  and its report due in a few work days; slice 29: a rental bed, a vendor loaner for a pump out for vendor repair, a demo unit past due
  back, a returned rental; slice 31: Philips engineers with training on the patient monitor model, one lapsed and named on the closed
  monitor repair, with no Philips attestation: the binder's deliberate vendor gap; TechCare's engineer and its attestation; slice 32: a
  draft program evaluation of the 12 months ending last month, made last, with no random draws; slice 34: a signed-off Philips review
  for the 12 months ending yesterday with a corrective plan, made just before the program evaluation; slice 35: an ESA per technician
  named on their in-house work, a ProSim due soon, a defibrillator analyzer past due, an infusion analyzer found out of tolerance with no
  review; slice 36: a July fiscal year, the beds' life lowered so a few are past it, the dialysis machines' support ended, the
  sterilizers' parts no longer available (the must-act flag), the vital signs monitors' parts end announced, the patient monitors
  supported; no random draws; the beyond-repair bed stays retired; slice 37: profiles for the connectable models, the pumps on two
  fictional firmware releases "(demo)" (the older past its security updates), the monitors' MDS2 on file for an older release, a C-arm
  whose OS support ended, the ventilators' MDS2 requested, the loaner pump recorded, the fleet's "Added" history dated before the demo's
  work so Changes for IT lists only the recent devices; slice 38: Kim's update batch to the pumps' newer release in one department (3 or 4 pumps, due at each next PM, assigned to a
  credentialed technician), one completed as installed and one with the version not confirmed today, the rest open, and the other
  C-arm's patches by the manufacturer remotely recorded by hand on a release added for its model ("7.2 (demo)"); no vendor job, nothing
  on the ventilators, today's work only (no evaluation or vendor review figure moves); slice 39, through decisions.record_decision by the
  CE manager after the updates: SD-<yy>-0001 for the pumps of ours on the older release (release scope, kept in use on the segment their
  profile records with traffic limited, decided a week after their security updates ended, recorded with that day as today; an update
  planned by the last day of last month, Kim its owner, reviewed that day: past review, the badge hot, its Check that the plan's date
  passed, its pumps' open updates beside it) and a number of its own year for the other C-arm (device scope, OS support ended, controls its profile
  allows, a replacement planned in the fiscal year after next, in force); the rental bed T-0101 on the beds' "2.0 (demo)" (security
  updates ended last month), connected (wireless), no decision: Needs attention's rentals row; DEMO-EX- numbers, no link, no CVE)
