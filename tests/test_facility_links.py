"""
Facility links (slice 22, part C): a person may work in several facilities, where record numbers repeat (WO-26-0001 is a work order
in each), so every link in a staff email names its facility (`?facility=<slug>`, apps.accounts.people.with_facility) and so does every
subject. A full-page GET of a link for another facility (apps.web.decorators.web_view) never opens this facility's record: it offers
the switch when the person may open that facility, and is a 404 otherwise. HTMX requests, form posts, and links for this facility
change nothing. After sign-in a person with several facilities follows `next` only when it names its facility; else the Overview.
"""
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from django.conf import settings as django_settings
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from test_rls_paths import rls  # noqa: F401

from apps.accounts.models import Role, User, create_default_roles
from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel
from apps.notifications import daily
from apps.notifications.models import NotificationPreference
from apps.reports import subscriptions as subs
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WoType
from apps.workorders.services import assign, create_work_order

PASSWORD = "Test-Pass-2026-x"
EMAIL = "kim@health.example"
APP = "https://ce.example.org"
MON = date(2026, 10, 5)
OURS, THEIRS = "CE-10001", "LS-2001"  # the device of each facility's WO-26-0001
PROBLEM = "Patient Jane Roe in bed 4: alarm keeps sounding"  # free text a requester typed: never in an email


@pytest.fixture(autouse=True)
def _setup(settings):
    """Links start with APP_BASE_URL; the fast hasher keeps sign-ins quick (the logic under test is the same)."""
    settings.APP_BASE_URL = APP
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def lakeside(db):
    t = Tenant.objects.create(name="Lakeside Surgery Center", slug="lakeside")
    create_default_roles(t)
    return t


def account(tenant, slug, *, username, person=None, pending=False, email=EMAIL, **extra):
    role = Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: made outside any facility
    user = User(username=username, email=email, first_name="Kim", last_name="Alvarez", tenant=tenant, role=role, person=person,
                is_invited=pending, **extra)
    if pending:
        user.set_unusable_password()
    else:
        user.set_password(PASSWORD)
    user.last_login = None if pending else timezone.now()
    user._password = None  # created, not changed: nothing to share yet
    user.save()
    return user


@pytest.fixture
def kim(tenant, lakeside):
    """Kim: director at Riverside (signed in there last), manager at Lakeside (joined)."""
    person = uuid.uuid4()
    a = account(tenant, "director", username=EMAIL, person=person)
    b = account(lakeside, "manager", username=f"{EMAIL}@lakeside", person=person)
    User.objects.filter(pk=b.pk).update(last_login=a.last_login - timedelta(days=1))
    return a, User.objects.get(pk=b.pk)


@pytest.fixture
def numbers(tenant, lakeside):
    """WO-26-0001 at each facility: Riverside's on CE-10001, Lakeside's on LS-2001."""
    out = {}
    for t, tag, name in ((tenant, OURS, "ICU ventilator"), (lakeside, THEIRS, "Surgical camera")):
        with tenant_context(t):
            dm = DeviceModel.objects.create(manufacturer="Acme", model=f"M-{tag}", description=name, category="Devices")
            asset = Asset.objects.create(tag=tag, device_model=dm, department=Department.objects.create(name="ICU"))
            out[t.slug] = create_work_order(asset=asset, type=WoType.REPAIR, priority="high", problem=PROBLEM, opened_on=MON - timedelta(days=3),
                                            due_on=MON)
    assert out["riverside"].number == out["lakeside"].number
    return out


def _path(url: str) -> str:
    assert url.startswith(APP + "/"), url
    return url[len(APP):]


# --- the emails ---------------------------------------------------------------------------------------------------------------

