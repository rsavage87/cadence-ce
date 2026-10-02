"""
Row-level security for real (slice 17): on PostgreSQL (CADENCE_TEST_DATABASE_URL; CI's second test job) the test database has the
policies production runs under (tests/conftest.py's django_db_setup), and these tests switch to the runtime role, which is not the
owner (SET LOCAL ROLE cadence_app, for the rest of the test's transaction), before they act: signing in, the signed-out pages, the
public portal, every screen, the API, scoped users, and the management commands. A query made where no tenant is set sees no rows
(or fails a write), as in production. tests/test_rls_paths.py stands in for this on SQLite. Skipped on SQLite.
"""
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from pg_helpers import as_app_role, needs_postgres

from apps.accounts import invitations, services
from apps.accounts.models import Role, User
from apps.equipment.models import Asset, Department, DeviceModel
from apps.reports.services import REPORTS
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WorkOrder

pytestmark = needs_postgres

PASSWORD = "Test-Pass-2026-x"


@pytest.fixture
def seeded(db):
    call_command("seed_demo", stdout=StringIO())
    return Tenant.objects.get(slug="riverside")


def _user(tenant, slug, email, **extra):
    with tenant_context(tenant):
        role = Role.objects.get(slug=slug)
    return User.objects.create_user(username=email, email=email, password=PASSWORD, tenant=tenant, role=role, **extra)


# --- the policies themselves ---------------------------------------------------------------------------------------------------

def test_the_database_is_postgres_and_the_suite_bypasses_the_policies(db):
    """CADENCE_TEST_DATABASE_URL is set, so this run must be on PostgreSQL, connected as a role the policies do not apply to (the
    rest of the suite builds its data without a tenant set, as on SQLite)."""
    assert connection.vendor == "postgresql"
    with connection.cursor() as cur:
        cur.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone()[0], "connect the suite as a superuser: tests/test_postgres_rls.py switches to the runtime role itself"


def test_no_tenant_sees_nothing_and_a_tenant_sees_only_its_rows(tenant, other_tenant, vent, techs):
    with tenant_context(other_tenant):
        d = Department.objects.create(name="Their ICU")
        m = DeviceModel.objects.create(manufacturer="X", model="Y", description="Z", category="C")
        Asset.objects.create(tag="THEIRS-1", device_model=m, department=d)
    as_app_role()
    with tenant_context(None):  # no tenant set (the ctx fixture left one set)
        assert Asset.unscoped.count() == 0  # unscoped and no tenant: the policy shows nothing
    with tenant_context(tenant):
        assert list(Asset.unscoped.values_list("tag", flat=True)) == [vent.tag]
    with tenant_context(other_tenant):
        assert list(Asset.unscoped.values_list("tag", flat=True)) == ["THEIRS-1"]


def test_a_row_for_another_tenant_cannot_be_written(tenant, other_tenant, vent):
    as_app_role()
    with tenant_context(tenant):
        with pytest.raises(DatabaseError), transaction.atomic():
            Department.unscoped.create(tenant=other_tenant, name="Planted")  # WITH CHECK refuses it
        with pytest.raises(DatabaseError), transaction.atomic():
            Asset.unscoped.filter(pk=vent.pk).update(tenant=other_tenant)  # nor moves a row out
    with tenant_context(other_tenant):
        assert not Department.unscoped.filter(name="Planted").exists()


def test_every_table_with_a_tenant_has_the_policy(db):
    """From the database itself, not from enable_rls's own list: every table with a tenant_id column has row-level security, forced,
    with the tenant_isolation policy. accounts_user is the one exception: the signed-in user is loaded before the tenant is set."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation')
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)""")
        rows = cur.fetchall()
    unprotected = sorted(name for name, rls, forced, policy in rows if not (rls and forced and policy))
    assert len(rows) >= 30 and unprotected == ["accounts_user"], unprotected  # 32 tables when this was written


# --- signed out ----------------------------------------------------------------------------------------------------------------

def test_signing_in_out_and_resetting_a_password(client, tenant, make_user, mailoutbox):
    make_user("technician", username="tech@riverside.example").__class__.objects.filter(username="tech@riverside.example").update(
        email="tech@riverside.example")
    services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz",
                         role=Role.unscoped.get(tenant=tenant, slug="technician"))  # unscoped: test setup, outside any tenant
    as_app_role()
    assert client.get("/login/").status_code == 200
    r = client.post("/login/", {"username": "TECH@riverside.example", "password": PASSWORD})
    assert r.status_code == 302 and client.get(r["Location"]).status_code == 200
    assert client.post("/logout/").status_code == 302
    for email in ["ana@riverside.example", "tech@riverside.example", "nobody@riverside.example"]:
        assert client.post("/password-reset/", {"email": email}).status_code == 302
    assert sorted(m.to[0] for m in mailoutbox) == ["ana@riverside.example", "tech@riverside.example"]
    assert "as Technician" in next(m for m in mailoutbox if m.to == ["ana@riverside.example"]).body


