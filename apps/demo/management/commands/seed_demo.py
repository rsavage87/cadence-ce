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
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts import people, services
from apps.accounts.models import Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType, Coverage
from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import qualified_technicians, renew_credential
from apps.equipment import services as equipment
from apps.equipment.models import AddedAs, Asset, AssetStatus, Department, DeviceModel, RiskClass, UseBeforeInspection
from apps.facility.models import PmWindow
from apps.facility.services import labor_rates, update_settings
from apps.notifications import assignments
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.pm.services import assign_week, missed_pms
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import create_recall_work_orders, set_status
from apps.tenants.context import tenant_context, zone_of
from apps.tenants.models import Tenant
from apps.workorders import inspections
from apps.workorders.completion import complete_work_order
from apps.workorders.models import InspectionResult, LaborLine, LateReason, PartLine, PmResult, Priority, Source, WorkOrder, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_late_reason

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
PROBLEMS = ["Battery will not hold charge", "Occlusion alarm with no occlusion", "Screen flickers intermittently", "Error code on startup",
            "Pump door latch loose", "No waveform on lead II"]
# Every demo procedure's checklist; the electrical safety step records the leakage current (slice 15 records each PM's results).
CHECKLIST = ["Visual inspection and cleaning", {"text": "Electrical safety test per IEC 62353", "measure": "leakage µA, limit 100"},
             "Functional test per service manual", "Alarm verification", "Apply PM sticker and document"]
VISUAL, LEAKAGE = 0, 1  # the steps a minor repair and the failed PM touch
REPAIR_RESOLUTIONS = ["Replaced faulty component and verified per OEM procedure", "Calibrated and functionally tested, returned to service",
                      "Cleaned connector and reseated cable, passed functional test", "Replaced battery, runtime test passed",
                      "Firmware updated, verified operation", "No fault found on bench, monitored 24 h and returned"]
MINOR_REPAIRS = ["Visual inspection: replaced a worn power cord strain relief", "Visual inspection: tightened a loose pole clamp",
                 "Visual inspection: replaced a cracked battery door latch", "Visual inspection: replaced a missing caster cap"]
MINOR_REPAIR_SHARE = 0.08  # of the demo's completed PMs; the rest pass, but for one that fails (the first eligible life-support PM)
FAIL_BEFORE_DAYS = 60  # the failed PM is at least this old, so its repair is done and the device back in service
FAILED_READING = "184 µA"
FAIL_REPAIR = "Replaced the line cord (open ground conductor). Leakage 38 µA after repair; electrical safety and functional tests passed."

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
# Two models risk-scored with the Settings rubric (each score in its class's band): model, (function, physical, maintenance, incidents),
# reviewed days ago. The second's yearly review is overdue (the binder's inventory lists it as the facility's own check).
RISK_SCORES = [("Hamilton-G5", (10, 5, 3, 0), 90), ("R Series Plus", (10, 4, 3, 0), 425)]


def _noon(day: date) -> datetime:
    """Noon of `day` in the current time zone (the facility's, inside tenant_context): a backdated timestamp's moment."""
    return timezone.make_aware(datetime.combine(day, time(12)))


class Command(BaseCommand):
    help = "Create a small demo tenant with devices, contracts, technicians, credentials, and work orders."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="riverside")
        parser.add_argument("--name", default="Riverside Regional Medical Center")

    def handle(self, *args, **opts):
        with assignments.quiet():  # a data load: the demo's work orders are assigned without emailing the demo's technicians
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
                            complete_work_order(wo, resolution=outcomes.choice(REPAIR_RESOLUTIONS), today=done)
                        elif failed_pm is None and day <= -FAIL_BEFORE_DAYS and self._can_fail(a):
                            failed_pm = self._failed_pm(wo, done)
                        else:
                            self._completed_pm(wo, done, outcomes)
                        change_status(wo, "closed", as_of=done)
            # facility settings as the mock shows them: a shop hotline on the portal and a monthly repair budget
            update_settings(portal_hotline="ext. 4400", repair_budget_monthly=Decimal("52000"))
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
            self._vendor_work(today)
            self._credential_lapse(today)
            self._risk_scores(today)
            self._new_devices(today, depts, user)
            # Slice 24: this week's PMs on the technicians' plates, as a CE manager's Auto-assign week puts them, so a technician signing
            # in (dwhitfield@... and the others, once they have a password) finds their own work on My work
            assign_week(today=today)
            self._late_reasons(today)
        devices = len(assets) + len(NEW_DEVICES)
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
                        complete_work_order(wo, resolution=outcomes.choice(REPAIR_RESOLUTIONS), today=done)
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
        complete_work_order(wo, resolution=LAPSE_RESOLUTION, today=done)
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
        complete_work_order(wo, inspection_result=result, results=results, resolution=resolution, today=day)
        change_status(wo, "closed", as_of=day)
        wo.refresh_from_db()
        return wo

    @staticmethod
    def _can_fail(asset) -> bool:
        """The demo's failed PM is on a life-support device in service (so it is tagged out until its repair is done)."""
        return asset.device_model.risk_class == RiskClass.LIFE_SUPPORT and Asset.objects.get(pk=asset.pk).status == AssetStatus.IN_SERVICE

    @staticmethod
    def _completed_pm(wo, done: date, outcomes: random.Random) -> None:
        """A PM completed through apps.workorders.completion like any other: mostly Pass, sometimes Pass with minor repair (the
        visual inspection found something put right on the spot), each with its leakage reading."""
        results = [{"result": "pass", "reading": f"{outcomes.randint(4, 62)} µA" if i == LEAKAGE else ""} for i in range(len(CHECKLIST))]
        if outcomes.random() < MINOR_REPAIR_SHARE:
            results[VISUAL]["result"] = "fail"
            complete_work_order(wo, pm_result=PmResult.PASS_MINOR_REPAIR, results=results, resolution=outcomes.choice(MINOR_REPAIRS), today=done)
        else:
            complete_work_order(wo, pm_result=PmResult.PASS, results=results, today=done)

    @staticmethod
    def _failed_pm(wo, done: date):
        """The demo's one failed PM: leakage over the limit. It opens its follow-up repair and tags the device out; the repair is done
        two days later (by a credentialed technician when the follow-up was left unassigned), which returns the device to service."""
        results = [{"result": "pass", "reading": ""} for _ in CHECKLIST]
        results[LEAKAGE] = {"result": "fail", "reading": FAILED_READING}
        repair = complete_work_order(wo, pm_result=PmResult.FAIL, results=results, tag_out=True, today=done).follow_up
        if repair.assigned_to_id is None:
            credentialed = qualified_technicians(repair.asset, done)
            assign(repair, technician=credentialed[0][0] if credentialed else wo.assigned_to)
        change_status(repair, "in_progress", as_of=done)
        complete_work_order(repair, resolution=FAIL_REPAIR, today=done + timedelta(days=2))
        change_status(repair, "closed", as_of=done + timedelta(days=2))
        return wo

    @staticmethod
    def _vendor_work(today: date) -> None:
        """Repairs dispatched to the vendor on the contract (through the work-order services like any other): what the demo vendor
        technician works on. One device each, the first in service by tag, so the demo stays the same from run to run."""
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
                complete_work_order(wo, resolution=VENDOR_RESOLUTION, today=done)
                change_status(wo, "closed", as_of=done)

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
