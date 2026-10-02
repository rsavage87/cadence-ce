"""
Seed a small, deterministic demo tenant (same fictional facility as the mock).

    python manage.py seed_demo            # creates tenant 'riverside' with director kim@riverside.example / DemoPass-2026
"""
import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType, Coverage
from apps.credentials.models import Credential, Scope, Technician
from apps.credentials.services import qualified_technicians
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.facility.services import labor_rates, update_settings
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.recalls.models import Alert, AlertMatch
from apps.recalls.services import create_recall_work_orders, set_status
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.completion import complete_work_order
from apps.workorders.models import LaborLine, PartLine, PmResult, Priority, Source, WorkOrder, WoType
from apps.workorders.services import assign, change_status, create_work_order

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


class Command(BaseCommand):
    help = "Create a small demo tenant with devices, contracts, technicians, credentials, and work orders."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="riverside")
        parser.add_argument("--name", default="Riverside Regional Medical Center")

    @transaction.atomic
    def handle(self, *args, **opts):
        rnd = random.Random(20260922)
        today = date.today()
        tenant, created = Tenant.objects.get_or_create(slug=opts["slug"], defaults={"name": opts["name"]})
        if not created:
            with tenant_context(tenant):  # inside the tenant: under row-level security the check would otherwise see no devices
                seeded = Asset.objects.exists()
            if seeded:
                self.stdout.write("Demo tenant already seeded.")
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
                    Credential.objects.create(technician=t, scope=scope, value=value, source="OEM training" if scope != Scope.CATEGORY else "In-house sign-off",
                                              issued_on=today - timedelta(days=rnd.randint(200, 1500)),
                                              expires_on=today + timedelta(days=rnd.randint(20, 900)) if rnd.random() < 0.4 else None)
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
                    wo = create_work_order(asset=a, type=wtype, priority=prio, problem="Scheduled preventive maintenance" if is_pm else rnd.choice(PROBLEMS),
                                           requester="PM planner" if is_pm else "Unit staff", source=Source.PM_PLANNER if is_pm else Source.MANUAL,
                                           opened_on=d, due_on=d + timedelta(days=rnd.randint(5, 21) if is_pm else 5), assigned_to=rnd.choice(techs))
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
            self._approved_aem(domain, today)
            self._vendor_work(today)
        self.stdout.write(self.style.SUCCESS(f"Seeded {tenant.name}: {len(assets)} devices, {len(techs)} technicians. Sign in as {kim} / DemoPass-2026"))

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