def test_accepting_an_invitation(client, tenant, mailoutbox):
    user = services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz",
                                role=Role.unscoped.get(tenant=tenant, slug="technician"))  # unscoped: test setup
    assert invitations.send_invitation(user)
    path = urlsplit(invitations.invitation_url(user)).path
    as_app_role()
    r = client.get(path)
    assert r.status_code == 302
    assert b"Set your password" in client.get(r["Location"]).content
    done = client.post(r["Location"], {"new_password1": "Kestrel-Harbor-2031", "new_password2": "Kestrel-Harbor-2031"})
    assert done.status_code == 302 and client.get(done["Location"]).status_code == 200


def test_the_public_portal_takes_a_request(client, seeded):
    with tenant_context(seeded):
        asset = Asset.objects.filter(status="in_service").select_related("department").first()
        before = WorkOrder.objects.count()
    as_app_role()
    assert client.get("/r/riverside/").status_code == 200
    r = client.post("/r/riverside/", {"asset_tag": asset.tag, "department": str(asset.department_id), "problem": "Alarm will not silence",
                                      "urgency": "normal", "requester_name": "Unit staff", "callback": "4410"})
    assert r.status_code in (200, 302), r.content.decode()[:500]
    with tenant_context(seeded):
        assert WorkOrder.objects.count() == before + 1


# --- signed in -------------------------------------------------------------------------------------------------------------------

def test_every_screen_renders_under_the_policies(client, seeded):
    director = User.objects.get(username="kim@riverside.example")
    client.force_login(director)
    as_app_role()
    urls = ["/", "/equipment/", "/work-orders/", "/work-orders/?mode=board", "/pm/", "/contracts/", "/recalls/", "/reports/", "/users/",
            "/users/roles/", "/users/credentials/", "/settings/", "/account/password/", "/export/equipment.csv",
            "/export/work-orders.csv", "/export/contracts.csv", "/print/labels/?status=in_service", "/print/route-sheets/"]
    keys = [r["key"] for r in REPORTS]
    urls += [f"/reports/{key}/" for key in keys] + [f"/reports/{key}.csv" for key in keys] + [f"/print/reports/{key}/" for key in keys]
    with tenant_context(seeded):
        wo = WorkOrder.objects.order_by("number").first()
        asset = Asset.objects.order_by("tag").first()
        model = DeviceModel.objects.order_by("manufacturer").first()
    urls += [f"/work-orders/{wo.number}/", f"/print/work-orders/{wo.number}/", f"/equipment/{asset.tag}/", f"/pm/models/{model.pk}/",
             f"/equipment/{asset.tag}/?tab=pm", f"/equipment/{asset.tag}/?tab=costs", f"/pm/models/{model.pk}/?tab=aem"]
    assert client.get("/search/?q=CE-1")["Location"] == "/equipment/?q=CE-1"
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        if hasattr(r, "streaming_content"):
            assert b"".join(r.streaming_content), url  # a CSV is read after the view returns, inside the request's tenant


def test_a_scoped_user_sees_their_share_under_the_policies(client, seeded):
    vendor = User.objects.get(username="fse-riverside@philips.example")
    client.force_login(vendor)
    as_app_role()
    body = client.get("/work-orders/?open=0").content.decode()
    with tenant_context(seeded):
        theirs = set(WorkOrder.objects.filter(vendor_service=True, vendor_name__iexact="Philips").values_list("number", flat=True))
        others = set(WorkOrder.objects.exclude(number__in=theirs).values_list("number", flat=True)[:50])
    assert theirs and all(n in body for n in theirs) and not any(n in body for n in others)
    assert client.get("/reports/").status_code == 403


