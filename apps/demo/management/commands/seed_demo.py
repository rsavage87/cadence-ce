"""
Seed a small, deterministic demo tenant (same fictional facility as the mock).

    python manage.py seed_demo            # creates tenant 'riverside' with director kim@riverside.example / DemoPass-2026

Slice 22: Kim also directs a second, small facility, Riverside North Campus (slug riverside-north: a handful of devices, two
technicians, and a few work orders), as the same person: her account there is linked to her Riverside account the way an
invitation links one (apps.accounts.services.add_account) and joined with her password (apps.accounts.people.join), so the demo
shows the facility menu. A database seeded before slice 22 gets the North Campus on the next run.

Slice 25: the survey binder (apps.reports.survey) shows a few deliberate items, not nothing and not a flood. Work orders go to a
technician credentialed for the device on the day (apps.credentials.services), and one credential lapsed and was renewed with a repair
done during the lapse; most late life-support and high-risk PMs have a reason recorded, the latest one not; four devices were added as
new to the facility (slice 26 adds them through the incoming inspection's own services: one waiting for its inspection, one that
failed it and passed its re-inspection, one put in use before it in an emergency and inspected the next day, and one in service with no
inspection, added in service as the API's old path still allows); two models are risk-scored, one of them past its yearly review; and
each recall match reached the facility the day its notice was published.

Slice 27: the North Campus's written policy counts a medium or low risk PM on time by the end of its due month (its PM completion
window, Settings), so All facilities shows a facility judged by a window other than the due date. Riverside keeps the default.

Slice 28: Riverside has two device incidents, recorded through apps.incidents.services as the screens record them, so the Incidents
nav badge, the survey binder's Device incidents section, and the incident drawer have something real. An open one: a defibrillator a
nurse reported through the portal (tagged out), adopted as the investigation by the on-call technician who recorded it, outcome not
known yet for a patient, held with its accessories kept and its log saved, the investigation in progress, the decision pending, its
report due OPEN_INCIDENT_LEFT work days from the seed's today. A closed one, two months back: a vital signs monitor, no harm, the
investigation done (the device met its specifications), decided not serious by risk management, returned to use, closed. Neither
emails anyone (apps.incidents.notify.quiet()), and each one's history is dated the days it happened.

Slice 29: Riverside has four rentals, vendor loaners, and demo units (TEMPORARY_DEVICES), added through apps.equipment.services as Add
rental or loaner adds them and inspected on the rental checklist: a rental bed with its owner's PM date recorded; a vendor loaner pump
standing in for one of ours out for vendor repair (it arrived before the recall batch, so its recall work order went to its owner as
vendor service); a demo monitor entered after it was dropped off on the unit, past its due back date with no owner's PM date (the
Overview's attention list and one survey binder check); and a rental ventilator returned to its owner after a surge.

Slice 30: every repair the demo completes records its checks after repair (apps.workorders.repair_checks) through
completion.complete_work_order: checked and ready for use with every step passed (POST_REPAIR_RESULTS, a constant list that draws
nothing from the demo's generators, so every later draw is as before), but for the patient monitor's closed vendor repair, checked by
the vendor with their service report's number (VENDOR_REPORT_NUMBER), and one smart bed found beyond repair (NOT_RETURNED_*): not
returned to use, then retired. The closed incident's investigation is checked before its release returns the monitor to use.

Slice 31: Riverside keeps records for two vendors' staff (VENDOR_ENGINEERS, VENDOR_TRAININGS, VENDOR_ATTESTATION), added through
apps.vendors.services as the Vendor staff tab adds them, before the vendor work: Philips has two field engineers on file, one with OEM
training on the patient monitor model in force for months to come, one whose training on it expired before the monitor's closed vendor
repair; TechCare Biomedical Services has one engineer with the vendor's own training on Fresenius dialysis machines (a product
category) and its attestation covering every device it services. The patient monitor's closed vendor repair names the engineer whose
training had expired (complete_work_order(vendor_engineer=...)), and Philips has no attestation on file: the survey binder's Contracted
service and vendor staff section shows its one deliberate GAP (Philips, linking to the Vendor staff tab). TechCare has no job in the
demo, so its records show on the Vendor staff tab and in the binder's tables only. Constants only: nothing is drawn from the demo's
generators, and no work order is added (every later number and draw stays as it was).

Slice 32: Riverside has a draft program evaluation (Reports, Program evaluation) of the 12 months ending at the end of last month,
drafted through apps.evaluations.services at the very end of the seed by the CE manager (rfeldman@: Evaluations Approve, and
View on every area the figures cover, which taking them needs), so its figures count everything the demo did: about six months of
work, with "Cadence's records begin" on the day the demo's work starts, and no previous column (the records begin after the 12 months
before end). Two of this period's objectives with their results, one for the next period, and two parts of the text (EVALUATION_*:
plain words about the program as a whole). A draft, so the demo can refresh its figures, send it to committee, print the committee's
copy, and record the approval. Constants only: nothing is drawn from the demo's generators. The North Campus has none.

Slice 33: every repair the demo completes records what it found (repair_finding, apps.workorders.findings). A drawn resolution says
it through REPAIR_FINDINGS (a constant map: no new draw, so every later draw is as before; one of REPAIR_RESOLUTIONS, the same length as
it was, now reads as a use error, so the figures by finding have one), and each fixed repair names its own: the failed PM's repair wore
out (found on that PM: never "PM could have caught it"), the vendor's closed repair states a finding from their report, the closed
incident's investigation found no problem (the device met its specifications), the bed beyond repair and the repair done during the
credential's lapse wore out. No work order is added.

Slice 34: Riverside has one signed-off review of a contracted service vendor (Contracts, Reviews): Philips, for the 12 months ending
yesterday (its closed monitor repair inside them), drafted through apps.evaluations.vendor_reviews by the CE manager just before the
program evaluation (so that one stays made last), its figures taken then. Every criterion rated (staff evidence not met: the engineer
named on that repair had no training on file for it, and Philips has no attestation), partially meets expectations, a corrective action
plan due VENDOR_REVIEW_PLAN_DAYS after the sign-off with no result yet, the vendor's reported response, and a summary about the vendor's
service as a whole (VENDOR_REVIEW_*: no counts, no engineer, patient, requester, or incident). Signed off today by the CE manager. The
survey binder's vendors section shows its number in Philips's last review, and its check names the other vendors under contract with
no signed-off review. Constants only: nothing is drawn from the demo's generators. The North Campus has none.

Slice 35: Riverside's CE department keeps its own test equipment (Equipment, Test equipment), added through apps.testequipment.services
as the screen adds it, TE_ADDED_DAYS_AGO (before the demo's work begins), each certificate recorded the day it came back from the lab
(TEST_EQUIPMENT; each one's history dated so, so none reads "on file since"), all before the completions that name them: an electrical
safety analyzer per technician (held by them), calibrated before the work began and due months from now, recorded on its holder's
in-house PMs, incoming inspections, and repairs checked and ready for use; and on the PMs of the models in TE_FOR_MODELS, the analyzer for
them: a ProSim 8 due within 60 days; a defibrillator analyzer past due DEFIB_ANALYZER_DUE_DAYS_AGO days, recorded on the defibrillators'
PMs up to its due day and on one after it (the survey binder's finding, and its gap while it is in use); and an infusion device analyzer
whose certificate last week found it out of tolerance, with the pumps' PMs done with it since its previous certificate not reviewed (the
binder's gap). Constants only: nothing is drawn from the demo's generators, and no work order or device is added. The North Campus has
none.

Slice 36: Riverside plans replacements by a fiscal year starting in July (FISCAL_YEAR_START, Settings), and CE recorded what it knows
about a few models through equipment.services.update_device_model, as the model drawer's Record support records it (MODEL_LIVES,
SUPPORT_RECORDS), so the Replacement planning report and the device drawer's outlook (apps.capital.priority) have something real: the
smart beds' expected life lowered to the manufacturer's 10 years, which puts a few of them past it; the infusion pumps' life as the AHA
guide's; the dialysis machines' manufacturer support ended a few months ago, after their sale ended (the notice on file, checked
recently: the review flag "Manufacturer support ended" on each, which TechCare services under contract); the sterilizers' parts no
longer available, after their support ended (the must-act flag "Parts no longer available": suggested for the current fiscal year); the
vital signs monitors' parts
available until a day announced about two and a half years ahead; and the patient monitors' support checked with Philips, nothing
announced. The other models read "Support not checked" and "life source not recorded": the report's worklist. The smart bed found beyond
repair (slice 30) stays retired: kept out of service waiting for its replacement, it would raise the must-act flag "Kept out of use after a
repair", but the survey binder's repairs section would then ask for it to be retired or repaired (a check the slice 30 demo does not
have), so the sterilizers carry the demo's must-act flag instead. The demo's work begins 180 days back, so failures and service cost read "too little history"
on every device (35 points unknown beside each score), as for any facility in its first year on Cadence. Calls that draw nothing, after
the draws: no device's install date moves, and every later draw is as before. The North Campus keeps a calendar fiscal year.

Slice 37: Riverside's device security register (the Device security screen, the device drawer's Security tab), recorded by the CE
manager through apps.cybersecurity.services as the screens record it (_device_security): the profiles of the models that connect (the
pumps wireless, the patient monitors wired and wireless, the ventilators and the C-arms wired, the beds wireless) and of the sterilizers,
which do not, with who installs updates here and the network segment on a few; the pumps' two fictional firmware releases (the older
one's security updates ended, the newer one's end announced), most pumps set by Set devices to this release and the last few left not
recorded; the patient monitors' MDS2 on file for an older release than most of them run (the screen shows it is for that version);
the C-arms' operating system, its vendor's published support ended; the ventilators' MDS2 requested and the beds' not recorded; the
vendor loaner pump recorded the day it came, and a C-arm kept off the network (SECURITY_*). The fleet the demo began with came into
Cadence the day before its work begins (_fleet_in_cadence dates each device's "Added" history row so), so Changes for IT lists the new
devices, the rentals and the loaner, and the bed retired after its repair, never the whole fleet as added on the seed's day. Fictional
throughout (every version "(demo)", every MDS2 document DEMO-, no link): nothing describes a real product's software or disclosure.
Constants only: nothing is drawn, no device moves. The North Campus has none.

Slice 38: software updates (_software_updates), after the register, through apps.cybersecurity.updates as the Device security screen and
the completion modal record them: Kim opens updates to the pumps' newer release from the older one, narrowed to one department's 3 or 4
pumps (Open updates to this release), each due on its pump's next PM and assigned to a credentialed in-house technician (CE installs the
pumps' updates here); today the technician completes one as installed (its record moves to the newer release: one pump fewer on the
older release, past its security updates) and one with the version not confirmed (Confirm version is offered on it), and the rest stay
open. And the second C-arm by tag (the first is kept off the network): CE found it on a release added for its model ("7.2 (demo)") at the
last round of checks, then the manufacturer applied the patches on its validated patch list remotely, recorded by hand (DEMO-PATCH-, the
list of the month before). The work orders' problems and resolutions name no version and no reference. Today's work: every period the
program evaluation and the vendor review count is as before; no vendor job, nothing on the ventilators. Constants only: nothing is drawn.
The North Campus has none.
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts import people, services
from apps.accounts.models import Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType, Coverage
from apps.core.workdays import is_work_day
from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import qualified_technicians, renew_credential
from apps.cybersecurity import services as cyber
from apps.cybersecurity import updates as cyber_updates
from apps.cybersecurity.models import (
    Connects,
    Disclosed,
    InstalledBy,
    Mds2Edition,
    Mds2Status,
    Network,
    OsFamily,
    Patching,
    PatientData,
    Sbom,
    Segmented,
    SoftwareRelease,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
)
from apps.equipment import services as equipment
from apps.equipment.models import (
    AddedAs,
    Asset,
    AssetStatus,
    Department,
    DeviceModel,
    LifeSource,
    Ownership,
    ReturnCleaning,
    ReturnData,
    RiskClass,
    UseBeforeInspection,
)
from apps.evaluations import services as evaluations
from apps.evaluations import vendor_reviews
from apps.evaluations.models import Decision, ObjectiveKind, ObjectiveResult, Overall, Rating, StatedInContract
from apps.facility.models import PmWindow
from apps.facility.services import labor_rates, update_settings
from apps.incidents import notify as incident_notify
from apps.incidents import services as incident_services
from apps.incidents.models import Accessories, Affected, Basis, DecidedBy, EventLog, Finding, Incident, IncidentHold, Outcome, Release
from apps.notifications import assignments
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.pm.services import assign_week, missed_pms
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import create_recall_work_orders, set_status
from apps.tenants.context import tenant_context, zone_of
from apps.tenants.models import Tenant
from apps.testequipment import rules as te_rules
from apps.testequipment import services as test_equipment
from apps.testequipment.models import AsFound, Calibration, Kind, Mark, Program, Result, TestEquipment, TestEquipmentUse
from apps.vendors import services as vendor_services
from apps.vendors.models import Covers, TrainingSource
from apps.workorders import inspections
from apps.workorders.completion import checklist_of, complete_work_order, inspection_procedure
from apps.workorders.models import (
    OPEN_STATUSES,
    InspectionResult,
    LaborLine,
    LateReason,
    PartLine,
    PmResult,
    Priority,
    RepairCheck,
    RepairFinding,
    ServiceRequest,
    Source,
    Urgency,
    WorkOrder,
    WoType,
)
from apps.workorders.repair_checks import POST_REPAIR_CHECKLIST
from apps.workorders.services import assign, change_status, create_service_request, create_work_order, set_late_reason

MODELS = [
    # manufacturer, model, description, category, risk, pm months, life yrs, cost, n, support
    ("BD", "Alaris 8015 PCU", "Infusion pump", "Infusion pumps", RiskClass.HIGH, 12, 10, 3200, 60, None),
    ("Philips", "IntelliVue MX750", "Patient monitor", "Patient monitoring", RiskClass.HIGH, 12, 8, 12500, 30, ContractType.OEM),
    ("Hamilton Medical", "Hamilton-G5", "ICU ventilator", "Ventilators", RiskClass.LIFE_SUPPORT, 6, 10, 38000, 12, ContractType.OEM),
    ("GE HealthCare", "Aisys CS2", "Anesthesia workstation", "Anesthesia", RiskClass.LIFE_SUPPORT, 6, 12, 95000, 6, ContractType.OEM),
    ("Zoll", "R Series Plus", "Defibrillator", "Defibrillators", RiskClass.LIFE_SUPPORT, 12, 10, 14000, 20, None),
    ("Hillrom", "Centrella", "Smart bed", "Beds & stretchers", RiskClass.LOW, 12, 12, 21000, 25, None),
    ("Steris", "Amsco 400", "Steam sterilizer", "Sterilization", RiskClass.HIGH, 6, 20, 145000, 3, ContractType.OEM),
    ("Fresenius", "2008T BlueStar", "Hemodialysis machine", "Dialysis", RiskClass.LIFE_SUPPORT, 6, 8, 24000, 8, ContractType.THIRD_PARTY),
    ("Welch Allyn", "Connex Spot", "Vital signs monitor", "Patient monitoring", RiskClass.MEDIUM, 12, 8, 3100, 30, None),
    ("Siemens Healthineers", "Cios Spin", "Mobile C-arm", "Imaging", RiskClass.HIGH, 6, 10, 275000, 2, ContractType.OEM),
]
# CMS (S&C 14-07) keeps imaging and radiologic equipment (diagnostic or therapeutic: X-ray, CT, MRI, ultrasound alike) and medical
# lasers on the manufacturer's schedule, never AEM. The demo's catalog has one such category, the mobile C-arm's (fluoroscopic X-ray).
OEM_SCHEDULE_CATEGORIES = {"Imaging"}
DEPTS = ["ICU", "ED", "OR", "Med/Surg 3E", "Med/Surg 4E", "NICU", "Dialysis", "Central Sterile", "Radiology", "Telemetry 5"]
# Fictional notices (they do not describe real recalls for these products), one per disposition the Recalls screen shows.
# FDA number, published days ago, classification, manufacturer, product, model terms, catalog model, title, action, status, closed days ago, note
ALERTS = [
    ("Z-2026-4408", 9, "Class II", "BD", "Alaris 8015 PCU infusion pump", ["Alaris 8015"], "Alaris 8015 PCU",
     "Keypad membrane may allow fluid ingress", "Inspect keypad; replace per service bulletin", AlertMatch.Status.IN_PROGRESS, None, ""),
    ("Z-2026-4415", 11, "Class I", "Hamilton Medical", "Hamilton-G5 intensive care ventilator", ["Hamilton-G5"], "Hamilton-G5",
     "Ventilator software may cause an unexpected transition to standby",
     "Apply software update and verify version on each unit. Keep units in service pending update unless directed otherwise; "
     "verify backup ventilation is available.", AlertMatch.Status.NEEDS_ACTION, None, ""),
    ("Z-2026-3902", 36, "Class II", "Fresenius", "2008T BlueStar hemodialysis machine", ["2008T"], "2008T BlueStar",
     "Hemodialysis machine blood pump rotor may loosen during treatment",
     "Inspect rotor set-screw torque at next PM; replace rotor assemblies from the affected range.", AlertMatch.Status.UNDER_REVIEW, None, ""),
    ("Z-2026-3055", 88, "Class II", "Steris", "Amsco 400 Series steam sterilizer", ["Amsco 400"], "Amsco 400",
     "Sterilizer door gasket may fail prematurely, causing cycle aborts", "Replace gaskets from affected date codes.",
     AlertMatch.Status.CLOSED, 40, "Gaskets replaced on all 3 sterilizers during July PM."),
]
TECHS = [
    ("Dana Whitfield", "Lead BMET", "CBET", [(Scope.CATEGORY, "Infusion pumps"), (Scope.CATEGORY, "Patient monitoring"), (Scope.CATEGORY, "Defibrillators"),
                                             (Scope.CATEGORY, "Beds & stretchers"), (Scope.MODEL, "Hamilton-G5")]),
    ("Marcus Reyes", "BMET II", "", [(Scope.CATEGORY, "Infusion pumps"), (Scope.MANUFACTURER, "Hillrom"), (Scope.CATEGORY, "Patient monitoring")]),
    ("Priya Natarajan", "BMET II", "CBET", [(Scope.CATEGORY, "Ventilators"), (Scope.CATEGORY, "Anesthesia"), (Scope.CATEGORY, "Dialysis"),
                                            (Scope.CATEGORY, "Infusion pumps")]),
    ("Tom Okafor", "BMET I", "", [(Scope.CATEGORY, "Beds & stretchers"), (Scope.MODEL, "Connex Spot")]),
    ("Lena Kowalski", "Imaging specialist", "CRES", [(Scope.CATEGORY, "Imaging"), (Scope.CATEGORY, "Sterilization"),
                                                     (Scope.MANUFACTURER, "Siemens Healthineers")]),
]
# first, last, email, role slug, department, status (active / invited / deactivated), hours since last sign-in (None = never)
USERS = [
    ("Rob", "Feldman", "rfeldman", "manager", "Clinical Engineering", "active", 1),
    ("Dana", "Whitfield", "dwhitfield", "technician", "Clinical Engineering", "active", 2),
    ("Marcus", "Reyes", "mreyes", "technician", "Clinical Engineering", "active", 3),
    ("Priya", "Natarajan", "pnatarajan", "technician", "Clinical Engineering", "active", 5),
    ("Tom", "Okafor", "tokafor", "technician", "Clinical Engineering", "active", 26),
    ("Lena", "Kowalski", "lkowalski", "technician", "Clinical Engineering", "active", 2),
    ("Angela", "Ruiz", "aruiz", "requester", "ICU", "active", 30),
    ("Devon", "Park", "dpark", "requester", "ED", "active", 80),
    ("Maria", "Santos", "msantos", "requester", "Central Sterile", "invited", None),
    ("Jordan", "Lee", "jlee", "analyst", "Finance", "active", 100),
    ("Sam", "Whitaker", "swhitaker", "analyst", "Quality and Patient Safety", "active", 40),
    ("Philips", "field service", "fse-{slug}@philips.example", "vendor", "External vendor", "active", 170),  # at the vendor's own domain
    ("Chris", "Nolan", "cnolan", "technician", "Clinical Engineering", "deactivated", 2700),
]
# Slice 16: the vendor technician sees only the work orders assigned to their company. The patient monitors' OEM contract names
# Philips, so work orders dispatched to their service carry that name (web.forms.vendor_name_for). The requesters' departments above
# are the demo's Departments (DEPTS), so each sees their own unit.
VENDOR_COMPANY = "Philips"
# Vendor service on contract devices, recent enough to leave the history above (and the AEM evidence drawn from it) as it was:
# device model, opened days ago, where it stands, problem. The C-arm's goes to Siemens Healthineers, which the Philips technician never sees.
VENDOR_WORK = [
    ("IntelliVue MX750", 2, "open", "No waveform on lead II"),
    ("IntelliVue MX750", 6, "in_progress", "Screen flickers intermittently"),
    ("IntelliVue MX750", 24, "closed", "Error code on startup"),
    ("Cios Spin", 4, "open", "Error code on startup"),
]
VENDOR_RESOLUTION = "Vendor replaced the main board under contract and verified operation per OEM procedure."
VENDOR_FINDING = RepairFinding.WEAR  # slice 33: the cause their report states (never "not stated")
PROBLEMS = ["Battery will not hold charge", "Occlusion alarm with no occlusion", "Screen flickers intermittently", "Error code on startup",
            "Pump door latch loose", "No waveform on lead II"]
# Every demo procedure's checklist; the electrical safety step records the leakage current (slice 15 records each PM's results).
CHECKLIST = ["Visual inspection and cleaning", {"text": "Electrical safety test per IEC 62353", "measure": "leakage µA, limit 100"},
             "Functional test per service manual", "Alarm verification", "Apply PM sticker and document"]
VISUAL, LEAKAGE = 0, 1  # the steps a minor repair and the failed PM touch
REPAIR_RESOLUTIONS = ["Replaced faulty component and verified per OEM procedure", "Settings corrected, no device fault; showed unit staff the setup",
                      "Cleaned connector and reseated cable, passed functional test", "Replaced battery, runtime test passed",
                      "Firmware updated, verified operation", "No fault found on bench, monitored 24 h and returned"]
# Slice 33: what each drawn resolution found (complete_work_order's repair_finding): read from the resolution, never drawn.
REPAIR_FINDINGS = {
    "Replaced faulty component and verified per OEM procedure": RepairFinding.WEAR,
    "Settings corrected, no device fault; showed unit staff the setup": RepairFinding.USE_ERROR,
    "Cleaned connector and reseated cable, passed functional test": RepairFinding.ACCESSORY,
    "Replaced battery, runtime test passed": RepairFinding.BATTERY,
    "Firmware updated, verified operation": RepairFinding.SOFTWARE,
    "No fault found on bench, monitored 24 h and returned": RepairFinding.NO_PROBLEM_FOUND,
}
MINOR_REPAIRS = ["Visual inspection: replaced a worn power cord strain relief", "Visual inspection: tightened a loose pole clamp",
                 "Visual inspection: replaced a cracked battery door latch", "Visual inspection: replaced a missing caster cap"]
MINOR_REPAIR_SHARE = 0.08  # of the demo's completed PMs; the rest pass, but for one that fails (the first eligible life-support PM)
FAIL_BEFORE_DAYS = 60  # the failed PM is at least this old, so its repair is done and the device back in service
FAILED_READING = "184 µA"
FAIL_REPAIR = "Replaced the line cord (open ground conductor). Leakage 38 µA after repair; electrical safety and functional tests passed."
FAIL_REPAIR_FINDING = RepairFinding.WEAR  # slice 33: the PM caught it (found on PM); what failed: the cord's ground conductor

# Slice 22: Kim's second facility. Its slug is the demo's with "-north"; its name the demo's first word's ("Riverside North Campus").
NORTH_SUFFIX = "-north"
NORTH_DEPTS = ["ICU", "ED", "Med/Surg 2W", "Outpatient Surgery"]
NORTH_FLEET = [("Alaris 8015 PCU", 6), ("IntelliVue MX750", 4), ("R Series Plus", 3), ("Connex Spot", 3)]  # models from MODELS, devices
NORTH_FIRST_TAG = 20100  # its own tag range, so a tag never names a Riverside device too
NORTH_TECHS = [  # name, title, email local part, credentials
    ("Avery Chen", "BMET II", "achen", [(Scope.CATEGORY, "Infusion pumps"), (Scope.CATEGORY, "Patient monitoring"), (Scope.CATEGORY, "Defibrillators")]),
    ("Jamal Brooks", "BMET I", "jbrooks", [(Scope.MODEL, "Connex Spot"), (Scope.CATEGORY, "Infusion pumps")]),
]
NORTH_WORK = [  # model, opened days ago, type, where it stands, problem
    ("Alaris 8015 PCU", 41, WoType.REPAIR, "closed", "Occlusion alarm with no occlusion"),
    ("Connex Spot", 33, WoType.PM, "closed", "Scheduled preventive maintenance"),
    ("R Series Plus", 26, WoType.PM, "closed", "Scheduled preventive maintenance"),
    ("IntelliVue MX750", 12, WoType.REPAIR, "closed", "Screen flickers intermittently"),
    ("Alaris 8015 PCU", 3, WoType.REPAIR, "in_progress", "Pump door latch loose"),
    ("IntelliVue MX750", 2, WoType.REPAIR, "open", "No waveform on lead II"),
    ("R Series Plus", 1, WoType.PM, "open", "Scheduled preventive maintenance"),
]

# Slice 25, the survey binder's deliberate items.
CREW_SEED = 20261008  # who does a work order when the technician drawn is not credentialed for the device (its own generator)
# Why the late life-support and high-risk PMs were late, in turn; the latest is left without a reason (the binder's one gap there).
LATE_REASONS = [LateReason.DEVICE_IN_USE, LateReason.STAFFING, LateReason.NOT_LOCATED, LateReason.WAITING_PARTS_VENDOR]
# Devices added as new to the facility: tag (past the fleet's range), model, department, added days ago, and where it stands.
NEW_DEVICES = [
    ("CE-11001", "R Series Plus", "ED", 2, "waiting"),  # out of service, its incoming inspection open and nobody's yet (a manager's note)
    ("CE-11002", "Connex Spot", "Med/Surg 3E", 12, "failed"),  # failed (leakage), the vendor swapped it, passed its re-inspection
    ("CE-11004", "Hamilton-G5", "ICU", 9, "used_before"),  # in use before its inspection (an emergency), inspected the next day
    ("CE-11003", "Centrella", "Med/Surg 4E", 6, "in_use"),  # in service from the day it came, never inspected (the API's old path)
]
INSPECTED_AFTER_DAYS = 2  # the failed inspection, after the device was added
REINSPECTED_AFTER_DAYS = 5  # its re-inspection, after the fail (the vendor's swap)
INCOMING_FAIL_READING = "640 µA"
INCOMING_FAIL = "Leakage 640 µA, over the 100 µA limit. Vendor to swap the unit under warranty."
# A credential that lapsed and was renewed: (technician, scope, value). It expired, the technician did a repair it covers, then it was
# renewed (days ago for each).
LAPSE = ("Tom Okafor", Scope.CATEGORY, "Beds & stretchers")
LAPSE_EXPIRED_DAYS_AGO, LAPSE_WORK_DAYS_AGO, LAPSE_RENEWED_DAYS_AGO = 75, 66, 45
LAPSE_PROBLEM = "Bed exit alarm does not sound"
LAPSE_RESOLUTION = "Replaced the bed exit sensor; alarm verified in every position."
LAPSE_FINDING = RepairFinding.WEAR  # slice 33
# Two models risk-scored with the Settings rubric (each score in its class's band): model, (function, physical, maintenance, incidents),
# reviewed days ago. The second's yearly review is overdue (the binder's inventory lists it as the facility's own check).
RISK_SCORES = [("Hamilton-G5", (10, 5, 3, 0), 90), ("R Series Plus", (10, 4, 3, 0), 425)]

# Slice 28, Riverside's device incidents (_incidents). No patient details anywhere: an incident has no free text, the portal request's
# problem says what the device did, and the investigation's resolution what was tested and found.
OPEN_INCIDENT_MODEL = "R Series Plus"  # a defibrillator (the pumps all have recall work under way: holding one would hold it up)
OPEN_INCIDENT_LEFT = 4  # work days from the seed's today to the open incident's report due date (the badge turns hot at 3)
OPEN_INCIDENT_PROBLEM = "Did not charge to the selected energy during use. Unit staff took it out of use and kept the pads and cable with it."
OPEN_INCIDENT_REFERENCE = "SE-{yy}-0412"  # the facility's event report number (its year: the year it happened)
CLOSED_INCIDENT_MODEL = "Connex Spot"
CLOSED_INCIDENT_DAYS_AGO = 60
# The closed incident's steps, days after it happened: the investigation started, completed (and the finding), decided, released, closed.
CLOSED_INCIDENT_STEPS = {"started": 1, "completed": 2, "decided": 5, "released": 6, "closed": 7}
CLOSED_INCIDENT_REFERENCE = "SE-{yy}-0377"
CLOSED_INCIDENT_RESOLUTION = ("Bench tested per the service manual: NIBP and SpO2 accuracy against the simulator, alarms, and electrical "
                              "safety all within specification. No device fault found.")
CLOSED_INCIDENT_FINDING = RepairFinding.NO_PROBLEM_FOUND  # slice 33: an investigation never records a use error
INCIDENT_CLEAR_DAYS = 30  # an incident's device has no PM due within this many days of the seed's today (none is missed while held)

# Slice 29, Riverside's rentals, vendor loaners, and demo units (_temporary_devices), oldest first: tag (a temporary sticker's range),
# model, department, whose, owner, the owner's reference, arrived days ago, due back (days from the seed's today; None: no date), the
# owner's PM date (days from today; None: not recorded), and where it stands. Fictional companies but for the manufacturers.
RENTAL_COMPANY = "Summit Medical Rentals"
TEMPORARY_DEVICES = [
    ("T-0098", "Hamilton-G5", "ICU", Ownership.RENTAL, RENTAL_COMPANY, "SMR-RA-55120", 70, -35, 60, "returned"),  # a surge ventilator, back
    ("T-0101", "Centrella", "Med/Surg 4E", Ownership.RENTAL, RENTAL_COMPANY, "SMR-RA-56204", 8, 20, 150, "rental"),  # owner's PM recorded
    ("T-0102", "Alaris 8015 PCU", "ICU", Ownership.LOANER, "BD", "RMA-448812", 6, None, 240, "loaner"),  # for a pump of ours at BD
    ("T-0103", "Connex Spot", "Telemetry 5", Ownership.DEMO, "Welch Allyn", "EVAL-2026-031", 40, -12, None, "demo"),  # past due back
]
LOANER_REPAIR_DAYS_AGO = 7  # the pump of ours went out for vendor repair (tagged out) the day before its loaner came
LOANER_REPAIR_PROBLEM = "Occlusion alarm with no occlusion"
DEMO_ENTERED_DAYS_AGO = 33  # the demo unit was dropped off on the unit by the vendor's rep; CE entered it a week later, already in use
TEMPORARY_LEAKAGE = "18 µA"

# Slice 30, checks after a repair (apps.workorders.repair_checks). Every step passed, the electrical steps with their readings (by
# step: protective earth, equipment leakage, applied-part leakage). A constant: it draws nothing, so the demo's later draws stay as
# they were.
POST_REPAIR_READINGS = {1: "0.08 Ω", 2: "38 µA", 3: "12 µA"}
POST_REPAIR_RESULTS = [{"result": "pass", "reading": POST_REPAIR_READINGS.get(i, "") if measure else ""}
                       for i, (_text, measure) in enumerate(POST_REPAIR_CHECKLIST)]
CHECKED = {"repair_check": RepairCheck.PASSED, "results": POST_REPAIR_RESULTS}
VENDOR_REPORT_NUMBER = "FSR-26-118204"  # the patient monitor's closed vendor repair: Philips's field service report
# The smart bed found beyond repair: not returned to use, then retired. The first bed of ours by tag in service with no open work, no
# work since NOT_RETURNED_OPENED_DAYS_AGO, and no PM due within INCIDENT_CLEAR_DAYS (so retiring it cancels no PM work order).
NOT_RETURNED_MODEL = "Centrella"
NOT_RETURNED_OPENED_DAYS_AGO, NOT_RETURNED_DONE_DAYS_AGO = 20, 18
NOT_RETURNED_PROBLEM = "Bed will not raise or lower"
NOT_RETURNED_RESOLUTION = ("Lift actuator seized and the base frame cracked at a weld. Beyond economical repair: not returned to use, "
                           "recommended for retirement.")
NOT_RETURNED_FINDING = RepairFinding.WEAR  # slice 33
NOT_RETURNED_RETIRED = "Beyond economical repair"

# Slice 31, vendor staff (_vendor_staff), fictional people. Engineers: name, company (as the contracts name the vendor). Trainings:
# engineer, the device models it names (by model) or a manufacturer's product category, source, certificate, issued (days ago),
# expires (days from the seed's today; negative: expired). The patient monitors' closed vendor repair names VENDOR_REPAIR_ENGINEER,
# whose training expired TRAINING_EXPIRED_DAYS_AGO, before that repair (VENDOR_WORK's closed one, done 21 days ago).
TECHCARE = "TechCare Biomedical Services"
VENDOR_ENGINEERS = [("Grace Lindqvist", "Philips"), ("Owen Marsh", "Philips"), ("Luis Ortega", TECHCARE)]
TRAINING_EXPIRED_DAYS_AGO = 60
VENDOR_TRAININGS = [
    ("Grace Lindqvist", {"models": ["IntelliVue MX750"]}, TrainingSource.OEM, "PHL-IVM-55120", 280, 200),
    ("Owen Marsh", {"models": ["IntelliVue MX750"]}, TrainingSource.OEM, "PHL-IVM-41877", 1150, -TRAINING_EXPIRED_DAYS_AGO),
    ("Luis Ortega", {"manufacturer": "Fresenius", "category": "Dialysis"}, TrainingSource.VENDOR, "", 200, 300),
]
VENDOR_REPAIR_ENGINEER = "Owen Marsh"
# TechCare's attestation, covering every device it services for us: its reference, the day it was received, and the first day of
# the year it states (days ago; it runs a year from then, less a day).
VENDOR_ATTESTATION_REFERENCE = "TC-ATT-2026-014"
VENDOR_ATTESTATION_RECEIVED_DAYS_AGO, VENDOR_ATTESTATION_FROM_DAYS_AGO = 100, 110

# Slice 32, the draft program evaluation (_program_evaluation), drafted by EVALUATION_AUTHOR (the CE manager). The text Clinical
# Engineering wrote (NARRATIVE fields: about the program as a whole, never a patient, a requester, or an incident's details); this
# period's objectives (text, target, result, comment) and the next period's (text, target).
EVALUATION_AUTHOR = "rfeldman"
EVALUATION_TEXT = {
    "summary": "A year of scheduled maintenance, repairs, and recall work across the fleet. The committee is asked to review the figures "
               "and this year's objectives, and to approve the objectives set for next year.",
    "improvements": "Late PMs on life-support and high-risk equipment need their reason recorded every time, and incoming inspections "
                    "must be passed before a new device is first used.",
}
EVALUATION_REVIEW_OBJECTIVES = [
    ("Complete every life-support and high-risk PM within its window", "100%", ObjectiveResult.PARTLY,
     "A few PMs were late; a reason is recorded for each but the latest."),
    ("Record the checks after repair on every completed repair", "Every repair", ObjectiveResult.MET,
     "Recorded on each repair since Cadence began keeping them."),
]
EVALUATION_NEXT_OBJECTIVES = [("Bring the mean repair turnaround within the facility's target", "3 days or less")]

# Slice 34, the Philips review (_vendor_review): the vendor's names as its contract and work orders write them, the CE manager's ratings
# and the rest of the review (the vendor's reported response from its quarterly service report; never uptime or response computed from
# Cadence's dates), and the corrective action plan's due day, in days after the sign-off. The summary is about the vendor's service as a
# whole: no counts (the figures have them), no engineer, patient, requester, or incident.
VENDOR_REVIEW_NAMES = [VENDOR_COMPANY]
VENDOR_REVIEW_FIELDS = {
    "rating_timeliness": Rating.MET, "rating_quality": Rating.MET, "rating_documentation": Rating.MET,
    "rating_staff_evidence": Rating.NOT_MET, "rating_user_satisfaction": Rating.MET, "rating_invoices": Rating.MET,
    "stated_in_contract": StatedInContract.PARTLY, "response_target_pct": Decimal("95.00"), "response_reported_pct": Decimal("97.50"),
    "vendor_report_reference": "PHL-QSR-0917", "overall": Overall.PARTIALLY, "decision": Decision.CORRECTIVE_PLAN,
    "summary": "Philips's service on the patient monitors was timely and its service reports were on file for the work it did. The "
               "evidence that its field staff were trained fell short: no training on file covered all of its work, and no attestation "
               "is on file. Philips is asked for a written attestation covering the patient monitors it services, by the plan's due day.",
    "approver_name": "Rob Feldman", "approver_title": "Clinical Engineering Manager",
}
VENDOR_REVIEW_PLAN_DAYS = 90

# Slice 35, the CE department's test equipment (_test_equipment), entered in Cadence TE_ADDED_DAYS_AGO, before the demo's 180 days of work.
# Fictional serials, certificate numbers, and laboratory. Each item: tag, kind, manufacturer, model, serial, the technician holding it (a
# TECHS name, or None), and its certificates, oldest first: (calibrated, days ago; due, days ago, or None for the interval's 12 months;
# as found; adjusted; accreditation mark; provider; certificate number), every one passed.
TE_ADDED_DAYS_AGO = 400
TE_LAB = "Keystone Calibration Laboratory"
ESA_CALIBRATED_DAYS_AGO = 200  # before the work began; due 12 months later, months from today
DEFIB_ANALYZER_DUE_DAYS_AGO = 20  # past due: its certificate (12 months before) typed with the due day its label shows
INFUSION_PREVIOUS_DAYS_AGO, INFUSION_FOUND_DAYS_AGO = 360, 6  # its certificate last week found it out of tolerance
TEST_EQUIPMENT = [
    *[(f"TE-{101 + i}", Kind.ELECTRICAL_SAFETY, "Fluke Biomedical", "ESA612", f"ESA612-31842{i}7", name,
       [(ESA_CALIBRATED_DAYS_AGO, None, AsFound.IN_TOLERANCE, False, Mark.SHOWN, TE_LAB, f"KCL-3301{i}")])
      for i, (name, *_rest) in enumerate(TECHS)],
    ("TE-201", Kind.PATIENT_SIMULATOR, "Fluke Biomedical", "ProSim 8", "PS8-4471023", None,
     [(325, None, AsFound.IN_TOLERANCE, False, Mark.NOT_SHOWN, "Fluke Biomedical", "FBS-118207")]),  # due within 60 days
    ("TE-301", Kind.DEFIBRILLATOR, "Fluke Biomedical", "Impulse 7000DP", "IMP-2209175", None,
     [(DEFIB_ANALYZER_DUE_DAYS_AGO + 365, DEFIB_ANALYZER_DUE_DAYS_AGO, AsFound.IN_TOLERANCE, False, Mark.SHOWN, TE_LAB, "KCL-29544")]),
    ("TE-401", Kind.INFUSION, "Fluke Biomedical", "IDA-5", "IDA5-1730466", None,
     [(INFUSION_PREVIOUS_DAYS_AGO, None, AsFound.IN_TOLERANCE, False, Mark.SHOWN, TE_LAB, "KCL-30311"),
      (INFUSION_FOUND_DAYS_AGO, None, AsFound.OUT_OF_TOLERANCE, True, Mark.SHOWN, TE_LAB, "KCL-41877")]),
]
# The analyzer a PM of these models records, beside its technician's electrical safety analyzer.
TE_FOR_MODELS = {"IntelliVue MX750": "TE-201", "Connex Spot": "TE-201", "R Series Plus": "TE-301", "Alaris 8015 PCU": "TE-401"}

# Slice 36, replacement planning (_replacement_planning), recorded by SUPPORT_RECORDED_BY (the CE manager) after every draw. Fictional:
# no life, date, or notice here describes a real product or a real guide's figure.
FISCAL_YEAR_START = 7  # July: FY2027 runs Jul 1, 2026 to Jun 30, 2027
SUPPORT_RECORDED_BY = "rfeldman"
# Where a model's expected life comes from, and its new life where CE changed it: model, life in years (None: as MODELS has it), source.
# The beds' life (12 in MODELS) lowered to 10 puts a few of ours past it, not all (their install dates are drawn up to 12 years back).
MODEL_LIVES = [("Centrella", 10, LifeSource.MANUFACTURER), ("Alaris 8015 PCU", None, LifeSource.AHA)]
# The manufacturers' support CE recorded: model; sale ended, support ends, and parts available until, each the last day of the month
# that many months from today's (None: none announced); the notice (its reference, {year} the year it was issued, and months ago) or
# None; checked, days ago. The dialysis machines' support ended after their sale did (the review flag); the sterilizers' parts are no
# longer available (the must-act flag: plan them this fiscal year); the vital signs monitors' parts end is announced for a later day;
# the patient monitors are supported (checked, nothing announced).
SUPPORT_RECORDS = [
    ("2008T BlueStar", -30, -4, None, ("FSN-{year}-014", 32), 12),
    ("Amsco 400", -72, -30, -3, ("FSN-{year}-031", 74), 9),
    ("Connex Spot", None, None, 30, ("PSN-{year}-207", 3), 20),
    ("IntelliVue MX750", None, None, None, None, 35),
]

# Slice 37, the device security register (_device_security), recorded by SECURITY_RECORDED_BY (the CE manager) through
# apps.cybersecurity.services after every draw. Fictional: no version, day, document number, or answer here describes a real product's
# software, its operating system, or its manufacturer's disclosure (every version reads "(demo)", every MDS2 document number starts
# DEMO-, the operating system's name is made up, and no link is recorded).
SECURITY_RECORDED_BY = "rfeldman"
# The fleet the demo began with came into Cadence the day before its work begins (180 days back): its "Added" history rows are dated so
# (_fleet_in_cadence), as a facility six months on Cadence reads, so Changes for IT lists what changed lately, never the whole fleet.
FLEET_ADDED_DAYS_AGO = 181
# The last days a vendor states (security updates, an OS's support) are the last day of the month this many months from today's; the
# days something happened (support checked, an MDS2 released or requested) are this many days from today. Negative: past.
SECURITY_LAST_DAYS = ("security_updates_end_on", "os_support_ends_on")
# Each model's profile (all versions, or version not stated): what the model can do as its manufacturer discloses, and the facility's
# arrangement for it (who installs updates here, whether its devices are on a medical device segment) on a few. The beds connect too
# (Changes for IT lists the bed retired after its repair); the sterilizers do not; the other models are not recorded.
SECURITY_PROFILES = [
    ("Alaris 8015 PCU", {"connects": Connects.WIRELESS, "patient_data": PatientData.HANDLES, "patching": Patching.FACILITY,
                         "updates_by": UpdatesBy.CE, "segmented": Segmented.ALL}),
    ("IntelliVue MX750", {"connects": Connects.BOTH, "patient_data": PatientData.STORES, "remote_service": Disclosed.YES,
                          "patching": Patching.MANUFACTURER, "sbom": Sbom.ON_REQUEST, "updates_by": UpdatesBy.MANUFACTURER,
                          "segmented": Segmented.SOME}),
    ("Hamilton-G5", {"connects": Connects.WIRED, "mds2": Mds2Status.REQUESTED, "mds2_requested_on": -40}),  # requested, not yet in
    ("Centrella", {"connects": Connects.WIRELESS}),  # no MDS2 recorded
    ("Cios Spin", {"connects": Connects.WIRED, "patient_data": PatientData.STORES, "os_family": OsFamily.WINDOWS, "os_name": "Demo embedded OS 7",
                   "os_support_ends_on": -10, "support_checked_on": -20, "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2013,
                   "mds2_document": "DEMO-MDS2-0003", "mds2_released_on": -1500, "updates_by": UpdatesBy.MANUFACTURER,
                   "segmented": Segmented.NONE}),
    ("Amsco 400", {"connects": Connects.NONE}),
]
# Software releases: model, version, fields. The pumps' two firmware releases: the older one's security updates ended, the newer one's
# end is announced (its MDS2 on file). The patient monitors' MDS2 is on file for their older release only, while most run the newer one.
PUMP_OLDER, PUMP_NEWER, MONITOR_OLDER, MONITOR_NEWER = "5.2 (demo)", "5.6 (demo)", "3.1 (demo)", "3.4 (demo)"
SECURITY_RELEASES = [
    ("Alaris 8015 PCU", PUMP_OLDER, {"os_family": OsFamily.EMBEDDED, "security_updates_end_on": -5, "support_checked_on": -20}),
    ("Alaris 8015 PCU", PUMP_NEWER, {"os_family": OsFamily.EMBEDDED, "security_updates_end_on": 29, "support_checked_on": -20,
                                     "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_document": "DEMO-MDS2-0002",
                                     "mds2_released_on": -420}),
    ("IntelliVue MX750", MONITOR_OLDER, {"mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_document": "DEMO-MDS2-0001",
                                         "mds2_released_on": -1100}),
    ("IntelliVue MX750", MONITOR_NEWER, {}),  # the version on file; everything else the model's
]
# Set devices to this release (one model's devices of ours on site at once, in tag order): model, version, which of them (a slice of
# the tag order), running it since (days ago; every one of them was installed before), and how they are connected here (None: as the
# model discloses). The last few pumps are left not recorded.
SECURITY_BULK = [
    ("Alaris 8015 PCU", PUMP_OLDER, (0, 12), 20, None),  # found on the older release at the last round of checks
    ("Alaris 8015 PCU", PUMP_NEWER, (12, -4), 150, None),  # the update campaign; the last 4 not checked yet
    ("IntelliVue MX750", MONITOR_OLDER, (0, 5), 20, Network.WIRED),
    ("IntelliVue MX750", MONITOR_NEWER, (5, None), 90, Network.WIRED),
]
SECURITY_LOANER = ("T-0102", PUMP_NEWER, Network.WIRELESS)  # the vendor loaner pump, recorded the day it came (its incoming inspection)
SECURITY_OFF_NETWORK = ("Cios Spin", 20)  # the first C-arm by tag is kept off the network: recorded so, checked days ago, no release

# Slice 38, software updates (_software_updates), after the register: Kim (the director: she may open and assign them) opens updates
# to the pumps' newer release from the older one in one department, the first in DEPTS order whose pumps of ours on the older release
# number UPDATE_DEPARTMENT_PUMPS (none held, none with an open update), due on each pump's next PM (else UPDATE_DUE_DAYS from today),
# assigned to the first in-house technician credentialed for every one of them (the pumps' updates are installed here by CE). Today:
# the first pump's update completed by that technician as installed (UPDATE_BASIS), the second's with the version not confirmed and
# UPDATE_NOT_CONFIRMED_RESOLUTION, the rest open. No version or reference in a problem or a resolution.
UPDATE_DEPARTMENT_PUMPS = (3, 4)
UPDATE_DUE_DAYS = 30
UPDATE_BASIS = UpdateBasis.MANUFACTURER_RELEASE
UPDATE_NOT_CONFIRMED_RESOLUTION = "Update installed and the pump restarted; the version on the device is still to be checked."
# The other C-arm (the first by tag is kept off the network): CE found it on a release of its own at the last round of checks (days
# ago, the profile's support check's day), added for the model then; the manufacturer applied the patches on its validated patch list
# remotely days ago (recorded by hand: no work order), the list named by the month before the day they were applied.
C_ARM_RELEASE, C_ARM_FOUND_DAYS_AGO, C_ARM_PATCHED_DAYS_AGO = "7.2 (demo)", 20, 6
C_ARM_PATCHES = "DEMO-PATCH-{year}-{month:02d}"


def _noon(day: date) -> datetime:
    """Noon of `day` in the current time zone (the facility's, inside tenant_context): a backdated timestamp's moment."""
    return timezone.make_aware(datetime.combine(day, time(12)))


class Command(BaseCommand):
    help = "Create a small demo tenant with devices, contracts, technicians, credentials, and work orders."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="riverside")
        parser.add_argument("--name", default="Riverside Regional Medical Center")

    def handle(self, *args, **opts):
        # A data load: the demo's work orders are assigned, and its incidents recorded, without emailing anyone.
        with assignments.quiet(), incident_notify.quiet():
            self._seed(*args, **opts)

    @transaction.atomic
    def _seed(self, *args, **opts):
        rnd = random.Random(20260922)
        tenant, created = Tenant.objects.get_or_create(slug=opts["slug"], defaults={"name": opts["name"]})
        today = timezone.localdate(timezone=zone_of(tenant))  # the facility's today (its time zone), not the server's
        if not created:
            with tenant_context(tenant):  # inside the tenant: under row-level security the check would otherwise see no devices
                seeded = Asset.objects.exists()
            if seeded:
                self.stdout.write("Demo tenant already seeded.")
                self._north_campus(tenant, opts)  # a database seeded before slice 22 has no North Campus yet
                return
        create_default_roles(tenant)
        with tenant_context(tenant):
            director = Role.objects.get(slug="director")
            domain = f"{opts['slug']}.example"  # usernames are unique across tenants, so a second demo tenant needs its own
            kim = f"kim@{domain}"
            user, _ = User.objects.get_or_create(username=kim,
                                                 defaults={"email": kim, "first_name": "Kim", "last_name": "Alvarez",
                                                           "tenant": tenant, "role": director, "is_staff": True, "department": "Clinical Engineering"})
            user.set_password("DemoPass-2026")
            user.save()
            depts = {d: Department.objects.create(name=d) for d in DEPTS}
            techs = []
            for name, title, cert, creds in TECHS:
                t = Technician.objects.create(name=name, title=title, certification=cert)
                for scope, value in creds:
                    issued_on = today - timedelta(days=rnd.randint(200, 1500))
                    expires_on = today + timedelta(days=rnd.randint(20, 900)) if rnd.random() < 0.4 else None
                    if (name, scope, value) == LAPSE:  # lapsed: renewed later (_credential_lapse)
                        expires_on = today - timedelta(days=LAPSE_EXPIRED_DAYS_AGO)
                    Credential.objects.create(technician=t, scope=scope, value=value, source="OEM training" if scope != Scope.CATEGORY else "In-house sign-off",
                                              issued_on=issued_on, expires_on=expires_on)
                techs.append(t)
            # the rest of the staff: demo accounts without a password (an administrator sets one in Admin), technicians linked by name
            tech_by_name = {t.name: t for t in techs}
            for first, last, local, slug, dept, status, hours in USERS:
                email = local.format(slug=opts["slug"]) if "@" in local else f"{local}@{domain}"
                u = User(username=email, email=email, first_name=first, last_name=last, tenant=tenant, role=Role.objects.get(slug=slug), department=dept,
                         company=VENDOR_COMPANY if slug == "vendor" else "", is_invited=status == "invited", is_active=status != "deactivated",
                         last_login=timezone.now() - timedelta(hours=hours) if hours is not None else None)
                u.set_unusable_password()
                u.save()
                if f"{first} {last}" in tech_by_name:
                    tech_by_name[f"{first} {last}"].user = u
                    tech_by_name[f"{first} {last}"].save(update_fields=["user", "updated_at"])
            assets = []
            tag = 10240
            for mfr, model, desc, cat, risk, pm, life, cost, n, ctype in MODELS:
                proc = PmProcedure.objects.create(code=f"{mfr[:2].upper()}-{model.split()[0][:6].upper()}-PM{pm}", name=f"{desc} {pm}-month PM",
                                                  estimated_hours=1.5 if cost > 20000 else 0.75, checklist=CHECKLIST)
                dm = DeviceModel.objects.create(manufacturer=mfr, model=model, description=desc, category=cat, risk_class=risk, oem_pm_interval_months=pm,
                                                expected_life_years=life, list_cost=cost, pm_procedure=proc,
                                                oem_schedule_required=cat in OEM_SCHEDULE_CATEGORIES)
                contract = None
                if ctype:
                    contract = Contract.objects.create(reference=f"SC-{rnd.randint(2023, 2026)}-{rnd.randint(100, 999)}",
                                                       vendor=mfr if ctype == ContractType.OEM else "TechCare Biomedical Services",
                                                       type=ctype, coverage=rnd.choice(list(Coverage)), start_on=today - timedelta(days=rnd.randint(200, 800)),
                                                       end_on=today + timedelta(days=rnd.randint(-40, 700)),
                                                       annual_cost=round(cost * n * (0.07 if ctype == ContractType.OEM else 0.04), -2))
                for _ in range(n):
                    tag += rnd.randint(1, 3)
                    installed = today - timedelta(days=rnd.randint(200, life * 365))
                    next_pm = today + timedelta(days=rnd.randint(-20, pm * 30))
                    a = Asset.objects.create(tag=f"CE-{tag}", serial=f"{mfr[:2].upper()}{rnd.randint(10000000, 99999999)}", device_model=dm,
                                             department=depts[rnd.choice(DEPTS)], room=str(rnd.randint(1, 40)),
                                             status=AssetStatus.IN_SERVICE if rnd.random() < 0.95 else AssetStatus.IN_REPAIR, installed_on=installed,
                                             acquisition_cost=round(cost * rnd.uniform(0.9, 1.1), 2),
                                             condition=rnd.randint(2, 5), last_pm_on=add_months(next_pm, -pm), next_pm_on=next_pm, contract=contract)
                    assets.append(a)
            # Slice 35: the CE department's test equipment and its certificates, before the work that records it (no draws)
            self._test_equipment(today)
            # six months of closed work orders, plus a small open backlog. What each one found comes from its own generator, so
            # the devices, dates, and technicians stay as they were before results were recorded.
            outcomes = random.Random(20261002)
            crew = random.Random(CREW_SEED)
            failed_pm = None
            for day in range(-180, 0):
                d = today + timedelta(days=day)
                if d.weekday() >= 5:
                    continue
                for _ in range(rnd.randint(1, 3)):
                    a = rnd.choice(assets)
                    is_pm = rnd.random() < 0.65
                    wtype = WoType.PM if is_pm else WoType.REPAIR
                    prio = Priority.HIGH if a.device_model.risk_class == RiskClass.LIFE_SUPPORT else Priority.NORMAL
                    # Drawn in the order the demo always drew them, so every later draw is as before.
                    problem = "Scheduled preventive maintenance" if is_pm else rnd.choice(PROBLEMS)
                    due = d + timedelta(days=rnd.randint(5, 21) if is_pm else 5)
                    drawn = rnd.choice(techs)
                    wo = create_work_order(asset=a, type=wtype, priority=prio, problem=problem, requester="PM planner" if is_pm else "Unit staff",
                                           source=Source.PM_PLANNER if is_pm else Source.MANUAL, opened_on=d, due_on=due,
                                           assigned_to=self._credentialed(a, d, drawn, crew))
                    hours = 0.75 if is_pm else rnd.uniform(1, 4)
                    LaborLine.objects.create(work_order=wo, technician=wo.assigned_to, worked_on=d, hours=round(hours, 2), rate=82)
                    if not is_pm and rnd.random() < 0.6:
                        PartLine.objects.create(work_order=wo, description="Replacement part", quantity=1, unit_cost=rnd.randint(40, 900))
                    late = rnd.random() < 0.03
                    done = wo.due_on + timedelta(days=rnd.randint(1, 8)) if late else d + timedelta(days=rnd.randint(0, min(6, (wo.due_on - d).days)))
                    if done <= today - timedelta(days=1):
                        change_status(wo, "in_progress", as_of=d)
                        if not is_pm:
                            resolution = outcomes.choice(REPAIR_RESOLUTIONS)
                            complete_work_order(wo, resolution=resolution, repair_finding=REPAIR_FINDINGS[resolution], today=done,
                                                test_equipment=self._test_equipment_used(wo, done), **CHECKED)
                        elif failed_pm is None and day <= -FAIL_BEFORE_DAYS and self._can_fail(a):
                            failed_pm = self._failed_pm(wo, done)
                        else:
                            self._completed_pm(wo, done, outcomes, test_equipment=self._test_equipment_used(wo, done, pm=True))
                        change_status(wo, "closed", as_of=done)
            # facility settings as the mock shows them: a shop hotline on the portal and a monthly repair budget
            update_settings(portal_hotline="ext. 4400", repair_budget_monthly=Decimal("52000"))
            # Slice 29: the rentals, loaner, and demo unit, before the recalls: the loaner pump was on site when the pumps' recall batch
            # was opened, so its recall work order goes to its owner
            self._temporary_devices(today, depts)
            # recalls that match the fleet, in every disposition the screen shows
            # Alerts are global (shared by every tenant), so a second demo tenant reuses the same notice.
            for external_id, days_ago, cls, mfr, product, terms, model, title, action, status_, closed_days_ago, note in ALERTS:
                alert, _ = Alert.objects.get_or_create(source=Alert.Source.FDA, external_id=external_id, defaults={
                    "classification": cls, "manufacturer": mfr, "product": product, "model_terms": terms, "title": title, "action": action,
                    "published_on": today - timedelta(days=days_ago), "raw": {"demo": True}})  # demo: the screen shows a disclaimer
                match = AlertMatch.objects.create(alert=alert, device_model=DeviceModel.objects.get(model=model))
                if status_ == AlertMatch.Status.UNDER_REVIEW:
                    set_status(match, AlertMatch.Status.UNDER_REVIEW)
                elif status_ == AlertMatch.Status.IN_PROGRESS:
                    # a batch already under way: a third of the devices done, the rest open with their technician
                    opened = today - timedelta(days=5)
                    batch = create_recall_work_orders(match, today=opened)
                    for wo in WorkOrder.objects.filter(alert=alert).order_by("number")[: batch.created // 3]:
                        change_status(wo, "in_progress", as_of=opened + timedelta(days=1))
                        change_status(wo, "completed", as_of=opened + timedelta(days=2))
                elif status_ == AlertMatch.Status.CLOSED:
                    set_status(match, AlertMatch.Status.IN_PROGRESS, today=today - timedelta(days=closed_days_ago + 7))
                    set_status(match, AlertMatch.Status.CLOSED, note=note, today=today - timedelta(days=closed_days_ago))
                # Slice 25: it reached the facility the day the notice was published (the binder dates a match by its arrival). After
                # the status changes, which save the whole row.
                AlertMatch.objects.filter(pk=match.pk).update(created_at=_noon(alert.published_on))
            self._approved_aem(domain, today)
            engineers = self._vendor_staff(today)  # slice 31: before the vendor work, which names one of them
            self._vendor_work(today, engineers[VENDOR_REPAIR_ENGINEER])
            self._credential_lapse(today)
            self._risk_scores(today)
            self._new_devices(today, depts, user)
            # Slice 24: this week's PMs on the technicians' plates, as a CE manager's Auto-assign week puts them, so a technician signing
            # in (dwhitfield@... and the others, once they have a password) finds their own work on My work
            assign_week(today=today)
            self._late_reasons(today)
            self._incidents(today, domain)
            self._not_returned(today)
            self._replacement_planning(today, domain)  # slice 36: draws nothing, after every draw (no install date moves)
            self._fleet_in_cadence(today, assets)  # slice 37: the fleet's "Added" rows dated the day before the demo's work begins
            self._device_security(today, domain)  # slice 37: draws nothing, after every draw
            self._software_updates(today, domain)  # slice 38: draws nothing, after the register (today's work: outside every period)
            self._vendor_review(today, domain)  # slice 34: before the program evaluation, which stays made last
            self._program_evaluation(today, domain)  # slice 32: last, so its figures count everything above
        devices = len(assets) + len(NEW_DEVICES) + len(TEMPORARY_DEVICES)
        self.stdout.write(self.style.SUCCESS(f"Seeded {tenant.name}: {devices} devices, {len(techs)} technicians. Sign in as {kim} / DemoPass-2026"))
        self._north_campus(tenant, opts)

    def _north_campus(self, tenant, opts) -> None:
        """Kim's second facility (slice 22): a few devices, two technicians, and some work orders, with Kim as its director. Her
        account there is added as an invitation would add it (services.add_account links it to her Riverside account, which has the
        address) and joined with her password (people.join, what choosing it in the facility menu does). She has signed in to both,
        Riverside last, so a sign-in lands there. Nothing happens once the North Campus has devices."""
        kim = User.objects.filter(tenant=tenant, username=f"kim@{opts['slug']}.example").first()
        if kim is None:
            return
        north, _ = Tenant.objects.get_or_create(slug=f"{opts['slug']}{NORTH_SUFFIX}", defaults={"name": f"{opts['name'].split()[0]} North Campus"})
        with tenant_context(north):  # inside the facility: under row-level security the check would otherwise see no devices
            if Asset.objects.exists():
                return
        create_default_roles(north)
        today = timezone.localdate(timezone=zone_of(north))
        rnd, outcomes = random.Random(20261006), random.Random(20261007)
        with tenant_context(north):
            if not services.is_member(north, kim.email):
                account = services.add_account(north, email=kim.email, role=Role.objects.get(slug="director"), first_name=kim.first_name,
                                               last_name=kim.last_name, department="Clinical Engineering", is_staff=True)
                if people.join(User.objects.get(pk=kim.pk), account):  # her password, read fresh: add_account gave her a person
                    now = timezone.now()
                    User.objects.filter(pk=account.pk).update(last_login=now - timedelta(days=1))
                    User.objects.filter(pk=kim.pk).update(last_login=now - timedelta(hours=1))
            # Slice 27: its written policy, set when it started on Cadence: medium and low risk PMs are on time by the end of their due
            # month (life support and high risk keep the due date); its default PM policy line follows the window
            update_settings(pm_window_other=PmWindow.DUE_MONTH)
            depts = [Department.objects.create(name=d) for d in NORTH_DEPTS]
            techs = []
            for name, title, local, creds in NORTH_TECHS:
                first, last = name.split()
                email = f"{local}@{north.slug}.example"
                user = User(username=email, email=email, first_name=first, last_name=last, tenant=north, role=Role.objects.get(slug="technician"),
                            department="Clinical Engineering", last_login=timezone.now() - timedelta(hours=rnd.randint(2, 30)))
                user.set_unusable_password()  # as the Riverside staff: an administrator sets a password in Admin
                user.save()
                t = Technician.objects.create(name=name, title=title, user=user)
                for scope, value in creds:
                    Credential.objects.create(technician=t, scope=scope, value=value, source="In-house sign-off",
                                              issued_on=today - timedelta(days=rnd.randint(200, 900)))
                techs.append(t)
            specs = {spec[1]: spec for spec in MODELS}
            by_model, tag = {}, NORTH_FIRST_TAG
            for model, n in NORTH_FLEET:
                mfr, _model, desc, cat, risk, pm, life, cost, _n, _support = specs[model]
                proc = PmProcedure.objects.create(code=f"{mfr[:2].upper()}-{model.split()[0][:6].upper()}-PM{pm}", name=f"{desc} {pm}-month PM",
                                                  estimated_hours=0.75, checklist=CHECKLIST)
                dm = DeviceModel.objects.create(manufacturer=mfr, model=model, description=desc, category=cat, risk_class=risk, oem_pm_interval_months=pm,
                                                expected_life_years=life, list_cost=cost, pm_procedure=proc)
                by_model[model] = []
                for _ in range(n):
                    tag += rnd.randint(1, 3)
                    next_pm = today + timedelta(days=rnd.randint(-10, pm * 30))
                    by_model[model].append(Asset.objects.create(
                        tag=f"CE-{tag}", serial=f"{mfr[:2].upper()}{rnd.randint(10000000, 99999999)}", device_model=dm, department=rnd.choice(depts),
                        room=str(rnd.randint(1, 30)), status=AssetStatus.IN_SERVICE, installed_on=today - timedelta(days=rnd.randint(200, life * 365)),
                        acquisition_cost=round(cost * rnd.uniform(0.9, 1.1), 2), condition=rnd.randint(3, 5), last_pm_on=add_months(next_pm, -pm),
                        next_pm_on=next_pm))
            for model, days_ago, wtype, state, problem in NORTH_WORK:
                asset = by_model[model].pop(0)  # one work order per device
                is_pm, opened = wtype == WoType.PM, today - timedelta(days=days_ago)
                wo = create_work_order(asset=asset, type=wtype, problem=problem, opened_on=opened, assigned_to=techs[1 if model == "Connex Spot" else 0],
                                       priority=Priority.HIGH if asset.device_model.risk_class == RiskClass.LIFE_SUPPORT else Priority.NORMAL,
                                       requester="PM planner" if is_pm else "Unit staff", source=Source.PM_PLANNER if is_pm else Source.MANUAL)
                if state == "open":
                    continue
                change_status(wo, "in_progress", as_of=opened)
                if state == "closed":
                    done = opened + timedelta(days=2)
                    LaborLine.objects.create(work_order=wo, technician=wo.assigned_to, worked_on=done, hours=Decimal("0.75" if is_pm else "1.5"), rate=82)
                    if is_pm:
                        self._completed_pm(wo, done, outcomes)
                    else:
                        resolution = outcomes.choice(REPAIR_RESOLUTIONS)
                        complete_work_order(wo, resolution=resolution, repair_finding=REPAIR_FINDINGS[resolution], today=done, **CHECKED)
                    change_status(wo, "closed", as_of=done)
        devices = sum(n for _model, n in NORTH_FLEET)
        self.stdout.write(self.style.SUCCESS(f"Seeded {north.name}: {devices} devices, {len(techs)} technicians. {kim.email} directs it too: "
                                             "the facility menu at the top switches between the two."))

    @staticmethod
    def _credentialed(asset, day: date, drawn, crew: random.Random):
        """Who does a work order on `asset` opened on `day`: `drawn` when credentialed for the device that day
        (credentials.services.qualified_technicians), else one of those who are (drawn by `crew`), else `drawn` when nobody is."""
        qualified = [t for t, _q in qualified_technicians(asset, day)]
        if not qualified or drawn in qualified:
            return drawn
        return crew.choice(qualified)

    @staticmethod
    def _risk_scores(today: date) -> None:
        """RISK_SCORES through equipment.services.set_risk_score, each on the day it was reviewed (the class stays: the score is in
        its band)."""
        for model, (function, physical, maintenance, incidents), days_ago in RISK_SCORES:
            equipment.set_risk_score(DeviceModel.objects.get(model=model), function=function, physical=physical, maintenance=maintenance,
                                     incidents=incidents, today=today - timedelta(days=days_ago))

    @staticmethod
    def _late_reasons(today: date) -> None:
        """Why the late life-support and high-risk PMs were late (workorders.services.set_late_reason), in turn, but for the latest,
        left without one: the survey binder lists it until a reason is recorded."""
        late = list(missed_pms(today).filter(asset__device_model__risk_class__in=(RiskClass.LIFE_SUPPORT, RiskClass.HIGH)).order_by("due_on", "number"))
        for i, wo in enumerate(late[:-1]):
            set_late_reason(wo, LATE_REASONS[i % len(LATE_REASONS)], today=today)

    @staticmethod
    def _incidents(today: date, domain: str) -> None:
        """Riverside's device incidents (slice 28), oldest first so they number in order: the closed one, then the open one."""
        Command._closed_incident(today, domain)
        Command._open_incident(today)

    @staticmethod
    def _incident_device(model: str, since: date, today: date) -> Asset:
        """The first device of `model` by tag in service with no open work order, none opened or completed since `since`, and no PM
        due within INCIDENT_CLEAR_DAYS: holding it changes nothing else in the demo (no PM missed while held, no work held up)."""
        busy = WorkOrder.objects.filter(Q(status__in=OPEN_STATUSES) | Q(opened_on__gte=since) | Q(completed_on__gte=since)).values("asset_id")
        return (Asset.objects.filter(device_model__model=model, status=AssetStatus.IN_SERVICE,
                                     next_pm_on__gt=today + timedelta(days=INCIDENT_CLEAR_DAYS))
                .exclude(pk__in=busy).order_by("tag").first())

    @staticmethod
    def _work_days_before(day: date, n: int) -> date:
        """The day with `n` work days after it, up to and including `day` (apps.core.workdays): an incident that happened then is due
        REPORT_WORK_DAYS - n work days after `day`."""
        current, counted = day, 0
        while counted < n:
            if is_work_day(current):
                counted += 1
            current -= timedelta(days=1)
        return current

    @staticmethod
    def _dated(incident, day_of: dict) -> None:
        """The incident and its holds as if recorded on the days they happened: each history row whose reason is in `day_of` dated
        noon of that day (the seed writes them all today), and the rows' created_at the day the incident was recorded."""
        recorded = day_of["Recorded"]
        for model, ids in ((Incident, [incident.pk]), (IncidentHold, list(incident.holds.values_list("pk", flat=True)))):
            for reason, day in day_of.items():
                model.history.filter(id__in=ids, history_change_reason=reason).update(history_date=_noon(day))
            model.objects.filter(pk__in=ids).update(created_at=_noon(recorded))

    @staticmethod
    def _closed_incident(today: date, domain: str) -> None:
        """A vital signs monitor two months back: no harm, recorded and held by a credentialed technician (the incident opened its
        investigation), bench tested and found within specification, decided not serious by risk management (CE manager entering
        it), returned to use, closed."""
        occurred = today - timedelta(days=CLOSED_INCIDENT_DAYS_AGO)
        on = {step: occurred + timedelta(days=n) for step, n in CLOSED_INCIDENT_STEPS.items()}
        device = Command._incident_device(CLOSED_INCIDENT_MODEL, occurred - timedelta(days=1), today)
        technician = qualified_technicians(device, occurred)[0][0]
        manager = User.objects.get(username=f"rfeldman@{domain}")
        incident = incident_services.record_incident(
            asset=device, occurred_on=occurred, outcome=Outcome.NO_HARM, affected=Affected.PATIENT,
            event_reference=CLOSED_INCIDENT_REFERENCE.format(yy=f"{occurred:%y}"), accessories=Accessories.KEPT, event_log=EventLog.NO_LOG,
            by=technician.user, today=occurred)
        wo = incident.work_order
        assign(wo, technician=technician)
        change_status(wo, "in_progress", as_of=on["started"])
        complete_work_order(wo, resolution=CLOSED_INCIDENT_RESOLUTION, repair_finding=CLOSED_INCIDENT_FINDING, today=on["completed"],
                            test_equipment=Command._test_equipment_used(wo, on["completed"]), **CHECKED)  # checked: it may return to use
        change_status(wo, "closed", as_of=on["completed"])
        incident_services.record_finding(incident, Finding.MET_SPECS, by=technician.user)
        incident_services.decide(incident, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=on["decided"],
                                 decided_by=DecidedBy.RISK, by=manager, today=today)
        incident_services.release(incident.holds.get(), Release.RETURN_TO_USE, by=manager, today=on["released"])
        incident_services.close(incident, by=manager, today=on["closed"])
        Command._dated(incident, {"Recorded": occurred, "Held": occurred, "Finding recorded": on["completed"],
                                  "Decided: not reportable": on["decided"], Release.RETURN_TO_USE.label: on["released"], "Closed": on["closed"]})

    @staticmethod
    def _open_incident(today: date) -> None:
        """A defibrillator a nurse reported through the portal, tagged out, OPEN_INCIDENT_LEFT work days before its report is due:
        the on-call technician took the request, recorded the incident on it (adopting the request as the investigation: outcome not
        known yet, a patient, accessories kept, log saved), the defibrillator held, the investigation in progress, the decision
        pending."""
        occurred = Command._work_days_before(today, incident_services.REPORT_WORK_DAYS - OPEN_INCIDENT_LEFT)
        device = Command._incident_device(OPEN_INCIDENT_MODEL, occurred - timedelta(days=1), today)
        technician = qualified_technicians(device, occurred)[0][0]
        request = create_service_request(asset=device, department=device.department, problem=OPEN_INCIDENT_PROBLEM, urgency=Urgency.HIGH,
                                         room=device.room, tagged_out=True)
        wo = request.work_order
        # Reported the day it happened: the request and its work order dated then (create_service_request opens them today).
        WorkOrder.objects.filter(pk=wo.pk).update(opened_on=occurred, due_on=occurred + (wo.due_on - wo.opened_on))
        ServiceRequest.objects.filter(pk=request.pk).update(created_at=_noon(occurred))
        wo.refresh_from_db()
        assign(wo, technician=technician)
        incident = incident_services.record_incident(
            asset=device, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, work_order=wo,
            event_reference=OPEN_INCIDENT_REFERENCE.format(yy=f"{occurred:%y}"), accessories=Accessories.KEPT, event_log=EventLog.SAVED,
            by=technician.user, today=occurred)
        change_status(wo, "in_progress", as_of=occurred)
        Command._dated(incident, {"Recorded": occurred, "Held": occurred})

    @staticmethod
    def _not_returned(today: date) -> None:
        """Slice 30: a smart bed found beyond repair. Its repair, by a technician credentialed for it, completed as not returned to use
        (the bed out of service), closed, and the bed retired. The bed's history is the seed's day like the rest of the fleet's (its
        "Added" row the day the fleet came into Cadence, slice 37's _fleet_in_cadence: the rows read in order)."""
        opened = today - timedelta(days=NOT_RETURNED_OPENED_DAYS_AGO)
        done = today - timedelta(days=NOT_RETURNED_DONE_DAYS_AGO)
        busy = WorkOrder.objects.filter(Q(status__in=OPEN_STATUSES) | Q(opened_on__gte=opened - timedelta(days=1))
                                        | Q(completed_on__gte=opened - timedelta(days=1))).values("asset_id")
        bed = (Asset.objects.filter(device_model__model=NOT_RETURNED_MODEL, ownership=Ownership.OWNED, status=AssetStatus.IN_SERVICE,
                                    next_pm_on__gt=today + timedelta(days=INCIDENT_CLEAR_DAYS))
               .exclude(pk__in=busy).order_by("tag").first())
        wo = create_work_order(asset=bed, type=WoType.REPAIR, priority=Priority.NORMAL, problem=NOT_RETURNED_PROBLEM, requester="Unit staff",
                               opened_on=opened, assigned_to=qualified_technicians(bed, opened)[0][0])
        change_status(wo, "in_progress", as_of=opened)
        complete_work_order(wo, resolution=NOT_RETURNED_RESOLUTION, repair_check=RepairCheck.NOT_RETURNED, repair_finding=NOT_RETURNED_FINDING,
                            today=done)
        change_status(wo, "closed", as_of=done)
        equipment.set_status(Asset.objects.get(pk=bed.pk), AssetStatus.RETIRED, note=NOT_RETURNED_RETIRED, today=today)

    @staticmethod
    def _replacement_planning(today: date, domain: str) -> None:
        """Slice 36: the fiscal year Riverside plans by (Settings, FISCAL_YEAR_START), and what the CE manager recorded about the models'
        expected lives (MODEL_LIVES) and the manufacturers' support (SUPPORT_RECORDS), through equipment.services.update_device_model as
        Record support writes it (each model's history says who). Draws nothing and moves no device."""
        manager = User.objects.get(username=f"{SUPPORT_RECORDED_BY}@{domain}")
        update_settings(by=manager, fiscal_year_start_month=FISCAL_YEAR_START)
        for model, life, source in MODEL_LIVES:
            fields = {"life_source": source} if life is None else {"life_source": source, "expected_life_years": life}
            equipment.update_device_model(DeviceModel.objects.get(model=model), by=manager, **fields)

        def month_end(months: int | None) -> date | None:  # the last day of the month `months` from today's
            return None if months is None else add_months(today.replace(day=1), months + 1) - timedelta(days=1)

        for model, sale_ended, support_ends, parts_end, notice, checked_days_ago in SUPPORT_RECORDS:
            reference = ""
            if notice is not None:
                text, issued_months_ago = notice
                reference = text.format(year=add_months(today, -issued_months_ago).year)
            equipment.update_device_model(
                DeviceModel.objects.get(model=model), by=manager, sale_ended_on=month_end(sale_ended), support_ends_on=month_end(support_ends),
                parts_end_on=month_end(parts_end), support_notice=reference, support_checked_on=today - timedelta(days=checked_days_ago))

    @staticmethod
    def _fleet_in_cadence(today: date, fleet: list) -> None:
        """Slice 37: the fleet the demo began with came into Cadence the day before its work begins (FLEET_ADDED_DAYS_AGO): each
        device's "Added" history row is dated so, before every later row of its own (every one was installed before that day). Only a
        device's history, the change log, and Changes for IT (apps.cybersecurity.rules) read that row's day: Changes for IT then lists
        the devices added, retired, or returned lately (the new devices, the rentals and the loaner, the bed beyond repair), never the
        whole fleet as added on the seed's day. Draws nothing."""
        Asset.history.filter(id__in=[a.pk for a in fleet], history_type="+").update(
            history_date=_noon(today - timedelta(days=FLEET_ADDED_DAYS_AGO)))

    @staticmethod
    def _device_security(today: date, domain: str) -> None:
        """Slice 37: what the CE manager recorded in the device security register through apps.cybersecurity.services, as the Device
        security screen and the device drawer's Record facts record it (each row's history says who): the profiles of the models that
        connect and of the sterilizers, which do not (SECURITY_PROFILES); the pumps' two firmware releases and the patient monitors'
        two (SECURITY_RELEASES); most pumps and every patient monitor of ours set by Set devices to this release (SECURITY_BULK), the last
        few pumps left not recorded; the vendor loaner pump recorded the day it came; and a C-arm kept off the network. So the Fleet tab's
        head figures each count something: models that can connect with no MDS2 on file (the beds; the ventilators' requested),
        devices on a release past its security updates (the pumps on the older release), devices that can connect with no release
        recorded, a model not on a separate segment (the C-arms), and one whose segmentation is not recorded; and the patient
        monitors' MDS2 is on file for an older release than most of them run. Constants only: nothing is drawn, no device moves."""
        manager = User.objects.get(username=f"{SECURITY_RECORDED_BY}@{domain}")  # the CE manager: Device security Approve

        def fields(spec: dict) -> dict:
            out = {}
            for name, value in spec.items():
                if name in SECURITY_LAST_DAYS:  # the last day of the month `value` months from today's
                    value = add_months(today.replace(day=1), value + 1) - timedelta(days=1)
                elif name.endswith("_on"):
                    value = today + timedelta(days=value)
                out[name] = value
            return out

        def ours(device_model) -> list:
            return list(Asset.objects.filter(device_model=device_model, ownership=Ownership.OWNED).exclude(status=AssetStatus.RETIRED)
                        .order_by("tag"))

        models = {dm.model: dm for dm in DeviceModel.objects.all()}
        for model, spec in SECURITY_PROFILES:
            cyber.add_release(models[model], by=manager, today=today, **fields(spec))
        releases = {version: cyber.add_release(models[model], by=manager, today=today, version=version, **fields(spec))
                    for model, version, spec in SECURITY_RELEASES}
        for model, version, (start, stop), since, network in SECURITY_BULK:
            cyber.record_facts_for(models[model], ours(models[model])[start:stop], by=manager, today=today, release=releases[version],
                                   release_since=today - timedelta(days=since), network=network or cyber.KEEP)
        tag, version, network = SECURITY_LOANER
        loaner = Asset.objects.get(tag=tag)
        cyber.record_facts(loaner, by=manager, today=today, release=releases[version], release_since=loaner.arrived_on, network=network,
                           checked_on=loaner.arrived_on)
        model, checked = SECURITY_OFF_NETWORK
        cyber.record_facts(ours(models[model])[0], by=manager, today=today, network=Network.NOT_CONNECTED,
                           checked_on=today - timedelta(days=checked))

    @staticmethod
    def _software_updates(today: date, domain: str) -> None:
        """Slice 38: software updates through apps.cybersecurity.updates, as the Device security screen and the completion modal record
        them (each row's history says who). Kim opens updates to the pumps' newer release (PUMP_NEWER) from the older one in one
        department (_update_department), each due on its pump's next PM (else UPDATE_DUE_DAYS from today) and assigned to the first
        technician credentialed for all of them (the pumps' profile: CE installs their updates here); the technician completes the first
        as installed (the device's record moves to the newer release today) and the second with the version not confirmed (Confirm
        version is offered on it); the rest stay open. And the other C-arm: CE found it on C_ARM_RELEASE at the last round of checks (the
        release added for its model, its facts recorded), then the manufacturer applied the patches on its validated patch list remotely,
        recorded by hand. No vendor job (the vendors binder's figures stay as they were), nothing on the ventilators. Today's work, so
        every period the evaluations and the vendor review count stays as it was. Constants only: nothing is drawn."""
        kim = User.objects.get(username=f"kim@{domain}")  # the director: Device security, Equipment, and Work orders, assigning too
        manager = User.objects.get(username=f"{SECURITY_RECORDED_BY}@{domain}")
        pump_model = DeviceModel.objects.get(model="Alaris 8015 PCU")
        older, newer = (SoftwareRelease.objects.get(device_model=pump_model, version=v) for v in (PUMP_OLDER, PUMP_NEWER))
        department, pumps = Command._update_department(pump_model, older)
        qualified = [{t.pk for t, _q in qualified_technicians(p, today)} for p in pumps]
        technician = next(t for t, _q in qualified_technicians(pumps[0], today) if all(t.pk in q for q in qualified))
        batch = cyber_updates.open_updates(newer, from_releases=[older.pk], department=department.pk,
                                           due_on=today + timedelta(days=UPDATE_DUE_DAYS), due_at_next_pm=True, priority=Priority.NORMAL,
                                           assign_to=technician.pk, by=kim, today=today)
        installed, not_confirmed = batch.work_orders[0], batch.work_orders[1]
        complete_work_order(installed, software=cyber_updates.UpdateChoice(UpdateOutcome.INSTALLED, release_id=str(newer.pk),
                                                                           basis=UPDATE_BASIS), by=technician.user, today=today)
        complete_work_order(not_confirmed, resolution=UPDATE_NOT_CONFIRMED_RESOLUTION,
                            software=cyber_updates.UpdateChoice(cyber_updates.NOT_CONFIRMED), by=technician.user, today=today)

        c_arm_model = DeviceModel.objects.get(model="Cios Spin")
        c_arm = Asset.objects.filter(device_model=c_arm_model, ownership=Ownership.OWNED).exclude(status=AssetStatus.RETIRED).order_by("tag")[1]
        found, patched = today - timedelta(days=C_ARM_FOUND_DAYS_AGO), today - timedelta(days=C_ARM_PATCHED_DAYS_AGO)
        release = cyber.add_release(c_arm_model, by=manager, today=today, version=C_ARM_RELEASE)
        cyber.record_facts(c_arm, by=manager, today=today, release=release, release_since=found, checked_on=found)
        listed = add_months(patched.replace(day=1), -1)  # the patch list of the month before
        patches = cyber_updates.UpdateChoice(UpdateOutcome.PATCHES, reference=C_ARM_PATCHES.format(year=listed.year, month=listed.month),
                                             basis=UpdateBasis.VALIDATED_LIST)
        cyber_updates.record(c_arm, patches, installed_by=InstalledBy.MANUFACTURER, applied_on=patched, by=manager, today=today)

    @staticmethod
    def _update_department(pump_model, older) -> tuple:
        """The department the update batch is narrowed to, and the pumps it opens updates on (as open_updates selects them: on site,
        recorded on `older`, in tag order): the first department in DEPTS order where they number one of UPDATE_DEPARTMENT_PUMPS, none
        held as evidence (open_updates would skip it)."""
        from apps.cybersecurity import rules as cyber_rules

        pumps = list(cyber_rules.on_site().filter(device_model=pump_model).select_related("cyber__release").order_by("tag"))
        on_older = [p for p in pumps if (runs := cyber_rules.release_of(p)) is not None and runs.pk == older.pk]
        for name in DEPTS:
            here = [p for p in on_older if p.department.name == name]
            if len(here) in UPDATE_DEPARTMENT_PUMPS and not any(p.incident_hold for p in here):
                return here[0].department, here
        raise RuntimeError("No department has 3 or 4 pumps on the older release: the demo's update batch has nowhere to go.")

    @staticmethod
    def _credential_lapse(today: date) -> None:
        """One credential that lapsed and was renewed, with a repair done during the lapse (the binder's staff section reads
        credentials as they stood each day, from their history). The credential was entered expired (the technicians above); its
        history is dated as it happened: entered when issued, renewed LAPSE_RENEWED_DAYS_AGO."""
        name, scope, value = LAPSE
        technician = Technician.objects.get(name=name)
        credential = Credential.objects.get(technician=technician, scope=scope, value=value)
        bed = Asset.objects.filter(device_model__category=value, status=AssetStatus.IN_SERVICE).order_by("tag").first()
        opened = today - timedelta(days=LAPSE_WORK_DAYS_AGO)
        done = opened + timedelta(days=1)
        wo = create_work_order(asset=bed, type=WoType.REPAIR, priority=Priority.NORMAL, problem=LAPSE_PROBLEM, requester="Unit staff",
                               opened_on=opened, assigned_to=technician)
        change_status(wo, "in_progress", as_of=opened)
        LaborLine.objects.create(work_order=wo, technician=technician, worked_on=done, hours=Decimal("1.25"), rate=82)
        complete_work_order(wo, resolution=LAPSE_RESOLUTION, repair_finding=LAPSE_FINDING, today=done,
                            test_equipment=Command._test_equipment_used(wo, done), **CHECKED)
        change_status(wo, "closed", as_of=done)
        renewed = today - timedelta(days=LAPSE_RENEWED_DAYS_AGO)
        renew_credential(credential, today=renewed)
        Credential.history.filter(id=credential.pk, history_type="+").update(history_date=_noon(credential.issued_on))
        Credential.history.filter(id=credential.pk, history_type="~").update(history_date=_noon(renewed))

    @staticmethod
    def _new_devices(today: date, depts: dict, director) -> None:
        """Devices added as new to the facility (equipment.services.create_asset, added_as NEW), dated the day they came, through the
        incoming inspection's own services (slice 26): Add device's way (incoming_inspection "waiting": out of service, no next PM, its
        inspection open and unassigned), each inspection done by a technician credentialed for the device and completed with the
        model's checklist and a result like any other (completion.complete_work_order; a pass puts the device in service and starts its
        PM clock on the day it passed).
        - waiting: still waiting, its inspection open and nobody's (the Work orders page tells a CE manager).
        - failed: failed for leakage, which opened its re-inspection for the same technician; the vendor swapped the unit (its serial
          edited) and the re-inspection passed. The binder lists the failed inspection and takes the passed one as the evidence.
        - used_before: put in use before its inspection in an emergency by the director (use_before_inspection, Equipment Approve),
          and inspected the next day: the binder's one finding there.
        - in_use: added in service with no inspection (the API's old path): the binder's one gap there."""
        for tag, model, dept, days_ago, stage in NEW_DEVICES:
            dm = DeviceModel.objects.get(model=model)
            added = today - timedelta(days=days_ago)
            details = {"tag": tag, "device_model": dm, "department": depts[dept], "serial": f"{dm.manufacturer[:2].upper()}{tag[3:]}0042", "room": "1",
                       "condition": 5, "added_as": AddedAs.NEW, "added_on": added, "today": today}
            if stage == "in_use":
                asset = equipment.create_asset(**details, installed_on=added, status=AssetStatus.IN_SERVICE)
            else:  # installed on the unit the day it came when it was used before its inspection, else when it passes (below)
                asset = equipment.create_asset(**details, installed_on=added if stage == "used_before" else None,
                                               incoming_inspection=equipment.INCOMING_WAITING)
            if stage == "failed":
                inspected = added + timedelta(days=INSPECTED_AFTER_DAYS)
                failed = Command._inspect(asset, inspected, InspectionResult.FAILED)
                swapped = inspected + timedelta(days=REINSPECTED_AFTER_DAYS)
                equipment.update_asset(Asset.objects.get(pk=asset.pk), serial=f"{details['serial']}S", installed_on=swapped, today=today)
                Asset.history.filter(id=asset.pk, history_change_reason="Edited").update(history_date=_noon(swapped))  # the swap's day
                Command._inspect(asset, swapped, InspectionResult.PASSED, wo=failed.follow_ups.get())
            elif stage == "used_before":
                equipment.use_before_inspection(asset, UseBeforeInspection.EMERGENCY, by=director, today=added)
                Command._inspect(asset, added + timedelta(days=1), InspectionResult.PASSED)
            # The day it came, after the row's last save (a save writes back the created_at it holds).
            Asset.objects.filter(pk=asset.pk).update(created_at=_noon(added))

    @staticmethod
    def _inspect(asset, day: date, result: str, wo=None) -> WorkOrder:
        """Complete the device's open incoming inspection (or `wo`) on `day` with `result`, by a technician credentialed for the device
        that day (assigned first when nobody has it), with the model's checklist and its leakage reading: every step passed, or the
        leakage over the limit for a fail. In one visit (completion starts it), then closed. Returns the inspection, read again."""
        wo = wo or inspections.open_inspection(asset)
        if wo.assigned_to_id is None:
            assign(wo, technician=qualified_technicians(asset, day)[0][0])
        results = [{"result": "pass", "reading": "21 µA" if i == LEAKAGE else ""} for i in range(len(CHECKLIST))]
        resolution = ""
        if result == InspectionResult.FAILED:
            results[LEAKAGE] = {"result": "fail", "reading": INCOMING_FAIL_READING}
            resolution = INCOMING_FAIL
        complete_work_order(wo, inspection_result=result, results=results, resolution=resolution, today=day,
                            test_equipment=Command._test_equipment_used(wo, day))
        change_status(wo, "closed", as_of=day)
        wo.refresh_from_db()
        return wo

    @staticmethod
    def _temporary_devices(today: date, depts: dict) -> None:
        """Riverside's rentals, vendor loaners, and demo units (slice 29: TEMPORARY_DEVICES), through equipment.services as Add rental
        or loaner records them, each dated the day it came (its history's first row and created_at):
        - returned: a rental ventilator for a surge, inspected the day it came, returned to its owner on its due back day (cleaned and
          decontaminated, patient data cleared). Off the inventory; "Returned to owner" wherever its status shows.
        - rental: a rental bed inspected the day it came, its owner's PM date recorded from the sticker, due back in a few weeks.
        - loaner: a vendor loaner pump standing in for a pump of ours out for vendor repair (_out_for_vendor_repair), inspected the day
          it came, its owner's PM date recorded, no due back date (it goes back when ours does).
        - demo: a demo monitor the vendor's rep dropped off on the unit, entered by CE a week later as already on site (no incoming
          inspection: counted, never a gap), past its due back date, no owner's PM date recorded (the binder's check)."""
        for tag, model, dept, kind, owner, reference, arrived_ago, due_back, owner_pm, stage in TEMPORARY_DEVICES:
            dm = DeviceModel.objects.get(model=model)
            arrived = today - timedelta(days=arrived_ago)
            stay = {"tag": tag, "device_model": dm, "department": depts[dept], "kind": kind, "owner": owner, "owner_reference": reference,
                    "serial": f"{dm.manufacturer[:2].upper()}{tag[2:]}7731", "room": "2", "arrived_on": arrived,
                    "due_back_on": today + timedelta(days=due_back) if due_back is not None else None,
                    "owner_pm_due_on": today + timedelta(days=owner_pm) if owner_pm is not None else None}
            if stage == "loaner":
                stay["stands_in_for"] = Command._out_for_vendor_repair(dm, today)
            if stage == "demo":
                added = today - timedelta(days=DEMO_ENTERED_DAYS_AGO)
                asset = equipment.add_temporary_device(**stay, added_as=AddedAs.EXISTING, today=added)
            else:  # new to the facility: waiting for its incoming inspection, opened the day it came, and inspected that day
                added = arrived
                asset = equipment.add_temporary_device(**stay, today=added)
                Command._inspect_temporary(asset, arrived)
            Asset.history.filter(id=asset.pk, history_type="+").update(history_date=_noon(added))
            if stage == "returned":
                equipment.return_to_owner(asset, cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED, on=asset.due_back_on,
                                          today=today)
                Asset.history.filter(id=asset.pk, history_change_reason=equipment.RETURNED_REASON).update(history_date=_noon(asset.due_back_on))
            # The day it came, after the row's last save (a save writes back the created_at it holds).
            Asset.objects.filter(pk=asset.pk).update(created_at=_noon(added))

    @staticmethod
    def _out_for_vendor_repair(device_model, today: date) -> Asset:
        """The pump of ours a vendor loaner stands in for: the last of the model in service by tag (the recall batch completes the first
        ones), tagged out with a repair LOANER_REPAIR_DAYS_AGO dispatched to its manufacturer's field service (equipment.services
        .service_vendor, as the screens name it) and under way."""
        pump = Asset.objects.filter(device_model=device_model, status=AssetStatus.IN_SERVICE, ownership=Ownership.OWNED).order_by("-tag").first()
        opened = today - timedelta(days=LOANER_REPAIR_DAYS_AGO)
        wo = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem=LOANER_REPAIR_PROBLEM, requester="Unit staff",
                               opened_on=opened, tag_out=True)
        assign(wo, vendor_name=equipment.service_vendor(pump))
        change_status(wo, "in_progress", as_of=opened)
        pump.refresh_from_db()
        return pump

    @staticmethod
    def _inspect_temporary(asset, day: date) -> WorkOrder:
        """Pass a temporary device's open incoming inspection on `day`, on the checklist it is done to (the rental checklist,
        completion.inspection_procedure), every step passed with its leakage reading, by a technician credentialed for the device
        that day; in one visit, then closed."""
        wo = inspections.open_inspection(asset)
        assign(wo, technician=qualified_technicians(asset, day)[0][0])
        results = [{"result": "pass", "reading": TEMPORARY_LEAKAGE if measure else ""}
                   for _text, measure in checklist_of(inspection_procedure(asset))]
        complete_work_order(wo, inspection_result=InspectionResult.PASSED, results=results, today=day,
                            test_equipment=Command._test_equipment_used(wo, day))
        change_status(wo, "closed", as_of=day)
        return wo

    @staticmethod
    def _can_fail(asset) -> bool:
        """The demo's failed PM is on a life-support device in service (so it is tagged out until its repair is done)."""
        return asset.device_model.risk_class == RiskClass.LIFE_SUPPORT and Asset.objects.get(pk=asset.pk).status == AssetStatus.IN_SERVICE

    @staticmethod
    def _completed_pm(wo, done: date, outcomes: random.Random, test_equipment=vendor_services.KEEP) -> None:
        """A PM completed through apps.workorders.completion like any other: mostly Pass, sometimes Pass with minor repair (the
        visual inspection found something put right on the spot), each with its leakage reading, and at Riverside the test equipment
        it recorded (slice 35: _test_equipment_used)."""
        results = [{"result": "pass", "reading": f"{outcomes.randint(4, 62)} µA" if i == LEAKAGE else ""} for i in range(len(CHECKLIST))]
        if outcomes.random() < MINOR_REPAIR_SHARE:
            results[VISUAL]["result"] = "fail"
            complete_work_order(wo, pm_result=PmResult.PASS_MINOR_REPAIR, results=results, resolution=outcomes.choice(MINOR_REPAIRS), today=done,
                                test_equipment=test_equipment)
        else:
            complete_work_order(wo, pm_result=PmResult.PASS, results=results, today=done, test_equipment=test_equipment)

    @staticmethod
    def _failed_pm(wo, done: date):
        """The demo's one failed PM: leakage over the limit. It opens its follow-up repair and tags the device out; the repair is done
        two days later (by a credentialed technician when the follow-up was left unassigned), which returns the device to service. Each
        records its test equipment (slice 35)."""
        results = [{"result": "pass", "reading": ""} for _ in CHECKLIST]
        results[LEAKAGE] = {"result": "fail", "reading": FAILED_READING}
        repair = complete_work_order(wo, pm_result=PmResult.FAIL, results=results, tag_out=True, today=done,
                                     test_equipment=Command._test_equipment_used(wo, done, pm=True)).follow_up
        if repair.assigned_to_id is None:
            credentialed = qualified_technicians(repair.asset, done)
            assign(repair, technician=credentialed[0][0] if credentialed else wo.assigned_to)
        change_status(repair, "in_progress", as_of=done)
        fixed = done + timedelta(days=2)
        complete_work_order(repair, resolution=FAIL_REPAIR, repair_finding=FAIL_REPAIR_FINDING, today=fixed,
                            test_equipment=Command._test_equipment_used(repair, fixed), **CHECKED)
        change_status(repair, "closed", as_of=fixed)
        return wo

    @staticmethod
    def _test_equipment(today: date) -> None:
        """Slice 35: TEST_EQUIPMENT through apps.testequipment.services as the Test equipment tab adds it, each item entered
        TE_ADDED_DAYS_AGO and each certificate the day it came back (their history's first rows and created_at dated so: nothing reads
        "on file since"). In use, the electrical safety analyzers held by their technicians."""
        added = today - timedelta(days=TE_ADDED_DAYS_AGO)
        technicians = {t.name: t for t in Technician.objects.all()}
        for tag, kind, manufacturer, model, serial, holder, certificates in TEST_EQUIPMENT:
            item = test_equipment.add_test_equipment(tag=tag, kind=kind, manufacturer=manufacturer, model=model, serial=serial,
                                                     program=Program.CALIBRATION, interval_months=12,
                                                     held_by=technicians[holder] if holder else None, by=None, today=added)
            TestEquipment.history.filter(id=item.pk, history_type="+").update(history_date=_noon(added))
            TestEquipment.objects.filter(pk=item.pk).update(created_at=_noon(added))
            for calibrated_ago, due_ago, as_found, adjusted, mark, provider, number in certificates:
                on = today - timedelta(days=calibrated_ago)
                cal = test_equipment.record_calibration(
                    item, calibrated_on=on, due_on=today - timedelta(days=due_ago) if due_ago is not None else None, provider=provider,
                    certificate=number, accreditation=mark, as_found=as_found, adjusted=adjusted, result=Result.PASSED, by=None, today=on)
                Calibration.history.filter(id=cal.pk, history_type="+").update(history_date=_noon(on))
                Calibration.objects.filter(pk=cal.pk).update(created_at=_noon(on))

    @staticmethod
    def _test_equipment_used(wo, done: date, *, pm: bool = False) -> list:
        """Slice 35: the test equipment a seeded completion of `wo` on `done` records, as the completion modal posts it (ids): the
        electrical safety analyzer its technician holds, and on a PM of a model in TE_FOR_MODELS that model's analyzer, but once the
        analyzer is past due, on one work order only (the binder's one finding there). Nothing where the facility has none (the North
        Campus)."""
        used = list(TestEquipment.objects.filter(held_by_id=wo.assigned_to_id, kind=Kind.ELECTRICAL_SAFETY)) if wo.assigned_to_id else []
        tag = TE_FOR_MODELS.get(wo.asset.device_model.model) if pm else None
        analyzer = TestEquipment.objects.filter(tag=tag).first() if tag else None
        if analyzer is not None:
            standing = te_rules.standings([analyzer], done)[analyzer.pk]
            if standing.state != te_rules.PAST_DUE or not TestEquipmentUse.objects.filter(
                    test_equipment=analyzer, used_on__gt=standing.due_on).exists():
                used.append(analyzer)
        return [str(i.pk) for i in used]

    @staticmethod
    def _vendor_staff(today: date) -> dict:
        """Slice 31: the vendors' engineers, their training, and TechCare's attestation, through apps.vendors.services as the Vendor
        staff tab adds them (VENDOR_ENGINEERS, VENDOR_TRAININGS, VENDOR_ATTESTATION_*). Returns {name: engineer}."""
        engineers = {name: vendor_services.add_engineer(name=name, company=company) for name, company in VENDOR_ENGINEERS}
        for name, covers, source, certificate, issued_days_ago, expires_in_days in VENDOR_TRAININGS:
            if "models" in covers:
                ids = [str(pk) for pk in DeviceModel.objects.filter(model__in=covers["models"]).values_list("pk", flat=True)]
                coverage = {"covers": Covers.MODELS, "model_ids": ids}
            else:
                coverage = {"covers": Covers.LINE, "manufacturer": covers["manufacturer"], "category": covers["category"]}
            vendor_services.add_training([engineers[name]], source=source, certificate=certificate, today=today,
                                         issued_on=today - timedelta(days=issued_days_ago), expires_on=today + timedelta(days=expires_in_days),
                                         **coverage)
        valid_from = today - timedelta(days=VENDOR_ATTESTATION_FROM_DAYS_AGO)
        vendor_services.add_attestation(vendor_names=[TECHCARE], covers=Covers.ALL, reference=VENDOR_ATTESTATION_REFERENCE,
                                        received_on=today - timedelta(days=VENDOR_ATTESTATION_RECEIVED_DAYS_AGO), valid_from=valid_from,
                                        valid_through=add_months(valid_from, 12) - timedelta(days=1), today=today)
        return engineers

    @staticmethod
    def _vendor_work(today: date, engineer) -> None:
        """Repairs dispatched to the vendor on the contract (through the work-order services like any other): what the demo vendor
        technician works on. One device each, the first in service by tag, so the demo stays the same from run to run. The closed one
        names `engineer` (slice 31: VENDOR_REPAIR_ENGINEER, as the vendor's report names them)."""
        used = set()
        for model, days_ago, state, problem in VENDOR_WORK:
            candidates = Asset.objects.filter(device_model__model=model, contract__isnull=False).exclude(pk__in=used).select_related("contract").order_by("tag")
            asset = candidates.filter(status=AssetStatus.IN_SERVICE).first() or candidates.first()
            used.add(asset.pk)
            opened = today - timedelta(days=days_ago)
            wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem=problem, requester="Unit staff", opened_on=opened)
            assign(wo, vendor_name=asset.contract.vendor)
            if state == "open":
                continue
            change_status(wo, "in_progress", as_of=opened + timedelta(days=1))
            if state == "closed":
                done = opened + timedelta(days=3)
                LaborLine.objects.create(work_order=wo, worked_on=done, hours=Decimal("2.5"), rate=labor_rates()["vendor"], description="Vendor service")
                complete_work_order(wo, resolution=VENDOR_RESOLUTION, repair_check=RepairCheck.VENDOR_REPORT, repair_finding=VENDOR_FINDING,
                                    checks_reference=VENDOR_REPORT_NUMBER, vendor_engineer=engineer, today=done)
                change_status(wo, "closed", as_of=done)

    @staticmethod
    def _vendor_review(today: date, domain: str) -> None:
        """Slice 34: Philips's review for the 12 months ending yesterday (Feb 27 on a Feb 29), through apps.evaluations.vendor_reviews as the Reviews tab
        writes one: drafted by the CE manager (its figures taken now), each of VENDOR_REVIEW_FIELDS recorded, the corrective action
        plan due VENDOR_REVIEW_PLAN_DAYS after today, and signed off today on the figures it holds. Constants only."""
        manager = User.objects.get(username=f"{EVALUATION_AUTHOR}@{domain}")  # the CE manager: Evaluations Approve, Contracts Edit
        end = today - timedelta(days=1)
        if vendor_reviews.leap_feb_28(end):  # on Feb 29 yesterday ends no 12 months (the next could not start on Feb 29)
            end -= timedelta(days=1)
        review = vendor_reviews.create_review(vendor_names=VENDOR_REVIEW_NAMES, period_end=end, by=manager, today=today)
        review = vendor_reviews.update_review(review, by=manager, plan_due_on=today + timedelta(days=VENDOR_REVIEW_PLAN_DAYS),
                                              **VENDOR_REVIEW_FIELDS)
        vendor_reviews.sign_off(review, snapshot_id=review.snapshot_id, signed_on=today, by=manager, today=today)

    @staticmethod
    def _program_evaluation(today: date, domain: str) -> None:
        """Slice 32: a draft evaluation of the 12 months ending at the end of last month, drafted by the CE manager through
        apps.evaluations.services (its figures taken now, after everything else), with this period's objectives and their results, the
        next period's, and two parts of the text. Constants only."""
        author = User.objects.get(username=f"{EVALUATION_AUTHOR}@{domain}")
        ev = evaluations.create_draft(period_start=add_months(today.replace(day=1), -12), by=author, today=today)
        for field, text in EVALUATION_TEXT.items():
            evaluations.update_text(ev, field, text, by=author, today=today)
        for text, target, result, comment in EVALUATION_REVIEW_OBJECTIVES:
            objective = evaluations.add_objective(ev, kind=ObjectiveKind.REVIEW, text=text, target=target, by=author)
            evaluations.update_objective(objective, result=result, comment=comment, by=author)
        for text, target in EVALUATION_NEXT_OBJECTIVES:
            evaluations.add_objective(ev, kind=ObjectiveKind.NEXT, text=text, target=target, by=author)

    def _approved_aem(self, domain: str, today: date) -> None:
        """One AEM interval in force, as the mock's PM library shows for the patient monitors: proposed by a technician with the
        model's failure history (computed from the records above as of the proposal date), approved for the Equipment Management
        Committee by the CE manager, through apps.pm.aem like any other (which refuses a model CMS keeps on the manufacturer's
        schedule, so the demo's AEM is never on one)."""
        monitor = DeviceModel.objects.get(manufacturer="Philips", model="IntelliVue MX750")
        proposer = User.objects.get(username=f"dwhitfield@{domain}")  # technician: PM Edit
        approver = User.objects.get(username=f"rfeldman@{domain}")  # CE manager: PM Approve
        proposed_on, decided_on = today - timedelta(days=75), today - timedelta(days=61)
        decision = aem.propose(monitor, interval_months=24, by=proposer, today=proposed_on,
                               rationale="The monitors run a self-test at every power-on. Proposing a 24-month interval for this model "
                                         "on the failure history attached.")
        aem.approve(decision, by=approver, decided_on=decided_on, note=f"EMC minutes, {decided_on:%B %Y} meeting, item 4", today=today)
