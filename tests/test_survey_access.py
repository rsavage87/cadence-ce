"""
Who sees the survey binder (slice 25, part E), and what it never shows.

Access: Reports View opens the binder, and each section needs its own areas' View (apps.reports.permissions.survey_refusal), asked the
same way by the page, the section CSVs, the gaps CSV, the print, and the API, so none is the weaker door. Per default role: the
director and the CE manager see all seven sections; a technician and Finance and quality six (Technician qualifications needs Users
and access View); a clinical requester and a vendor technician are refused everywhere (no Reports, and scoped: the binder covers the
whole facility), as is a scoped custom role with Full everywhere. A link naming another facility answers with web_view's switch page.

The requester-text canary: a marker planted in everything a requester (or a work order's free text) carries: a work order's problem,
requester, callback, reported location, and resolution, a work order note, a portal request, and a device's notes. It appears in nothing
the binder makes, over the REAL sections: the page, every section's every table CSV, the gaps CSV, the print, and both API endpoints.
"""
import uuid
from datetime import timedelta

import pytest
from csvutil import csv_text
from django.utils import timezone
from survey_helpers import fake_section, fake_sections

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.equipment import services as eq
from apps.equipment.models import Asset
from apps.reports import survey
from apps.reports.survey import GAP, Gap
from apps.tenants.models import Tenant
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import add_note, assign, change_status, create_service_request, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
ALL = ["program", "inventory", "maintenance", "aem", "inspections", "recalls", "staff"]
SEES = {"director": ALL, "manager": ALL, "technician": [k for k in ALL if k != "staff"], "analyst": [k for k in ALL if k != "staff"]}
STAFF_REFUSAL = "Needs Users and access View, which your role does not have."


@pytest.fixture
def fakes(monkeypatch):
    """Every section a fake with one table ("main") and one gap naming its key."""
    fake_sections(monkeypatch, {key: fake_section(key, gaps=[Gap(GAP, f"Gap in {key}", f"/settings/?{key}", key)]) for key in ALL})


def sign_in(client, make_user, slug, **fields):
    user = make_user(slug, username=f"{slug}-{uuid.uuid4().hex[:6]}@riverside.example")
    for name, value in fields.items():
        setattr(user, name, value)
    user.save()
    client.force_login(user)
    return user


def every_url(key: str = "program") -> list[str]:
    return ["/reports/survey/", "/reports/survey/gaps.csv", f"/reports/survey/{key}/main.csv", "/print/survey/", "/api/v1/survey/",
            f"/api/v1/survey/{key}/"]


# --- per default role --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", sorted(SEES))
def test_each_role_sees_the_sections_its_levels_allow(client, make_user, fakes, role):
    sign_in(client, make_user, role)
    sees = SEES[role]
    left_out = [k for k in ALL if k not in sees]
    r = client.get("/reports/survey/")
    assert r.status_code == 200
    cards = r.context["cards"]
    assert [c["key"] for c in cards] == ALL and [c["key"] for c in cards if not c["left_out"]] == sees
    assert [c["left_out"] for c in cards if c["left_out"]] == [STAFF_REFUSAL] * len(left_out)
    body = r.content.decode()
    assert ("This binder is incomplete for your role" in body) is bool(left_out)
    assert all((f'id="sv-{k}"' in body) is (k in sees) for k in ALL)
    # the print: a page per section it may see, the others named on the cover with why
    body = client.get("/print/survey/").content.decode()
    assert body.count('<section class="sheet svp-sec"') == len(sees) and all((f'id="sv-{k}"' in body) is (k in sees) for k in ALL)
    assert (f"Left out: {STAFF_REFUSAL}" in body) is bool(left_out)
    # the gaps CSV: only the gaps of the sections it may see
    text = csv_text(client.get("/reports/survey/gaps.csv"))
    assert all((f"Gap in {k}" in text) is (k in sees) for k in ALL)
    # each section's CSV and API: its rows, or a 403 in the same words
    for key in ALL:
        csv_r, api_r = client.get(f"/reports/survey/{key}/main.csv"), client.get(f"/api/v1/survey/{key}/")
        if key in sees:
            assert csv_r.status_code == 200 and api_r.status_code == 200 and api_r.json()["key"] == key, key
        else:
            assert csv_r.status_code == 403 and csv_r.content.decode() == STAFF_REFUSAL, key
            assert api_r.status_code == 403 and api_r.json()["detail"] == STAFF_REFUSAL, key
    data = client.get("/api/v1/survey/").json()
    assert [s["key"] for s in data["sections"]] == sees and [lo["key"] for lo in data["left_out"]] == left_out
    assert data["complete"] is (not left_out)


@pytest.mark.parametrize("slug, fields", [("requester", {"department": "ICU"}), ("vendor", {"company": "Hamilton Medical"})])
def test_requesters_and_vendors_are_refused_everywhere(client, make_user, fakes, slug, fields):
    sign_in(client, make_user, slug, **fields)
    for key in ALL:
        for url in every_url(key):
            assert client.get(url).status_code == 403, url
            assert client.get(url, **HX).status_code == 403, url
    assert client.get("/reports/").status_code == 403


