"""
Seed a small, deterministic demo tenant (same fictional facility as the mock).

    python manage.py seed_demo            # creates tenant 'riverside' with director kim@riverside.example / DemoPass-2026
"""
import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, User, create_default_roles
from apps.contracts.models import Contract, ContractType, Coverage
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.pm.dates import add_months
from apps.pm.models import PmProcedure
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import LaborLine, PartLine, Priority, Source, WoType
from apps.workorders.services import change_status, create_work_order

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
DEPTS = ["ICU", "ED", "OR", "Med/Surg 3E", "Med/Surg 4E", "NICU", "Dialysis", "Central Sterile", "Radiology", "Telemetry 5"]
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
    ("Rob", "Feldman", "rfeldman@riverside.example", "manager", "Clinical Engineering", "active", 1),
    ("Dana", "Whitfield", "dwhitfield@riverside.example", "technician", "Clinical Engineering", "active", 2),
    ("Marcus", "Reyes", "mreyes@riverside.example", "technician", "Clinical Engineering", "active", 3),
    ("Priya", "Natarajan", "pnatarajan@riverside.example", "technician", "Clinical Engineering", "active", 5),
    ("Tom", "Okafor", "tokafor@riverside.example", "technician", "Clinical Engineering", "active", 26),
    ("Lena", "Kowalski", "lkowalski@riverside.example", "technician", "Clinical Engineering", "active", 2),
    ("Angela", "Ruiz", "aruiz@riverside.example", "requester", "ICU", "active", 30),
    ("Devon", "Park", "dpark@riverside.example", "requester", "ED", "active", 80),
    ("Maria", "Santos", "msantos@riverside.example", "requester", "Central Sterile", "invited", None),
    ("Jordan", "Lee", "jlee@riverside.example", "analyst", "Finance", "active", 100),
    ("Sam", "Whitaker", "swhitaker@riverside.example", "analyst", "Quality and Patient Safety", "active", 40),
    ("Philips", "field service", "fse-riverside@philips.example", "vendor", "External vendor", "active", 170),
    ("Chris", "Nolan", "cnolan@riverside.example", "technician", "Clinical Engineering", "deactivated", 2700),
]
PROBLEMS = ["Battery will not hold charge", "Occlusion alarm with no occlusion", "Screen flickers intermittently", "Error code on startup",
            "Pump door latch loose", "No waveform on lead II"]


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
        if not created and Asset.unscoped.filter(tenant=tenant).exists():  # unscoped: idempotency check before entering context
            self.stdout.write("Demo tenant already seeded.")
            return
        create_default_roles(tenant)
        with tenant_context(tenant):
            director = Role.objects.get(slug="director")
            user, _ = User.objects.get_or_create(username="kim@riverside.example",
                                                 defaults={"email": "kim@riverside.example", "first_name": "Kim", "last_name": "Alvarez",
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
            for first, last, email, slug, dept, status, hours in USERS:
                u = User(username=email, email=email, first_name=first, last_name=last, tenant=tenant, role=Role.objects.get(slug=slug), department=dept,
                         is_invited=status == "invited", is_active=status != "deactivated",
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
                                                  estimated_hours=1.5 if cost > 20000 else 0.75,
                                                  checklist=["Visual inspection and cleaning", "Electrical safety test per IEC 62353",
                                                             "Functional test per service manual", "Alarm verification", "Apply PM sticker and document"])
                dm = DeviceModel.objects.create(manufacturer=mfr, model=model, description=desc, category=cat, risk_class=risk, oem_pm_interval_months=pm,
                                                expected_life_years=life, list_cost=cost, pm_procedure=proc)
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
            # six months of closed work orders, plus a small open backlog
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
                        change_status(wo, "completed", as_of=done)
                        change_status(wo, "closed", as_of=done)
            # a recall that matches the fleet
            alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-2026-4408", classification="Class II", manufacturer="BD",
                                         product="Alaris 8015 PCU infusion pump", model_terms=["Alaris 8015"], title="Keypad membrane may allow fluid ingress",
                                         action="Inspect keypad; replace per service bulletin", published_on=today - timedelta(days=9))
            AlertMatch.objects.create(alert=alert, device_model=DeviceModel.objects.get(model="Alaris 8015 PCU"))
        self.stdout.write(self.style.SUCCESS(f"Seeded {tenant.name}: {len(assets)} devices, {len(techs)} technicians. "
                                             "Sign in as kim@riverside.example / DemoPass-2026"))
