# Cadence CE

Multi-tenant CMMS for hospital Clinical Engineering: equipment inventory, work orders, preventive maintenance,
service contracts, recall matching, technician credentials, a public service request portal, and KPIs.

This is the starter codebase generated from the interactive mock in `spec/`. It contains the data model, tenancy,
permissions, lifecycle services, importer, PM engine, KPI math, REST API, admin, portal, and tests, plus the HTMX
web UI for Overview, Equipment, Work orders, Contracts, Recalls and alerts, Incidents (devices suspected in a death or serious
injury, held and reported under the Safe Medical Devices Act), Reports (eight survey-ready reports with CSV
download, the facility's own custom reports, and the survey binder), Users and access (Users, Roles, Technician credentials), and Settings
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

## My work
Technicians start the day on My work (`/my-work/`, first in the nav, and where they land after signing in): their own work orders on
a phone-first page. Repairs and requests come by priority (a recall alert's devices as one row), today's PMs by location, then work
waiting on parts (with its note and how long it has waited), this week's PMs, and the hours they logged today and this week. From a
card they start a repair, log time, complete a PM in one step (the checklist, the result, and their hours in one form; no Start
needed), mark work waiting on parts or resume it, and open the full work order. Scan a label opens the work order of theirs on that
device, or the device; on an iPhone the Camera app pointed at the label's QR code does the same through its request link. Unassigned
work they are credentialed for is offered under "You could take" (Take it), and a work order a technician creates can be assigned to
themselves; a facility can turn taking work off in Settings. Vendor technicians see their company's open work there. The daily
digest links to My work.

## Survey binder
Reports' "Survey binder" (`/reports/survey/`, Reports View) gathers what a Joint Commission, CMS, or DNV surveyor asks Clinical
Engineering for, for a period (the twelve months ending today by default, up to three years), from the records Cadence already keeps:
the maintenance policy and targets set in Cadence, the inventory by risk class, PM completion by risk class (every PM not done by its
due date, why it was late, when it was done after all, due dates moved after they had passed, failed PMs and their repairs, and
life-support or high-risk devices past their PM date today), the AEM intervals in force with their committee approvals and how those
devices have done since, incoming inspections of new devices before first use, recall and alert responses, technician credentials
with any work done without a credential that day (credentials read as they stood each day), and the contracted services with the
evidence on file for the vendor staff who did the work. What is missing is listed with a link to
the record that fixes it: a gap (one record fixes it), a finding (a surveyor will ask; be ready to explain), or a check (housekeeping).
Each section also needs View on the areas it lists, so a role may see part of the binder, and the page says what was left out. Every
table downloads as CSV, the gaps too, and "Print binder" makes one printable document with a cover page and what the binder does not
cover. The binder never shows what a requester typed. `/api/v1/survey/` serves the same.

To give the binder its evidence, a PM that missed its due date can record why (a choice, never free text): in the work order's "Why
late" row, when completing it late, or through the API; once a work order is closed, recording it needs Approve. Add device asks whether
the device is new or already in use here, since only new equipment needs an incoming inspection before first use; imported devices are
marked as imported. On time stays "completed on or before the due date", as on the Overview, and the default PM policy texts now say so.

## Incoming inspections
A new device is inspected before its first clinical use. Add device asks how it arrives: new and waiting for its incoming inspection
(out of service, with an Incoming inspection work order opened and due in 5 days unless you say otherwise; a technician can assign it
to themselves when credentialed, a CE manager picks the inspector or the vendor), new and inspected now (the inspection opens in Mark
completed), or already in use here (existing equipment). Mark completed records the inspection like a PM, step by step with the
leakage reading, on the model's PM checklist or Cadence's incoming checklist, and its result: Passed puts the device in service and
starts its PM schedule from that day; Failed keeps it out of service and opens a re-inspection (never a repair). Until it passes,
nothing else puts it in use: not Return to service, not a completed repair, not a PM. When a device must be used before CE can inspect
it (an emergency, a loaner or rental needed now, one that arrived on the unit already in use), a CE manager or director records "Put in
use before inspection" with the reason; its inspection becomes high priority, due the next day, and the survey binder lists it. The
device drawer says where the inspection stands, and the Work orders page tells a manager how many are waiting for a technician.

## When a PM counts as on time
Settings' "PM on-time window" says when a completed PM counts as on time, once for life support and high risk and once for medium and
low: by the due date (the default), within a number of days after it (up to 45), by the end of the due month, or by the end of the month
after. Every compliance figure follows it: the Overview's PM tiles and trend (with how many PMs are still inside their window), the PM
compliance and technician reports, custom reports, All facilities, and the survey binder; a PM completed after its window asks why it was
late. The schedule keeps the due date: the PM calendar, the PM badge, Auto-assign week, Equipment's overdue filter, My work, and the
work order lists still call a PM past its due date overdue, and a PM finished inside its window takes its next due date from its due
date, so the schedule never drifts. A change counts every PM by the new window, past months included, is recorded with who made it, and
the survey binder notes it; the default PM policy lines follow the window, and Settings warns when a line the facility wrote disagrees.

## Checks after a repair
Every repair records how it ended before its device goes back into use (CMS and the Joint Commission expect safety, operational, and
functional checks after a major repair; the facility's own policy line says "Safety test required before return to service"). Mark
completed on a repair asks "After the repair": checked and ready for use (a short standard checklist: visual, protective earth, touch and
patient leakage with their readings, function, alarms, settings restored; the visual and function checks are never N/A, the electrical
ones are N/A when the repair did not touch the power or patient parts), checked by the vendor (on vendor service, with their service
report number), or not returned to use (beyond repair, going back to its owner: the device stays out of service, ready to retire). A
failed check keeps the repair open and, by default, takes the device out of use. Cadence records the readings and the technician's
judgment, never the limits, which the safety analyzer applies. Nothing puts a device back in service while a repair holds it out: Return
to service waits for the repair's checks, and an incident's Return to use waits for its investigation's. The survey binder's "Checks
after repair before return to use" section lists them. API clients that complete repairs send the outcome (and the checklist's results
when checked): a work order's `completion_checklist` lists what to send.

## Test equipment
The Equipment screen's "Test equipment" tab keeps the department's own safety analyzers, simulators, and meters and their calibration
certificates, apart from the medical equipment: no fleet, PM, or report figure counts them, and their tags share the devices' sticker
space (a tag is one or the other, in any letter case). Each item has its kind, make, model, serial, whose kit it is, its interval, and
whether its certificates calibrate or verify it (a SpO2 simulator is verified). Each certificate from a lab or the manufacturer is
recorded with its day, due day (prefilled from the interval, never more than its month later), number, as found, adjusted, and result,
and is judged by its own dates whenever it was typed in ("on file since" when it was typed after the work). An item reads "Calibration
due Mar 31, 2027", "Calibration was due …", "No calibration on file", or "Failed calibration …"; one out of service returns to use only
with a passed certificate dated on or after the day it is held from (the day it went out, or a failed certificate's own day), and a failed one takes it out of use. A certificate that found an item out of tolerance, or failed it, opens a
review of the work done with it since its last passed certificate: the CE manager (Work orders Approve) records the decision on the list
shown (refused if work was added meanwhile), and the devices' rechecks are counted as they happen. Mark completed asks which test equipment
was used (optional; the technician's own kit first; one past due can be recorded, and the record says so) on in-house work and repairs
the facility checked; each use keeps the day of the work it was recorded on (reopening and completing the work again never moves it),
the work order shows each item's standing on that day, and a use is never taken off once the item has been calibrated on or after that
day. The survey binder's "Test equipment calibration" section lists the items, the certificates, and the work done with
test equipment past due, with none on file, failed, or found out of tolerance later, and checks work with readings that names none.
Deferred: the import, labels and Scan, reminders, in-house verification against a reference standard.

## Repair findings
Every repair records what it found, as one choice: no problem found, use error, power / gas / network / room conditions, damaged in
handling, battery, accessory or cable, software or firmware, wore out, PM could have caught it, caused by earlier service, or (on vendor
service) not stated by the vendor. The codes follow the evidence-based maintenance codes clinical engineering uses, and "use error" is
FDA's and IEC 62366's word. Mark completed asks it above the resolution, with a hint for each choice and the device's recent PM and
repairs as evidence; a repair opened from a failed PM never reads "PM could have caught it" (the PM caught it). A vendor's own account
records only what it sees on the device, never use error or the facility's judgments, and never sees the finding; an incident's
investigation never records a use error (that stays in the facility's event report). The work order's "What was found" row corrects one
or records it on a repair completed before findings existed. Use errors, damage, accessories, power or room conditions, and no problem
found are not device failures: MTBF counts failures beside all repairs, the AEM evidence and the program evaluation show failures,
PM-related failures, and use-related repairs with how many repairs have a finding recorded, and the survey binder lists each repair's
finding. Custom reports' work orders source can show and group by it.

## Loaners and rentals
Equipment on site that the facility does not own (rentals such as specialty beds or surge pumps, vendor loaners sent while one of ours
is out for repair, demo or evaluation units) goes on the inventory while it is here: Equipment's "Add rental or loaner" (or "Vendor
loaner arrived" on the device it stands in for) records whose it is, the owner, the agreement, PO, or RMA number, when it arrived and is
due back, and the owner's PM date from its sticker. It is inspected before first use like any new device, on a short rental checklist that
checks the owner's PM label (a unit whose owner's PM is past is not passed), unless it was already on site when entered. Its owner
maintains it: Cadence opens no PMs on it, a repair or recall work order goes to the owner as vendor service, and it never counts in PM
compliance, AEM evidence, cost of service, uptime, MTBF, or replacement planning (it does count in work order and recall figures). Return
to owner records how it was cleaned and what was done about patient data, and the device then reads "Returned to owner"; Keep it (a CE
manager) makes a bought demo or rental ours, with its price and a first PM. A unit that comes back later is entered again under the tag on
it then, and Cadence names the earlier record by its serial number. The Overview lists units past their due back date and loaners whose
device is back; the survey binder's inventory section lists what is on site with each owner's PM date.

## Device incidents
When a device in Cadence may have played a part in a patient's or a staff member's death, serious injury, or serious illness, the
Incidents screen (or Record incident on the device, or on the repair request it was reported as) records it in a few choices: the day
it happened, the day the facility's clinical staff first knew, the outcome ("not known yet" counts as serious until decided), and who
was affected (patient, staff member on duty, visitor, no one), never who they are. The device is held as evidence: it goes out of
service, nobody can start or complete any work on it but its investigation (the request it was reported as, or a repair the incident
opens), and only the incident releases it. Other parts of the system (a pump's channel module) can be held on the same incident, and a
device sent to the manufacturer stays held until it comes back. The Safe Medical Devices Act's clock (21 CFR 803) is counted for you:
10 work days (weekdays, Federal holidays skipped) after clinical staff first knew, with the people who decide (Incidents Approve) emailed
when one is recorded and as the due date nears. The decision asks the regulation's question (may the device have caused or
contributed, use error included?) with a reason from a short list, who decided, and the facility's event report number, where the
narrative and the deliberations live: Cadence keeps no free text about an incident. Reports sent to the FDA and the manufacturer are
recorded with the 803.3(x) report number; the list's year filter and CSV give CE's share for the facility's annual report (Form FDA
3419). The survey binder's Device incidents section shows them, with what is late or missing. Technicians record and hold (Incidents
Edit); CE managers and directors decide, record reports, release, and close (Incidents Approve); clinical requesters and vendors never
see an incident, only that a device is "Held by Clinical Engineering". A facility made before this slice gets Incidents on its
standard roles at their defaults; set it for any custom role on the Roles tab.

## Contracted service and vendor staff
CMS asks a hospital to show how it makes sure its contractors use qualified people, and surveyors ask for the list of contracted
services; neither asks for every vendor engineer's certificate. The Contracts screen's "Vendor staff" tab keeps the evidence, by vendor:
the vendor's written attestation (a yearly letter or a contract clause stating that everyone who services the listed devices for you,
on site, at its depot, or remotely, is trained on them, with the training records available on request: its reference, the names the
vendor goes by on your work orders and contracts, the devices it covers, and the period it states), and, where you keep them, each
engineer's training on file (a certificate or the vendor's record for named device models or one manufacturer's product category, with
its dates). A renewal is a new record; one entered in error is removed (its history keeps it). Completing vendor work asks who did it,
as the vendor's report names them (optional; a new name is added under that vendor), with the report's number; the work order's "Field
engineer" row changes it once the report arrives. The survey binder's "Contracted service and vendor staff" section lists each vendor
with its contracts and what is on file, and judges every vendor job on the day it was completed: the named engineer's training on that
model, else the vendor's attestation (or the engineer's own company's), else nothing on file, which is a gap for that vendor; on
imaging, radiologic, or laser equipment kept on the manufacturer's schedule, a job naming no engineer is a check. Cadence keeps the
documents and never judges anyone's competence; badges, immunizations, and background checks belong to the facility's vendor
credentialing system. Contracts View sees the tab, Contracts Edit changes it; a vendor's own account sees only its company's engineers'
names on its work.