def test_a_scoped_role_with_full_levels_is_refused_too(client, ctx, fakes):
    """Reports View or more is not enough: the binder covers the whole facility, which a scoped role never reads."""
    role = Role.objects.create(name="Scoped full", slug="scoped-full", scope=DataScope.COMPANY)
    role.set_levels({m: Level.FULL for m in Module.values})
    user = User.objects.create_user(username="scoped@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role, company="Acme")
    client.force_login(user)
    for url in every_url():
        r = client.get(url)
        assert r.status_code == 403, url
    assert "part of this facility" in client.get("/api/v1/survey/").json()["detail"]


def test_a_link_for_another_facility_offers_the_switch(client, tenant, fakes):
    """A binder link (a print or CSV opened from a tab left in another facility, or shared) names its facility: a person whose
    browser is in Riverside gets the switch page, never Riverside's binder."""
    lakeside = Tenant.objects.create(name="Lakeside Surgery Center", slug="lakeside")
    create_default_roles(lakeside)
    person = uuid.uuid4()
    accounts = []
    for t, username in ((tenant, "kim@health.example"), (lakeside, "kim@health.example@lakeside")):
        role = Role.unscoped.get(tenant=t, slug="director")  # unscoped: made outside any facility
        accounts.append(User.objects.create_user(username=username, email="kim@health.example", password="Test-Pass-2026-x", tenant=t, role=role,
                                                 person=person, first_name="Kim", last_name="Alvarez"))
    client.force_login(accounts[0])
    query = "from=2026-01-01&to=2026-06-30&facility=lakeside"
    for path in (f"/reports/survey/?{query}", f"/print/survey/?{query}", f"/reports/survey/gaps.csv?{query}", f"/reports/survey/program/main.csv?{query}"):
        r = client.get(path)
        body = r.content.decode()
        assert r.status_code == 200 and "This link is for Lakeside Surgery Center" in body and "Gap in program" not in body, path
        assert f'name="next" value="{path.replace("&", "&amp;")}"' in body
    r = client.get("/reports/survey/?from=2026-01-01&to=2026-06-30&facility=riverside")  # a link for here opens it
    assert r.status_code == 200 and "Gap in program" in r.content.decode()


# --- the requester-text canary, over the real sections -----------------------------------------------------------------------

MARKER = "Zq7Canary"  # short enough for every column it is planted in (callback, room)


@pytest.fixture
def planted(ctx, dept, vent_model, pump_model, techs, pump_recall):
    """A facility with work of every kind the sections read, each carrying the marker in its requester-typed text."""
    today = timezone.localdate()  # the facility's (ctx)
    vent = Asset.objects.create(tag="CE-20001", device_model=vent_model, department=dept, next_pm_on=today - timedelta(days=20), notes=MARKER)
    pump = Asset.objects.create(tag="CE-20002", device_model=pump_model, department=dept, next_pm_on=today + timedelta(days=40), notes=MARKER)
    new = eq.create_asset(tag="CE-20003", device_model=pump_model, department=dept, notes=MARKER)  # new: waits for its incoming inspection
    texts = {"problem": MARKER, "requester": MARKER, "callback": MARKER, "reported_location": MARKER}
    # a life-support PM open past its due date, and one completed late that failed (it opens a repair)
    create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, opened_on=today - timedelta(days=60), due_on=today - timedelta(days=20),
                      **texts)
    late = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, opened_on=today - timedelta(days=90),
                             due_on=today - timedelta(days=50), **texts)
    assign(late, technician=techs["tom"])
    complete_work_order(late, pm_result="fail", resolution=MARKER, today=today - timedelta(days=40))
    # a repair from the portal, worked and completed with a note; an incoming inspection opened on the new device
    sr = create_service_request(asset=pump, department=dept, problem=MARKER, urgency="normal", requester_name=MARKER, callback=MARKER, room=MARKER)
    repair = sr.work_order
    assign(repair, technician=techs["dana"])
    add_note(repair, MARKER)
    change_status(repair, WoStatus.IN_PROGRESS)
    complete_work_order(repair, resolution=MARKER)
    create_work_order(asset=new, type=WoType.INSPECTION, priority=Priority.NORMAL, **texts)
    assert WorkOrder.objects.filter(problem=MARKER).count() >= 4 and Asset.objects.filter(notes=MARKER).count() == 3
    return {"today": today}


def test_requester_text_appears_in_nothing_the_binder_makes(client, make_user, planted):
    sign_in(client, make_user, "director")
    periods = ["", f"?from={(planted['today'] - timedelta(days=200)).isoformat()}&to={planted['today'].isoformat()}"]
    for q in periods:
        seen = []

        def get(url, **headers):
            r = client.get(url, **headers)
            assert r.status_code == 200, (url, r.status_code)
            text = csv_text(r) if getattr(r, "streaming", False) else r.content.decode()
            assert MARKER not in text, url
            seen.append(url)
            return r

        get(f"/reports/survey/{q}")
        get(f"/reports/survey/{q}", HTTP_HX_TARGET="survey-body", **HX)
        get(f"/reports/survey/gaps.csv{q}")
        get(f"/print/survey/{q}")
        binder = get(f"/api/v1/survey/{q}").json()
        assert [s["key"] for s in binder["sections"]] == survey.section_keys() and binder["left_out"] == []
        for s in binder["sections"]:
            get(f"/api/v1/survey/{s['key']}/{q}")
            for t in s["tables"]:
                get(f"/reports/survey/{s['key']}/{t['key']}.csv{q}")
        assert len(seen) >= 5 + len(survey.section_keys())