def test_every_link_in_a_staff_email_names_its_facility(kim, numbers, lakeside, mailoutbox, django_capture_on_commit_callbacks):
    """Kim's emails from Lakeside (an assignment, the daily digest, a contract reminder, a scheduled report): every link carries
    facility=lakeside, every subject names Lakeside, and none repeats what a requester typed."""
    _a, b = kim
    wo = numbers["lakeside"]
    with tenant_context(lakeside):
        tech = Technician.objects.create(name="Kim Alvarez", user=b)
        NotificationPreference.objects.update_or_create(user=b, defaults={"daily_digest": True})
        Contract.objects.create(reference="SC-LS-1", vendor="Stryker", start_on=MON - timedelta(days=300), end_on=MON + timedelta(days=28))
        Asset.objects.filter(pk=wo.asset_id).update(contract=Contract.objects.get())
        with django_capture_on_commit_callbacks(execute=True):
            assign(wo, technician=tech)
        sub = subs.set_subscription(b, "replace", "weekly")
    assert subs.send_report_email(sub, MON) is True
    daily.send_due(MON, facility=lakeside)

    assert sorted(m.subject.split(" · ")[0].split(":")[0] for m in mailoutbox) == [
        "Assigned to you", "Replacement planning", "Service contract SC-LS-1 ends in 28 days", "Your work for Mon, Oct 5"]
    for m in mailoutbox:
        assert m.to == [EMAIL] and " · Lakeside Surgery Center" in m.subject, m.subject
        urls = re.findall(r"https?://\S+", m.body)
        assert len(urls) >= 2, m.subject  # a record or list, and the preferences or the report
        for url in urls:
            assert parse_qs(urlsplit(url).query)["facility"] == ["lakeside"], url
        assert PROBLEM not in m.body and "Jane Roe" not in m.subject
    [assigned] = [m for m in mailoutbox if m.subject.startswith("Assigned to you")]
    assert assigned.subject == f"Assigned to you: {wo.number} · {THEIRS}, Surgical camera · due Oct 5 · Lakeside Surgery Center"
    assert f"Open it: {APP}/work-orders/{wo.number}/?facility=lakeside\n" in assigned.body
    [digest] = [m for m in mailoutbox if m.subject.startswith("Your work")]
    assert f"{APP}/my-work/?facility=lakeside\n" in digest.body  # slice 24: the digest's "all your open work" is My work


def test_an_emailed_link_for_the_other_facility_offers_the_switch_and_opens_its_own_record(kim, numbers, lakeside, client,
                                                                                         django_capture_on_commit_callbacks, mailoutbox):
    a, b = kim
    with tenant_context(lakeside):
        tech = Technician.objects.create(name="Kim Alvarez", user=b)
        with django_capture_on_commit_callbacks(execute=True):
            assign(numbers["lakeside"], technician=tech)
    path = _path(re.search(r"Open it: (\S+)", mailoutbox[0].body).group(1))
    assert path == f"/work-orders/{numbers['lakeside'].number}/?facility=lakeside"

    client.force_login(a)  # in Riverside, where the same number is another work order
    r = client.get(path)
    body = r.content.decode()
    assert r.status_code == 200 and "This link is for Lakeside Surgery Center" in body and "Switch to Lakeside Surgery Center to open it" in body
    assert OURS not in body and THEIRS not in body and "web/workorders.html" not in [t.name for t in r.templates]  # neither record
    assert 'action="/account/facility/"' in body and f'name="account" value="{b.pk}"' in body and f'name="next" value="{path}"' in body
    assert int(client.session["_auth_user_id"]) == a.pk  # nothing switched until the person chooses it

    r = client.post("/account/facility/", {"account": b.pk, "next": path})
    assert r.status_code == 302 and r["Location"] == path and int(client.session["_auth_user_id"]) == b.pk
    body = client.get(path).content.decode()
    assert THEIRS in body and OURS not in body  # Lakeside's own WO-26-0001
    my_work = client.get("/my-work/?facility=lakeside")  # the digest's My work link (slice 24), now here
    assert my_work.status_code == 200 and numbers["lakeside"].number in my_work.content.decode()


def test_a_link_for_this_facility_or_none_changes_nothing(kim, numbers, client):
    a, _b = kim
    client.force_login(a)
    number = numbers["riverside"].number
    for query in ("", "?facility=riverside", "?facility=", "?facility=riverside&facility="):
        r = client.get(f"/work-orders/{number}/{query}")
        assert r.status_code == 200 and OURS in r.content.decode(), query


def test_htmx_requests_and_posts_are_answered_here(kim, numbers, client, tenant):
    """A drawer or a list re-fetch comes from a page already open in this facility (a tab left in another reloads first), and a
    form post from a page that was let through: neither is a link from an email."""
    a, _b = kim
    client.force_login(a)
    number = numbers["riverside"].number
    r = client.get(f"/work-orders/{number}/?facility=lakeside", HTTP_HX_REQUEST="true", HTTP_X_CADENCE_FACILITY=str(tenant.pk))
    assert r.status_code == 200 and OURS in r.content.decode() and "This link is for" not in r.content.decode()
    r = client.post("/account/password/?facility=lakeside", {})
    assert r.status_code == 200 and "This link is for" not in r.content.decode() and r.context["form"].errors


