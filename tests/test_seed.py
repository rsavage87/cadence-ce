"""The demo seed is what every screenshot and smoke test starts from; make sure it still builds every state the screens show."""
from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command

from apps.accounts import people
from apps.accounts.backends import find_account
from apps.accounts.models import User
from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset
from apps.facility.services import get_settings
from apps.recalls import services as rc
from apps.recalls.models import AlertMatch
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WorkOrder, WoType


def test_seed_demo_builds_the_demo_tenant(db):
    out = StringIO()
    call_command("seed_demo", stdout=out)
    tenant = Tenant.objects.get(slug="riverside")
    assert "Sign in as kim@riverside.example" in out.getvalue()
    with tenant_context(tenant):
        assert User.objects.filter(tenant=tenant).count() == 14 and Contract.objects.count() == 6
        assert rc.group_counts() == {"all": 4, "action": 2, "progress": 1, "closed": 1}
        in_progress = AlertMatch.objects.get(status=AlertMatch.Status.IN_PROGRESS)
        p = rc.progress(in_progress)
        assert p["total"] == 60 and p["completed"] == 20 and WorkOrder.objects.filter(type=WoType.RECALL, alert=in_progress.alert).count() == 60
        closed = AlertMatch.objects.get(status=AlertMatch.Status.CLOSED)
        assert closed.closed_on == date.today() - timedelta(days=40) and closed.disposition_note.startswith("Gaskets replaced")
        s = get_settings()
        assert s.pk and s.portal_hotline == "ext. 4400" and s.repair_budget_monthly == 52000 and s.portal_require_callback
    call_command("seed_demo", stdout=StringIO())  # a second run is a no-op
    assert Tenant.objects.count() == 2  # Riverside and Kim's North Campus (slice 22)


def test_seed_demo_gives_kim_a_second_facility(db, client):
    """Slice 22: Kim directs Riverside North Campus too, as the same person: linked, joined with her one password, and signed in
    to Riverside last, so a sign-in lands there and the facility menu switches to the North Campus."""
    out = StringIO()
    call_command("seed_demo", stdout=out)
    riverside, north = Tenant.objects.get(slug="riverside"), Tenant.objects.get(slug="riverside-north")
    assert north.name == "Riverside North Campus" and "Seeded Riverside North Campus: 16 devices, 2 technicians" in out.getvalue()
    kim = User.objects.get(username="kim@riverside.example")
    there = User.objects.get(tenant=north, email="kim@riverside.example")
    assert kim.person and there.person == kim.person and there.username == "kim@riverside.example@riverside-north"
    assert people.is_joined(there) and there.password == kim.password and there.check_password("DemoPass-2026")
    assert find_account("kim@riverside.example") == kim
    assert [(m["name"], m["current"], m["invited"]) for m in people.facility_menu(kim)] == [
        ("Riverside North Campus", False, False), ("Riverside Regional Medical Center", True, False)]
    with tenant_context(north):
        assert there.role.slug == "director" and Asset.objects.count() == 16 and Technician.objects.count() == 2
        assert WorkOrder.objects.count() == 7 and WorkOrder.objects.filter(status="closed").count() == 4
        assert WorkOrder.objects.filter(type=WoType.PM, status="closed").exclude(pm_result="").count() == 2
        assert User.objects.filter(tenant=north).count() == 3
    with tenant_context(riverside):
        assert User.objects.filter(tenant=riverside).count() == 14  # the North Campus's staff are its own
    assert client.post("/login/", {"username": "kim@riverside.example", "password": "DemoPass-2026"}).status_code == 302
    r = client.post("/account/facility/", {"account": there.pk, "screen": "workorders"})
    assert r.status_code == 302 and int(client.session["_auth_user_id"]) == there.pk
    call_command("seed_demo", stdout=StringIO())  # a second run adds nothing
    assert User.objects.filter(email="kim@riverside.example").count() == 2
    with tenant_context(north):
        assert Asset.objects.count() == 16


def test_seed_demo_gives_the_survey_binder_a_few_deliberate_items(db):
    """Slice 25: work done by technicians credentialed for the device, one credential that lapsed and was renewed with a repair done
    in the lapse, a reason on every late life-support and high-risk PM but the latest, three new devices (one gap), one model past its
    yearly risk review, and recall matches dated by when their notice was published."""
    from django.utils import timezone

    from apps.core.days import local_day
    from apps.credentials.models import Credential
    from apps.credentials.services import qualification
    from apps.equipment.models import AddedAs, RiskClass
    from apps.pm.services import missed_pms
    from apps.reports.survey import CHECK, FINDING, GAP, default_period, inspections, inventory

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        lapse = Credential.objects.select_related("technician").get(technician__name=seed_demo.LAPSE[0], scope=seed_demo.LAPSE[1],
                                                                    value=seed_demo.LAPSE[2])
        expired = today - timedelta(days=seed_demo.LAPSE_EXPIRED_DAYS_AGO)
        renewed = today - timedelta(days=seed_demo.LAPSE_RENEWED_DAYS_AGO)
        assert [(r.history_type, local_day(r.history_date), r.expires_on) for r in lapse.history.order_by("history_date")] == [
            ("+", lapse.issued_on, expired), ("~", renewed, lapse.expires_on)]
        in_lapse = WorkOrder.objects.filter(assigned_to=lapse.technician, asset__device_model__category=seed_demo.LAPSE[2],
                                            completed_on__gt=expired, completed_on__lt=renewed)
        assert in_lapse.filter(problem=seed_demo.LAPSE_PROBLEM, status="closed").count() == 1
        work = WorkOrder.objects.filter(assigned_to__isnull=False, vendor_service=False).select_related("asset__device_model", "assigned_to")
        work = work.prefetch_related("assigned_to__credentials")
        assert all(qualification(w.assigned_to, w.asset, w.opened_on).ok for w in work)  # with the lapse renewed: everyone's credentialed
        late = missed_pms(today).filter(asset__device_model__risk_class__in=(RiskClass.LIFE_SUPPORT, RiskClass.HIGH)).order_by("due_on", "number")
        assert len(late) >= 2 and all(w.late_reason for w in late[: len(late) - 1]) and late[len(late) - 1].late_reason == ""
        p = default_period(today)
        new = inspections.build(p, None)
        assert [(g.kind, g.record) for g in new.gaps] == [(GAP, "CE-11003")]
        figures = {f.label: f.value for f in new.figures}
        assert figures["New devices added"] == 3 and figures["Inspected before first use"] == 1 and figures["Waiting: never in service yet"] == 1
        assert Asset.objects.filter(added_as=AddedAs.NEW).count() == 3
        assert [(g.kind, g.record) for g in inventory.build(p, None).gaps] == [(CHECK, "Zoll R Series Plus")]
        assert not [g for g in new.gaps + inventory.build(p, None).gaps if g.kind == FINDING]
        assert all(local_day(m.created_at) == m.alert.published_on for m in AlertMatch.objects.select_related("alert"))


def test_a_database_seeded_before_slice_22_gets_the_north_campus(db, monkeypatch):
    monkeypatch.setattr(seed_demo.Command, "_north_campus", lambda self, tenant, opts: None)
    call_command("seed_demo", stdout=StringIO())
    assert not Tenant.objects.filter(slug="riverside-north").exists()
    monkeypatch.undo()
    out = StringIO()
    call_command("seed_demo", stdout=out)
    assert "already seeded" in out.getvalue() and "Seeded Riverside North Campus" in out.getvalue()
    assert people.is_joined(User.objects.get(email="kim@riverside.example", tenant__slug="riverside-north"))