## Vendor reviews
The Contracts screen's "Reviews" tab keeps the facility's yearly review of each contracted service vendor (CMS asks that every contracted
service is evaluated through the quality program and the governing body kept informed; surveyors have cited board minutes that named an
"annual contract review" with no evaluations behind them). It lists every vendor with a contract in force, recent jobs, or a review,
with its last review and when the next is due (a year after the last, or the contract's end first). A review covers 12 months that have
ended (a contract's anniversary works) and may name several spellings of one vendor. Cadence takes the figures from its own records of
the vendor's work: its contracts from their history (cost prorated over each version's days), jobs by who completed them, time with the
vendor from the day the job was handed to it, PMs assigned to it on time (late ones by whose reason), repeat repairs that were real
failures, documentation, staff evidence, and cost. It never computes uptime or response from its dates: the reviewer records the
vendor's own reported figures with the report's reference. The reviewer rates six criteria, the overall result, and the decision (renew,
renew with changes, a corrective action plan with its due date, replace, bring in-house), signs it off (on the figures and the ratings
the sign-off shows: a change saved meanwhile is refused), and later records the plan's result and the committees it went to; the contracted services summary prints every vendor's last review for the governing body, and one
committee meeting is recorded for several reviews at once. The survey binder's vendors section shows each vendor's last review and checks
a review overdue, vendors under contract never reviewed, and a plan past its due day. Reviews need Evaluations (the module of Program
evaluation, now named Evaluations) with Contracts View, in the change log too, where the reviewer's summary reads only as edited; a
vendor's own account never sees its review.

## Program evaluation
Reports' "Program evaluation" drafts the yearly evaluation of the medical equipment program that hospitals present to their environment
of care or safety committee and their quality program (DNV and ACHC expect one; The Joint Commission's 2026 standards no longer require
it of hospitals, and Cadence never says which accreditor does). Choose a 12-month period that has ended; Cadence takes the figures for
it: scope, PMs on time for life support and high risk, for medium and low risk on the manufacturer's interval and on AEM (each by
quarter, against the facility's own targets as they stood when the period ended), incoming inspections, repairs and their checks, how
each AEM model did, recalls, incident counts (Incidents View only), work during a credential lapse, vendor jobs with nothing on file,
and repair spend against the budget, with the previous period beside them and the binder's gaps as an appendix. Use errors and
no-problem-found are not recorded in Cadence, and the evaluation says so. CE writes the rest (a summary, scope and changes, AEM safety
and effectiveness, what went well and what needs improvement, capital and staffing, cybersecurity, the written plan reviewed), reviews
last year's objectives and sets next year's, refreshes the figures while it is a draft, and sends it to committee, which freezes it for
the meeting; the committee's approval is recorded from its minutes (later ones as it goes on to quality oversight or the governing
body). An approved evaluation is never edited: an amendment is its next version. The printed copy carries the approvals or signature
lines. Program evaluation is a module of its own in Roles: the CE manager drafts and records approvals by default, the director too,
Finance and quality reads it.