def test_the_api_under_the_policies(client, seeded):
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    for url in ["/api/v1/assets/", "/api/v1/work-orders/", "/api/v1/device-models/", "/api/v1/contracts/", "/api/v1/settings/",
                "/api/v1/pm/calendar/", "/api/v1/overview/"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
    assert client.get("/api/v1/assets/").json()["count"] == 196


def test_changing_things_under_the_policies(client, seeded):
    """A few writes through the screens: the rows are stamped with the request's tenant and pass WITH CHECK."""
    client.force_login(User.objects.get(username="kim@riverside.example"))
    with tenant_context(seeded):
        asset = Asset.objects.filter(status="in_service").first()
    as_app_role()
    r = client.post("/work-orders/new/", {"asset": asset.tag, "type": "repair", "priority": "normal", "problem": "Screen flickers",
                                          "requester": "Kim"}, HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    with tenant_context(seeded):
        wo = WorkOrder.objects.filter(asset=asset, problem="Screen flickers").get()
    assert client.post(f"/work-orders/{wo.number}/notes/", {"text": "Checked the cable"}, HTTP_HX_REQUEST="true").status_code == 200
    r = client.post("/settings/targets/", {"target_pm_pct": "96", "target_uptime_pct": "99.5", "target_mttr_days": "3",
                                           "repair_budget_monthly": "", "labor_rate": "85", "vendor_labor_rate": "215"}, HTTP_HX_REQUEST="true")
    assert r.status_code == 200


# --- management commands -------------------------------------------------------------------------------------------------------

def test_bootstrap_and_seed_commands(db, mailoutbox):
    as_app_role()
    call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "dir@lakeside.example", "--invite",
                 stdout=StringIO())
    assert len(mailoutbox) == 1 and "as Director" in mailoutbox[0].body
    call_command("seed_demo", stdout=StringIO())
    call_command("seed_demo", stdout=(again := StringIO()))
    assert "already seeded" in again.getvalue()


def test_the_daily_jobs_do_their_work_under_the_policies(seeded, monkeypatch, mailoutbox):
    """Not only that they finish: under the policies a job that read no rows would still finish, so each one's effect is checked.
    generate_pm creates the due PM work orders, import_openfda stores and matches a notice, send_report_emails sends a due report,
    and run_daily_jobs records every job as succeeded."""
    from datetime import date

    from apps.jobs.models import JobRun
    from apps.recalls.models import AlertMatch
    from apps.reports.models import ReportSubscription
    from apps.workorders.models import WoType

    record = {"product_res_number": "Z-2026-9999", "recalling_firm": "Zoll", "product_description": "R Series Plus",
              "reason_for_recall": "Battery may not hold a charge", "event_date_posted": "2026-09-30"}

    class Answer:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"meta": {"results": {"total": 1}}, "results": [record]}

    monkeypatch.setattr("requests.get", lambda *a, **k: Answer())
    kim = User.objects.get(username="kim@riverside.example")
    with tenant_context(seeded):
        ReportSubscription.objects.create(user=kim, report="cosr", frequency="weekly")
        pms_before = WorkOrder.objects.filter(type=WoType.PM).count()
    as_app_role()
    out = StringIO()
    call_command("generate_pm", stdout=out)
    call_command("import_openfda", "--days", "7", stdout=out)
    call_command("send_report_emails", "--date", "2026-09-28", stdout=out)
    with tenant_context(seeded):
        assert WorkOrder.objects.filter(type=WoType.PM).count() > pms_before, out.getvalue()
        assert AlertMatch.objects.filter(alert__external_id="Z-2026-9999", device_model__manufacturer="Zoll").exists(), out.getvalue()
    assert [m.to for m in mailoutbox] == [["kim@riverside.example"]], out.getvalue()
    call_command("run_daily_jobs", "--force", stdout=StringIO())  # the rows say how they went
    assert set(JobRun.objects.filter(run_on=date.today()).values_list("status", flat=True)) == {"succeeded"}


def test_importing_assets_and_the_scheduler_under_the_policies(seeded, tmp_path, monkeypatch):
    csv_file = tmp_path / "inventory.csv"
    csv_file.write_text("Asset Tag,Manufacturer,Model,Department,Room\nCE-90001,Zoll,R Series Plus,ICU,4\nCE-90002,Acme,New One,Oncology,1\n")
    monkeypatch.setattr("apps.jobs.services.close_old_connections", lambda: None)  # one test, one transaction (tests/test_jobs.py)
    monkeypatch.setattr("apps.jobs.management.commands.scheduler.close_old_connections", lambda: None)
    as_app_role()
    out = StringIO()
    call_command("import_assets", "--tenant", "riverside", str(csv_file), stdout=out)
    assert "2 created" in out.getvalue()
    with tenant_context(seeded):
        assert set(Asset.objects.filter(tag__in=["CE-90001", "CE-90002"]).values_list("department__name", flat=True)) == {"ICU", "Oncology"}
    call_command("scheduler", "--once", stdout=StringIO())


def test_the_admin_under_the_policies(client, seeded, django_user_model):
    """A platform superuser in the Django admin: the lists render as the runtime role, and deleting what several facilities' rows
    point at (a user, a notice, a facility) is refused rather than failing at commit."""
    from apps.recalls.models import Alert

    root = django_user_model.objects.create_superuser("root", "root@example.com", PASSWORD)
    client.force_login(root)
    kim = User.objects.get(username="kim@riverside.example")
    alert = Alert.objects.first()
    as_app_role()
    for url in ["/admin/", "/admin/accounts/user/", "/admin/tenants/tenant/", "/admin/recalls/alert/", "/admin/jobs/jobrun/"]:
        assert client.get(url).status_code == 200, url
    for url in [f"/admin/accounts/user/{kim.pk}/delete/", f"/admin/recalls/alert/{alert.pk}/delete/", f"/admin/tenants/tenant/{seeded.pk}/delete/"]:
        assert client.get(url).status_code == 403, url