def test_the_link_page_comes_before_this_facilitys_levels_and_scope(tenant, lakeside, client):
    """Vic is a vendor technician at Riverside (sees only his company's work orders, no Contracts or Settings) and a manager at
    Lakeside: a Lakeside contract or settings link offers the switch instead of Riverside's refusal."""
    person = uuid.uuid4()
    vic = account(tenant, "vendor", username="vic@health.example", email="vic@health.example", person=person, company="Acme Service")
    account(lakeside, "manager", username="vic@health.example@lakeside", email="vic@health.example", person=person)
    client.force_login(vic)
    assert client.get("/contracts/").status_code == 403
    for path in ("/contracts/?facility=lakeside", "/settings/?facility=lakeside", "/reports/replace/?facility=lakeside"):
        r = client.get(path)
        assert r.status_code == 200 and "This link is for Lakeside Surgery Center" in r.content.decode(), path


def test_a_link_for_a_facility_the_person_cannot_open_is_a_404(kim, numbers, client, other_tenant, make_user, lakeside):
    a, b = kim
    number = numbers["riverside"].number
    client.force_login(a)
    for query in ("facility=other", "facility=nowhere", "facility=Lakeside", "facility=lakeside&facility=other",
                  "facility=riverside&facility=lakeside"):
        assert client.get(f"/work-orders/{number}/?{query}").status_code == 404, query
    assert client.head(f"/work-orders/{number}/?facility=other").status_code == 404
    User.objects.filter(pk=b.pk).update(is_active=False)  # deactivated at Lakeside
    assert client.get(f"/work-orders/{number}/?facility=lakeside").status_code == 404
    User.objects.filter(pk=b.pk).update(is_active=True)
    Tenant.objects.filter(pk=lakeside.pk).update(is_active=False)  # Lakeside deactivated
    assert client.get(f"/work-orders/{number}/?facility=lakeside").status_code == 404
    Tenant.objects.filter(pk=lakeside.pk).update(is_active=True)
    assert client.get(f"/work-orders/{number}/?facility=lakeside").status_code == 200
    client.force_login(make_user("director"))  # someone in Riverside alone
    assert client.get(f"/work-orders/{number}/?facility=lakeside").status_code == 404
    assert client.get(f"/work-orders/{number}/?facility=riverside").status_code == 200


def test_a_link_for_a_pending_invitation_offers_to_join_it(tenant, lakeside, client):
    person, email = uuid.uuid4(), "ines@health.example"
    a = account(tenant, "director", username=email, person=person, email=email)
    b = account(lakeside, "technician", username=f"{email}@lakeside", person=person, pending=True, email=email)
    client.force_login(a)
    body = client.get("/work-orders/?facility=lakeside").content.decode()
    assert "This link is for Lakeside Surgery Center" in body and ">Join Lakeside Surgery Center</button>" in body
    r = client.post("/account/facility/", {"account": b.pk, "next": "/work-orders/?facility=lakeside"})
    assert r["Location"] == "/work-orders/?facility=lakeside" and int(client.session["_auth_user_id"]) == b.pk
    b.refresh_from_db()
    assert b.has_usable_password() and b.last_login is not None


def test_a_link_opened_signed_out_signs_in_then_offers_the_switch(kim, numbers, client):
    a, b = kim
    path = f"/work-orders/{numbers['lakeside'].number}/?facility=lakeside"
    r = client.get(path)
    assert r.status_code == 302 and r["Location"] == f"/login/?next={quote(path, safe='/')}"
    r = client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": path})
    assert r["Location"] == path and int(client.session["_auth_user_id"]) == a.pk  # Riverside: used last
    assert "This link is for Lakeside Surgery Center" in client.get(path).content.decode()
    client.logout()
    User.objects.filter(pk=b.pk).update(last_login=timezone.now())  # Lakeside used last: the link opens at once
    client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": path})
    assert THEIRS in client.get(path).content.decode()


# --- sign-in's next -----------------------------------------------------------------------------------------------------------