## Replacement planning
Reports' "Replacement planning" ranks our devices in use for the capital committee, as a recommendation, never a decision (no US rule
requires a replacement plan; AAMI EQ56 recommends planning replacements when parts become hard to get, and published methods warn against
age alone). Each device scores 0 to 100 on five things, each shown with what it was read from: age against the model's expected life
(30), the manufacturer's support (25: parts no longer available, support ended, sale ended, an end announced, or supported and checked in
the last 12 months), failures a year (20: repairs whose finding is a failure, or none recorded, over up to 36 months), service cost a
year against the replacement estimate (15: every labor and parts line recorded, never recall work, and the device's share of its service
contract, as the Overview's cost of service reads them, which counts recall work too; record a visit the contract covers at $0 to keep it
out of both), and a confirmed recall not yet done on the device (10), times a multiplier for the model's risk class (1.0 life support to
0.85 low). What Cadence does not know is never scored as 0 nor spread over the rest: its weight shows beside the score as points unknown
("+35 unknown"), and the first year (or since the device was installed) reads "too little history" for failures and cost. Condition is
shown in the drawer, never scored. The estimate is the model's list price (the day it was set), else the device's acquisition cost (a
historical price), with no markup. Flags stand apart from the score: must act (parts no longer available; kept out of use after its last
repair) and for review (support ended; a confirmed recall). Each device is suggested for a fiscal year (the current one when it must act,
else when its life, support, or parts end; "suggested", never "due"), and the report adds fleets by model, the outlook for this fiscal
year and the four after it, what the scores do not know (no install date, no estimate, where a model's expected life comes from, support
not checked) with links, and the devices not ranked. Its CSV keeps the first seven columns, "Repairs, 6 mo" became "Failures, 36 mo", and
the rest are appended (each component's points, flags, support, suggested fiscal year, the install date and acquisition cost the devices
import reads back, and the rule's version, "v2": age 30, support 25, failures 20, service cost 15, recall 10, times 1.0 / 0.95 / 0.9 /
0.85 by risk class); the API's `/api/v1/reports/replace/` rows follow the CSV by position. A device model's PM program tab records where
its expected life comes from and the manufacturer's notice (no longer sold from, support ends, parts available until, the notice's
reference, the day CE checked) through Record support (Equipment Edit); the device drawer's Costs tab shows the device's score and how it
was scored, and its Overview the expected life's fiscal year. Settings' "Fiscal year" sets the month it starts (a fiscal year is named by
the year it ends; a change re-buckets the outlook and moves nothing stored). Capital requests, committee decisions, budgets, and the link
from a retired device to its replacement come next.