def test_after_sign_in_a_person_in_several_facilities_follows_only_a_next_that_names_its_facility(kim, client):
    a, b = kim

    def lands(next_url):
        client.logout()
        r = client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": next_url})
        assert r.status_code == 302, next_url
        return r["Location"]

    assert lands("/work-orders/WO-26-0001/") == "/"  # which facility's? The Overview
    assert lands("/equipment/") == "/" and lands("/work-orders/?facility=") == "/"
    assert lands("/work-orders/WO-26-0001/?facility=lakeside") == "/work-orders/WO-26-0001/?facility=lakeside"
    assert lands("/equipment/?status=in_service&facility=riverside") == "/equipment/?status=in_service&facility=riverside"
    assert lands("https://evil.example/?facility=riverside") == "/"
    # opening the sign-in page while signed in: the same rule
    assert client.get("/login/?next=/equipment/")["Location"] == "/"
    assert client.get(f"/login/?next={quote('/equipment/?facility=riverside')}")["Location"] == "/equipment/?facility=riverside"
    # signed out, the page keeps whatever next it was given
    client.logout()
    assert 'name="next" value="/equipment/"' in client.get("/login/?next=/equipment/").content.decode()
    # one facility left that they can enter: next is followed as before
    User.objects.filter(pk=b.pk).update(is_active=False)
    assert lands("/equipment/") == "/equipment/"


def test_a_pending_invitation_counts_as_a_facility_and_one_facility_is_unchanged(tenant, lakeside, client, make_user):
    person, email = uuid.uuid4(), "ines@health.example"
    account(tenant, "director", username=email, person=person, email=email)
    account(lakeside, "technician", username=f"{email}@lakeside", person=person, pending=True, email=email)
    r = client.post("/login/", {"username": email, "password": PASSWORD, "next": "/equipment/"})
    assert r["Location"] == "/"
    client.logout()
    solo = make_user("technician", username="solo@riverside.example")
    r = client.post("/login/", {"username": solo.username, "password": PASSWORD, "next": "/equipment/"})
    assert r["Location"] == "/equipment/"


def test_sign_in_chooses_where_to_land_before_any_facility_is_set(kim, client, rls):  # noqa: F811
    """The redirect is chosen as the sign-in completes, while no facility is set (the request began signed out): it reads only
    User and Tenant, never a tenant-scoped row (tests/test_rls_paths.py's stand-in for the policy)."""
    with rls:
        r = client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": "/equipment/"})
        assert r["Location"] == "/"
        client.logout()
        r = client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": "/equipment/?facility=riverside"})
        assert r["Location"] == "/equipment/?facility=riverside"
    assert rls.violations == []


# --- the parameter is web_view's ----------------------------------------------------------------------------------------------

def test_no_view_reads_a_query_parameter_named_facility():
    """`facility` in a query is the link's facility (apps.web.decorators); a view or form that read it for something else would be
    answered by the link check first."""
    root = Path(django_settings.BASE_DIR) / "apps"
    read = re.compile(r"""(GET|query_params)(\.get|\.getlist|\[)\(?\s*["']facility["']""")  # request.GET.get("facility"), ...
    field = re.compile(r"""name=["']facility["']""")  # a form field of that name in a template
    found = [f"{p.relative_to(root)}:{n}" for glob, pattern in (("*.py", read), ("*.html", field)) for p in root.rglob(glob)
             for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert found == []


# --- under the policies ---------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_link_page_and_the_switch_under_the_policies(kim, numbers, client):
    """Signing in reads the person's facilities with no facility set, and the link page reads only User and Tenant; Lakeside's
    record is read only once the browser works in Lakeside."""
    a, b = kim
    path = f"/work-orders/{numbers['lakeside'].number}/?facility=lakeside"
    as_app_role()
    assert client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": "/equipment/"})["Location"] == "/"
    client.logout()
    assert client.post("/login/", {"username": EMAIL, "password": PASSWORD, "next": path})["Location"] == path
    assert int(client.session["_auth_user_id"]) == a.pk
    body = client.get(path).content.decode()
    assert "This link is for Lakeside Surgery Center" in body and THEIRS not in body and OURS not in body
    assert client.get(f"/work-orders/{numbers['riverside'].number}/?facility=other").status_code == 404
    assert client.post("/account/facility/", {"account": b.pk, "next": path})["Location"] == path
    body = client.get(path).content.decode()
    assert THEIRS in body and OURS not in body