## Device security
The "Device security" screen keeps the medical device security register: what each device model can do and what its manufacturer
discloses, release by release, and what the facility knows about each device's software and connection. HHS 405(d)'s Health Industry
Cybersecurity Practices (HICP 2023) recommends keeping the medical device inventory, with at least major version information, in the
CMMS; no US law, CMS rule, or accreditor asks for a per-device record, and Cadence never says one does. Each model has a profile (all
versions, or version not stated) and its software releases as the manufacturer writes them, each with the operating system (family, name
with edition, the OS vendor's last day of support), the last day of the manufacturer's security updates for it, the day support was
checked, the manufacturer's disclosure (MDS2: on file with its form, document number, release day, and https link; requested; or none
from the manufacturer), and a few of its answers in words (can store or display patient data, how it connects, remote service, who may
install updates, SBOM). A release overrides its model's profile by group (support, MDS2, disclosure), never field by field, and a device
whose release is not recorded reads the profile as "the model's disclosure, version not stated". The profile also keeps the facility's
own arrangement: who installs updates here (Clinical engineering, IT, the manufacturer, the department that uses it) and whether the
model's connected devices are on a medical device network segment. Each device has the release it runs and since when, how it is
connected here when that differs from what its model discloses, and the day it was checked and by whom: one at a time from the device
drawer's Security tab (Record facts), or one model's devices at once from its row ("Set devices to this release", up to 500, optionally
one department's). A device whose model changes loses its release (its history says why); a release entered in error stays in the
history, cannot be named while a device runs it, and comes back if the version is added again.

Every device reads two standing lines, never merged, about the dates on file and never about the unit: the manufacturer's ("Security
updates end Mar 31, 2028 (announced)", "Security updates ended after Mar 31, 2028", or, when the release has no date of its own, the
model's manufacturer support from Replacement planning) and the OS vendor's ("The OS vendor's published support for <the operating system
as recorded> ends Jan 31, 2030", or "ended after ..."; extended support bought for one unit is not recorded yet). Fleet lists one row per
model with devices on site, ours and temporary alike (devices, connected here, by release with "not recorded", past security updates, OS
past its vendor's support, the MDS2 and which version it is for, patient data, who installs updates, segment), with the facility's counts
above it: models that can connect with no MDS2 on file (with how many of them are requested), devices that can connect on a release past its security
updates, devices that can connect with no release recorded, and models with connected devices not on a separate segment or whose segment
is not recorded; a model's row opens its devices (tag, location, release, since, connected, the two lines). Models keeps each model's
profile and releases. Changes for IT lists the devices whose model can connect or handle patient data (or that are connected here) added,
marked missing, retired, or returned to their owner in the last 7, 30, or 90 days, so the IT department can follow the network's medical
devices. Fleet, a model's devices, and Changes for IT download as CSV with the columns on screen.

Never kept: device credentials (no field can hold one), network identifiers (IP, MAC, hostname, VLAN, ports), SBOM contents, the MDS2
file itself, advisory text, and free text of any kind: references are tokens and links are https to a public host name (never an IP
address, a port, a single-label name, or a .local, .lan, .internal, .home.arpa, or .corp name, and never with a password, token, or
signature in them: an MDS2's link and an advisory source's alike), answers are choices. None of it reaches an email, a report email, a custom report, the device's own CSV or API record, the
portal, labels, or Scan. The register never calls a device or a release "secure", "compliant", "certified", "patched" or "unpatched",
"vulnerable", "unsupported", "mitigated", "protected", "safe", "at risk", "exposed", "exploitable", "remediated", "affected", "fixed",
"exploited", "zero-day", or "uncontrolled" (an advisory's statuses are its issuer's words, never Cadence's), never "end of life" for an
end of support, nor "high risk" or "critical" in its own voice; an MDS2 answer is named by its question in words, never by a
form's question number. A security decision is never an exemption or a waiver, a device never "hardened", and a review date never a
"deadline" or "overdue".

Device security is a module of its own in Roles: the director Full, the CE manager Approve, technicians Edit, Finance and quality,
requesters, and vendors None. View sees the register; Edit records models' profiles and releases and devices' facts; Approve marks a
software update entered in error, records security decisions, and closes advisories (all below). Every list naming devices by tag or location (Fleet's devices, Changes for IT, the drawer's tab, their CSVs) also needs
Equipment View; the Models tab needs Device security View alone. A vendor's or a requester's account never sees it, whatever its levels
(on a custom role that sees only its company's or unit's share, Device security has no effect). For the IT department, make a custom role
"IT security" with Device security Edit and Equipment View: it records the facts without adding, editing, or retiring devices. For IT to
record its exceptions as decisions, raise Device security to Approve; with Equipment View it then also marks updates entered in error.
Add Work orders View to follow update work orders. With Device security Edit, IT records advisories, sets and confirms their models,
and marks them not applicable. To close them and record decisions, raise Device security to Approve, with Equipment View. Upgrading:
every custom role made before this release reads None for Device security, so a custom role that administers users cannot invite anyone
into a role holding it (a technician's, for one: nobody gives more access than their own) until a director raises its Device security
level. Software updates, security decisions, and security advisories are below. The version recorded at every completion, the import,
the survey binder's section, and the evaluation's figures come next, then the CISA feed.

## Software updates
A software update is work of its own (FDA's 2016 postmarket guidance calls a routine cybersecurity update an enhancement, "not
considered a repair"; HHS 405(d)'s HICP keeps security maintenance as its own work), so it has its own work order type, "Software update"
("Update" in the lists). It is opened only from the Device security screen: "Open software update" on a device's Security tab (one
device: a release of its model to install, or the manufacturer's patches with the list's reference and its month or revision), or "Open
updates to this release" on a release's row in Models (one work order per device of that model on site whose recorded release is one you
choose, or not recorded, optionally in one department, up to 100 at once, due on a day or on each device's next PM, priority normal or
high). New work order never offers the type, the API's create refuses it, and the work order import brings in done updates as history
only (an open one is refused). Each update's problem is one of two fixed sentences with no version or reference in it. When the
person opening them may assign work, who they go to follows the model's "Who installs updates here?": clinical engineering's go to the
technician chosen (or stay unassigned for a technician to take); the manufacturer's, and every rental's, loaner's, or demo unit's, to the
vendor's service; IT's and the department's stay unassigned, and the result says who installs them. A device held as evidence for an
incident, or with an update already open, is skipped (the batch says which and why); retiring a device or returning it to its owner
cancels its updates not yet started (one in progress is completed or put back to open first).

What an update did is recorded when it is completed: Mark completed's "Software on the device" asks whether the release was installed
(and which one the device runs now), patches were applied (the version as recorded; the manufacturer's list and its month or revision),
the device was already on the update (nothing installed), or "Not confirmed now". The device's release, since when, and the day checked
follow (never backwards: a record dated before the device's latest facts is kept as a record, and says so). A vendor's job, or one
completed with the version not confirmed, is confirmed afterwards with "Confirm version" on the Security tab (dated the day it was
completed), and an install with no work order (by the manufacturer remotely, automatically, by IT, or by the department that uses it) is
recorded with "Record update". The basis for each install is recorded too: the manufacturer's validated patch list (patches only), the
manufacturer's release, service notice, or field change, or not stated. An update states what the person recording it says it did; Cadence
never infers what a device ran before it (the device's History shows each change of its release). The Security tab lists the device's updates newest first, each in
a sentence ("5.6 installed Oct 20, 2026", "Patches on the manufacturer's validated patch list applied Oct 14,
2026, version unchanged (7.2); ref ICAR-2026-09; installed by the manufacturer remotely", "Completed Oct 20, 2026; version not
confirmed"); a release's row in Models counts its updates ("Updates to 5.6: 3 open, 1 done, 1 not confirmed"). "Entered in error"
marks an install recorded by hand as a mistake, or clears a work order's recorded outcome so it can be confirmed again; the device's
facts stay as they are (correct them with Record facts). A release cannot be marked entered in error while an open update is to install
it. Drug libraries are not recorded here. An update's labor and parts are service cost, never a failure; the survey binder reads an
update as work (the technician's credential for the device, a vendor's staff evidence).

Who may do what: recording what an update did, Confirm version, and Record update need Device security Edit and Equipment View; opening
updates also needs Work orders Edit, and assigning them Work orders Approve (without it they stay unassigned); Entered in error needs
Device security Approve and Equipment View. For the IT department's custom role "IT security" (Device security Edit and Equipment View),
add Work orders Edit for it to open updates, and Work orders Approve to assign them. A vendor's or a requester's account never records
what an update did (a vendor's completion reads "Completed by the vendor ...; version not confirmed" until the facility confirms it) and
never sees the versions.

Never shown outside the register: an update's target, versions, references, and sentences are read only with Device security View and
Equipment View. The work order list, its CSV, the drawer and print (whose "Software update" line is for those readers only), My work,
the Overview, Scan, labels, the portal, emails, custom reports, the API's work order for anyone else, and the status history and
resolution show the type and the fixed problem only. Upgrading: every custom role made before this release still reads None for Device
security, so its users see update work orders without their targets, and cannot record what one did, until a director raises its
Device security level.

## Security decisions
When a device stays in use after its manufacturer's security updates end, or after its operating system vendor's support does, HHS
405(d)'s HICP 2023 recommends documenting mitigating controls and a timeline for resolving the risk, through the organization's risk
acceptance or exception process; no rule requires it, and Cadence never says one does. A security decision records that for one device,
for a model's devices of ours on one release, or for every device of ours of a model: the reasons (the manufacturer's security updates,
support, or parts, the OS vendor's support: ended, or announced to end within 12 months), the decision (Keep in use with controls, Keep in use without
added controls, or Take out of use), the controls it relies on from a list (a medical device network segment, traffic limited to the
ports and hosts it needs, no internet access, a connectivity feature turned off, the manufacturer's published mitigation, vendor remote
access off or attended only, a malware scan when serviced, application allowlisting, unused ports and services turned off, network
monitoring, physical access and removable media controlled, or controls recorded in IT's system with its change number), a plan (wait
for the manufacturer; an update planned by a day; a replacement planned by a day, worded by the fiscal year it falls in; return it to its
owner, for a rental, loaner, or demo unit; or none) with an optional owner, who accepted it (the CE leader, IT security or the CISO, a
committee, department leadership, or the authorizing official), IT's exception number, the day decided, and a review date (by default
12 months after the day decided, 3 to take out of use; for a rental, loaner, or demo unit its due back day when that comes first, else 3
months; at most 36 months, 12 to take out of use: Cadence's choices, not a rule). IT's exception stays in IT's system of record: Cadence
keeps its number, never its text, and a reference is never an address (an address with a port, range, or prefix too), a MAC in any
layout, alone or within a reference (a ticket number of digits in a MAC's layout, or a UUID, is taken), a VLAN, or a port. A device recorded "Not connected here" (Record facts) needs no decision about its support, so "taken
off the network" is not offered as a control there (an advisory's decision offers it: below), and the segment is refused as one while the model's record says its devices are on no separate
segment. A decision is never changed: Review and renew records the next one and ends it as superseded; End (no longer needed: its devices
updated, replaced, retired, or returned, or each under a decision of its own; recorded as "No longer needed", never why) and Entered in
error end one. Retiring, returning, or updating a device never touches a decision: the
screens read what holds now.

The most specific decision in force governs each device: its own, else its release's, else its model's; a rental, loaner, or demo unit
only its own (a release or model decision describes where the facility put its own devices). Past its review date a decision reads
"Review due since ..." first and covers nothing, and a broader one never takes over. An open update to a release still supported, and
an update done with the version not confirmed, are shown beside the decision as answers of their own. A decision reads as one sentence:
"Keep in use with controls (2: traffic limited to the ports and hosts it needs; vendor remote access off, or attended only); review by
Oct 21, 2027. Plan: replace, planned for FY2029. Accepted by IT security or the CISO on Oct 21, 2026, as recorded by Rob Feldman; IT
exception DEMO-EX-1042." (the day is the decision's: one recorded later, for an exception decided earlier, says so) "Risk accepted until" appears only in the sentence for Keep in use without added controls, and never once past
its review.

Device security's Decisions tab opens with Needs attention: devices of ours past their support, connected or able to connect, with no
decision recorded and no update to a supported release open (per model and release, with Record decision); rentals, loaners, and demo units past theirs (ask the owner to update it,
or return it); updates done with the version not confirmed; ends announced within 12 months (information); decisions past their review
date or to review within 30 days, each whose scope still holds a device here (a decision none of the devices it governs needs any more offers
End, not Review and renew; while a device's connection or support is not recorded, or the fact one of the decision's reasons rests on (the
security updates' end, the OS support), neither: its page says what to record first; the count
opens the list narrowed the same way), or governing no device here now (End); and Check lines: a support
or OS end that holds now and the decision does not name, the model's record no longer giving the segment it relies on, devices joined since, the plan's date passed, a device still in service after a
decision to take it out of use, the plan owner's account deactivated. With nothing listed it reads "Nothing listed from what is recorded
here." Below, the decisions by state (In force, Review within 30 days, Review due, Ended), filtered by scope, decision, or IT exception
number, download as CSV. A decision's page shows its sentence, its reasons with their dates, controls, plan, who accepted and recorded it,
its review, the devices it governs (tag, serial, model, department, connection, release, and what answers each), its Check lines, the
decision it superseded or that superseded it, and its History; Review and renew only while it would be taken (else why not), End the main
action when nothing it governs needs a decision; the reasons Record decision offers and requires, and "What holds now", come from the
devices the decision would govern (never one a more specific decision governs), and a release or model scope whose devices needing a
decision all have a more specific one is refused; it prints for IT and the committee ("As recorded in Cadence; IT's
exception record is the system of record") and its devices download as CSV. Record decision opens from a release's row in Models (that
release's devices of ours, offered while one of them has no decision governing it and no update answering it), from a model's panel ("Record decision for every <model> of ours", offered while a device of ours of the model on site has a need, no
decision governing it, and no update answering it, whatever its release, and keeping the panel's filters), from a device's Security tab (that device), from Needs attention, and as Review and renew (prefilled,
the exception number blank, a plan no longer offered starting at No plan recorded, and the panel's filters kept); a review date already
passed is said once, and only when the service would take the decision; the Security tab shows the device's need, what answers it, and the decision that governs it. Device
security's nav item carries a badge counting the decisions past their review date whose scope still holds a device here. Decisions never reach
an email, a digest, the Overview's attention list, a custom report, the portal, labels, or Scan.

Who may do what: reading decisions (the tab, a decision's page, print, and CSVs, the badge, the API, and their history in the change log)
needs Device security View and Equipment View, since a decision can name a device; recording, Review and renew, End, and Entered in error
need Device security Approve and Equipment View. A vendor's or a requester's account never sees them. A release a decision is in force for
cannot be marked entered in error until the decision ends, and a release any decision names keeps its version (add the new version
instead); the refusal names the decision only to someone who may read decisions, and tells only someone who may end it to end it first.
Each row of Needs attention links to the model's panel only where the panel lists just the row's devices (else to the device, or not at
all).

## Security advisories
HHS 405(d)'s HICP 2023 recommends taking in security advisories, finding the devices they concern, and choosing a response with the
manufacturer; no rule requires it, and Cadence never says one does. Cadence receives no advisory by itself: Device security's Advisories
tab records each one your team takes in, and keeps no advisory text. An advisory has a number (AD-26-0003, by the year received), the day
received, its CVE IDs (pasted as CVE-YYYY-NNNN, separated by spaces, commas, or new lines), and its sources: who published it (a CISA ICS
medical advisory, a manufacturer's bulletin or customer portal notice, an FDA safety communication, an ISAC alert, or other), its
reference as the publisher prints it, a link, the day published, and its latest revision (the revision or the day updated, and what
changed: products or releases, vulnerabilities added, a fix now named, mitigations, other details). Record only the reference an ISAC
lets you share inside the facility. A CVE ID or reference already on another advisory is shown before saving, with "Add as a source of
AD-…" or "Record as its own advisory"; the same publication twice is refused.

For each model of the facility's catalog it names, Set models records which releases it names (the releases listed, every release, or
releases not stated) and what it offers, as the advisory states it: a release from the manufacturer (with the release it names as the
fix), the manufacturer's validated patches (with the patch list it names), mitigations only, no fix yet, or no fix planned. A version
not on file is added from the same form, and "Models recorded with an operating system" narrows the list for an operating system's
advisory. Until "These are all the models it names that are here" is confirmed, it reads "Models to confirm". A revision that changes
its products, vulnerabilities, or fix asks for the models to be confirmed again (and reopens one marked not applicable); one that changes
only mitigations or other details does not. Which devices it concerns comes from each device's recorded release, never stored: every
device on site of a named model, ours and rentals, loaners, and demo units alike, reads a line ("On 5.2 (demo), a release the advisory
names", "Release not recorded: it may run a release the advisory names", "On 5.6 (demo), the release the advisory names as the fix", "On
5.4 (demo), a release the advisory does not name", "The advisory names every release of the Cios Spin", "The advisory names the Centrella
without stating releases"), and a device on a release it names, or with no release recorded, needs a response, connected or not.

A response is an update open to the fix or to a release it does not name, the manufacturer's patches recorded, or a decision. An update
is judged by where it leaves the device: one to a release the advisory names is shown but answers nothing. Patches count when recorded on
the list the advisory names (on any day) or, when it names none, since it was received; patches naming another list are a state of
their own, as is an update done with the version not confirmed. A decision for an advisory's devices is recorded as a security decision
is (above), for one device, a release's devices of ours, or every device of ours of a model, with no reasons: keep in use with controls
(where "Taken off the network (used standalone)" is offered, once every device it covers is recorded "Not connected here"), keep in use
without added controls, take out of use, or "Decided the part it names is not in use here" (no plan; a feature turned off as a control
is keep in use with controls). A rental, loaner, or demo unit is covered only by its own. Each need has its own decisions: a support
decision never answers an advisory, nor the reverse. Record decision for a scope that already has a decision of another kind starts
from its controls and plan; who accepted it and IT's exception number are recorded for this advisory. The Decisions tab lists both
kinds, with a Why filter and column ("Support ended, or its end announced", or the advisory's number), and a decision's page, print,
and CSVs name its advisory.

An advisory reads "Models to confirm", then, for readers of devices, "Open: 4 devices with no response recorded", "Open: 1 device under a
decision past its review date", "Open: 1 device with patches naming another list", "Open: 1 update done, version not confirmed", "Open:
12 updates open", or "Open: ready to close" (a reader without Equipment View reads "Open"). Close needs its models confirmed and every
device that needs a response answered, and says which are not. Not applicable here takes one of three reasons, each checked against the
records: it names no model in this facility's catalog; no device of the models it names is recorded on site; no device is recorded on a
release it names. Both are checks at that moment: a device named since is listed again ("Closed or not applicable, devices named
since"), and a closed advisory whose models need confirming after a revision says so. Reopen and Entered in error correct one (Entered in
error is refused while a decision under it is in force).

The Advisories tab opens with Needs attention: advisories with models to confirm; devices of ours on a release an advisory names, or
with no release recorded, with no response (per advisory and model, with Record decision); each rental, loaner, or demo unit with no
response (ask its owner to update it, or return it); updates done with the version not confirmed; patches naming another list;
advisories closed or not applicable with devices named since; closed ones with models to confirm since a revision; releases added since
the models were set; and advisory decisions past their review date (opening the Decisions tab narrowed to them). With nothing listed it
reads "Nothing listed from what is recorded here." Below, the advisories filtered by status (open by default), models to confirm, or a
CVE ID or reference, with their counts, download as CSV. An advisory's page shows its sources, the models it names with their counts,
each device's line and what answers it (Record decision, Open software update, and Open updates to the fix where they can act), its
Checks, the decisions recorded for it, and its History; its devices download as CSV for IT (tag, serial, model, department, whose,
connection, release, line, response, decision, IT exception number, review by). A device's Security tab lists the advisories naming its
model. Device security's badge counts the advisories with models to confirm (for Edit) beside the decisions past their review date.

Who may do what: Device security View reads advisories, their sources, the models and releases they name, and counts of devices by line
and of updates; every device line, response, decision, and the devices CSV also need Equipment View. Edit records advisories, sources,
and revisions, sets, confirms, and removes models, marks one not applicable, and reopens one marked so; Approve with Equipment View
closes one and records decisions; Approve reopens a closed one and marks one entered in error. A vendor's or a requester's account never
sees them. A release an advisory names cannot be marked entered in error, nor its version changed (add the new version instead).
Advisories never reach an email, a digest, the Overview's attention list, a custom report, the portal, labels, or Scan.

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
who can edit contracts), and, for the people who decide whether a device incident was reportable, when one is recorded and as its
report's due date nears. The digest and reminders go out with the daily jobs; nothing is ever sent twice. These emails never carry
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
- device incidents: recording, holds, the decision, reports, release, and closing (`views_incidents.py`);
- vendor engineers, their training on file, and vendors' attestations (`views_vendors.py`: Contracts View reads; Edit adds, changes, and
  removes a record entered in error; never a vendor's or a requester's account);
- a vendor job names the vendor's engineer (slice 31): the transition to completed takes `vendor_engineer`, an id from the work order's
  `engineer_choices` (GET the work order), or null when the vendor's report names nobody; left out, the engineer already named stays.
  Or send `engineer_name` for someone else, added under the work order's vendor. Afterwards, POST
  `/api/v1/work-orders/{id}/vendor-engineer/` (facility staff only; Approve once closed). `checks_reference`, the vendor's report number,
  is taken on any vendor job (left out, the number on file stays) and refused on in-house work;
- a repair is completed with how it ended (slice 30): POST `/api/v1/work-orders/{id}/transition/` with status completed and
  `repair_check`: `passed` with `results` for every step of the work order's `completion_checklist` (GET the work order for the steps,
  their signature, the steps never N/A, and the outcomes you may send), `vendor_report` with `checks_reference` (the vendor's service
  report number; vendor service, facility staff only), or `not_returned`; a repair sent with no outcome, or passed with no results,
  answers a 400 naming what to send;
- program evaluations, read only (`views_evaluations.py`: the list, and each with its text, objectives, approvals, and the figures your
  access allows);
- vendor reviews, read only (`views_vendor_reviews.py`: the list, and each with its ratings, decision, plan, committee reports, and the
  figures when you hold Evaluations, Contracts, Reports, and Work orders View);
- a device model's life source and manufacturer support (slice 36): `life_source`, `sale_ended_on`, `support_ends_on`, `parts_end_on`,
  `support_notice`, `support_checked_on` with PATCH (Equipment Edit; refused on POST: set them once the model is added), and a
  read-only `support_status` ({"key", "words"} for the facility's today); Settings' `fiscal_year_start_month` (1 to 12);
- test equipment, read only (`views_testequipment.py`: Equipment View, never a vendor's or a requester's account): the list with each
  item's standing in words, filtered by `?standing=past_due|due_soon|no_record|failed`, `?status=`, `?kind=`, `?held_by=`; one item by id
  or tag with its calibrations and tolerance reviews. A work order reads the test equipment it recorded (`test_equipment`, each item's
  words on the day of the work, for Equipment View); the transition to completed takes `test_equipment`, a list of item ids (`[]` for
  none; left out, what is recorded stays, except that a completion with `repair_check` `vendor_report` takes ours off but the items a
  calibration since has judged); afterwards POST `/api/v1/work-orders/{id}/test-equipment/` (facility staff only; Approve once
  closed). A work order with test equipment recorded is never deleted;
- the device security register, read only (`views_cybersecurity.py`; never a vendor's or a requester's account): `/api/v1/software-releases/`
  (Device security View: each model's profile and releases with their groups and words, the two standing lines, and how many devices on
  site run each; `?device_model=`, `?search=`, and those entered in error too with `?removed=1`) and `/api/v1/device-security/` (Device
  security View and Equipment View: every device on site, ours and temporary, one by id or tag, with its facts, its connection, and its
  standing words; the Fleet's filters `?whose=`, `?department=`, `?category=`, `?connected=`, `?standing=`, `?norelease=1`, plus
  `?device_model=` and `?search=` a tag or serial); a write answers 405 naming the Device security screen. The device and device model
  records never carry the register;
- software updates (slice 38): `/api/v1/software-updates/`, read only (Device security View and Equipment View; never a vendor's or a
  requester's account): every update work order's record and every install recorded by hand (one entered in error is left out), newest
  first, with the device, the work order's id and number (never a link), the target, the state (`open`, `done`, `not_confirmed`,
  `cancelled`) and the sentence the Security tab shows, the outcome, the releases before and after, the reference, the basis, the
  installer, and who recorded it; `?device=` (an id, or a tag in any letter case), `?release=` (the release to install or the one run
  after), `?state=`. A work order reads `software_update` (its target, state, outcome, and sentence) for those readers, and, GET one
  open, `software_choices` (what you may send). The transition to completed takes `software_outcome` (`installed`, `patches`,
  `already`, or `not_confirmed`; needed while nothing is recorded, then left out keeps what was), `software_release` (an id from
  `software_choices`), `software_reference`, and `software_basis`; a missing one answers a 400 naming what to send. Every other move,
  and a new work order, refuses them; an update work order keeps its device, type, and problem, and is cancelled rather than deleted.
- security decisions (slice 39): `/api/v1/security-decisions/`, read only (Device security View and Equipment View; never a vendor's or a
  requester's account), each by id or number: its scope (the device's id and tag, the model's id, the release's id and version), the
  reasons, the decision and its label, the controls and their words, the controls' reference, the plan with its day, words, and owner,
  who accepted it (with its words), IT's exception number, who recorded it, the days decided, to review, and ended, why it ended, the
  decision it supersedes, its sentence, the devices it governed when recorded, and its Check lines; `?scope=device|release|model`,
  `?state=in_force|review_soon|review_due|ended` (in_force: a review more than 30 days away; review_soon: within 30 days), `?device=` (an id, or a tag in any letter case). Recording them is the Device security screen's. A
  device or model a decision names is never deleted (the refusal names the decision for a Device security reader; retire the device).
  Each decision reads `advisory` ({id, number}, or null for one about support), filtered by `?applies_to=support_ended|advisory` and
  `?advisory=` (an id or a number).
- security advisories (slice 40): `/api/v1/security-advisories/`, read only (Device security View; never a vendor's or a requester's
  account), each by id or number: its name, status with its words for your access, the day received and who recorded it, whether its
  models are confirmed, its CVE IDs, why it is not applicable, its sources, and the models it names (which releases, what it offers, the
  fix or patch list, and the devices per line, plus responses for Equipment View readers); one advisory adds each device's line and
  responses for Equipment View readers; `?status=`, `?model=`, `?cve=`, `?reference=`. Recording them is the Device security screen's. A
  device model an advisory names is never deleted (the refusal names the advisory for a Device security reader).
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
