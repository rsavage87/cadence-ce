"""
Row-level security for real (slice 17): on PostgreSQL (CADENCE_TEST_DATABASE_URL; CI's second test job) the test database has the
policies production runs under (tests/conftest.py's django_db_setup), and these tests switch to the runtime role, which is not the
owner (SET LOCAL ROLE cadence_app, for the rest of the test's transaction), before they act: signing in, the signed-out pages, the
public portal, every screen, the API, scoped users, and the management commands. A query made where no tenant is set sees no rows
(or fails a write), as in production. tests/test_rls_paths.py stands in for this on SQLite. Skipped on SQLite.
"""
from html import unescape
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from pg_helpers import as_app_role, needs_postgres

from apps.accounts import invitations, services
from apps.accounts.models import Role, User
from apps.equipment.models import Asset, Department, DeviceModel
from apps.reports import survey
from apps.reports.services import REPORTS
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web import views_vendor_reviews
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


# --- slice 39: security decisions on the seed (read as the suite's role, inside the facility) -------------------------------------------

def _record_links(today) -> list:
    """The scopes Needs attention offers Record decision for (rules.Link, a scope with no decision yet), as the system reads them."""
    from apps.cybersecurity import rules

    return [row.record for h in rules.needs_attention(today, user=None).headings for row in h.rows
            if row.record is not None and row.record.query[0][0] != "supersedes"]


def _record_address(link) -> str:
    from urllib.parse import urlencode

    return f"/security/decisions/new/?{urlencode([(k, str(v)) for k, v in link.query])}"


def _scope_of(link) -> dict:
    """record_decision's scope for a Record decision link (asset=<tag>, release=<id>, or model=<id>)."""
    from apps.cybersecurity.models import SoftwareRelease

    key, value = link.query[0]
    if key == "asset":
        return {"asset": Asset.objects.get(tag=value)}
    if key == "release":
        return {"release": SoftwareRelease.objects.get(pk=value)}
    return {"device_model": DeviceModel.objects.get(pk=value)}


def _seeded_decision(tenant):
    """(a live security decision, its sentence, its first device's tag or "", Record decision's address for a scope Needs attention
    offers). The seed's first live decision; when the seed has none yet, one recorded here as the system on the first scope offered, with
    the reasons that hold for it (rules.support_need over its devices)."""
    from django.utils import timezone

    from apps.cybersecurity import decisions, rules
    from apps.cybersecurity.models import AcceptedBy, DecisionResponse, DeviceSecurityDecision

    with tenant_context(tenant):
        today = timezone.localdate()
        offered = _record_links(today)
        assert offered, "the seed has devices past their support with no decision (Needs attention's Record decision)"
        live = DeviceSecurityDecision.objects.filter(ended_on__isnull=True).order_by("number").first()
        if live is None:
            scope = _scope_of(offered[0])
            if "asset" in scope:
                devices = rules.scope_devices(asset=scope["asset"])
            elif "release" in scope:
                devices = rules.scope_devices(device_model=scope["release"].device_model_id, release=scope["release"])
            else:
                devices = rules.scope_devices(device_model=scope["device_model"])
            needs = [rules.support_need(s, today) for s in rules.standings(devices, today).values()]
            reasons = [k for k in rules.REASONS if any(n is not None and n.decidable and k in n.reasons for n in needs)]
            live = decisions.record_decision(**scope, reasons=reasons, response=DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
                                             accepted_by=AcceptedBy.CE_LEADER, by=None, today=today)
        held = rules.decision_devices(live, today)
        return live, rules.decision_words(live, today), held[0].asset.tag if held else "", _record_address(offered[0])


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
    # slice 37: the Device security screen (Fleet, Models, a model's panel, Changes for IT, their CSVs) and the drawer's Security tab
    urls += ["/security/", "/security/export.csv", f"/security/devices.csv?model={model.pk}", "/security/models/",
             f"/security/models/{model.pk}/", "/security/changes/", "/security/changes.csv", f"/equipment/{asset.tag}/?tab=security"]
    # slice 38: the software updates' modals (urls_cybersecurity), on the seed's rows: Open software update on a pump with none open,
    # Record update on the C-arm with its patches, Confirm version on the update completed with the version not confirmed, Entered in
    # error on the one recorded by hand, Open updates to this release on the pumps' newer release; and the update work order's drawer
    with tenant_context(seeded):
        from apps.cybersecurity.models import SoftwareRelease, SoftwareUpdate

        unconfirmed = SoftwareUpdate.objects.get(work_order__status="completed", outcome="")
        hand = SoftwareUpdate.objects.select_related("asset").get(work_order__isnull=True)
        updated = set(SoftwareUpdate.objects.values_list("asset_id", flat=True))
        idle = Asset.objects.filter(device_model__model="Alaris 8015 PCU", status="in_service").exclude(pk__in=updated).order_by("tag").first()
        newer = SoftwareRelease.objects.get(device_model__model="Alaris 8015 PCU", version="5.6 (demo)")
        update_wo = SoftwareUpdate.objects.select_related("work_order").filter(work_order__isnull=False).order_by("work_order__number").first()
    # The modals are opened as htmx opens them (HX-Request): opened directly, each answers with a redirect to the drawer or the panel
    modals = [f"/security/devices/{idle.tag}/updates/open/", f"/security/devices/{hand.asset.tag}/updates/record/",
              f"/security/updates/{unconfirmed.pk}/confirm/", f"/security/updates/{hand.pk}/remove/",
              f"/security/releases/{newer.pk}/open-updates/"]
    urls += [f"/work-orders/{update_wo.work_order.number}/", f"/print/work-orders/{update_wo.work_order.number}/",
             f"/equipment/{hand.asset.tag}/?tab=security"]
    # Slice 38 review, round 2: the open update work order's completion modal (by HTMX, its "Software on the device" with the target),
    # the Security tabs with update rows (the C-arm's patches recorded by hand, the pump whose update was completed as installed), the
    # pumps' panel with the newer release's campaign line, and the change log's software updates area with its CSV and print. Each must
    # show what it reads: a row the policies hid would still answer 200. What they must show is read here as the suite's role (the
    # policies bypassed); the pages below as the runtime role.
    with tenant_context(seeded):
        from apps.cybersecurity import rules
        from apps.web.views_wo_release import LEGEND

        with_words = SoftwareUpdate.objects.select_related("asset", "work_order", "target_release", "release_after")
        open_row = with_words.filter(work_order__status="open").order_by("work_order__number").first()
        installed = with_words.get(outcome="installed", work_order__isnull=False)
        hand_row = with_words.get(pk=hand.pk)
        campaign = rules.campaign_words(newer, rules.campaigns(newer.device_model).get(newer.pk))
        shown = {f"/work-orders/{open_row.work_order.number}/complete/": [LEGEND, rules.update_words(open_row)],
                 f"/equipment/{installed.asset.tag}/?tab=security": [rules.update_words(installed)],
                 f"/equipment/{hand.asset.tag}/?tab=security": [rules.update_words(hand_row)],
                 f"/security/models/{newer.device_model_id}/": [campaign]}
        logged = [f"{installed.asset.tag} · software update {installed.work_order.number}", f"{hand.asset.tag} · software update recorded"]
    assert open_row is not None and campaign.startswith("Updates to 5.6 (demo): ") and all(all(w) for w in shown.values())
    complete = f"/work-orders/{open_row.work_order.number}/complete/"  # the drawer's Mark completed, by HTMX
    modals.append(complete)
    urls += [url for url in shown if url not in modals and url not in urls]
    log = ["/users/log/?area=software_updates", "/users/log/export.csv?area=software_updates", "/users/log/print/?area=software_updates"]
    urls += log
    # Slice 39: the Decisions tab (Needs attention and the list) and its CSV, a decision's page (the full page: the tab with the drawer
    # open), its print and devices' CSV, the change log's decisions area, and the modals (Record decision for a scope Needs attention
    # offers, Review and renew, End, Entered in error): a live decision of the seed's, or one recorded here as the system when it has none
    decision, words, tag, offered = _seeded_decision(seeded)
    page = f"/security/decisions/{decision.number}/"
    shown.update({"/security/decisions/": [decision.number], "/security/decisions/export.csv": [decision.number], page: [decision.number, words],
                  f"{page}print/": [words], f"{page}devices.csv": [tag], "/users/log/?area=device_security_decisions": [decision.number]})
    urls += ["/security/decisions/", "/security/decisions/export.csv", page, f"{page}print/", f"{page}devices.csv",
             "/users/log/?area=device_security_decisions"]
    modals += [offered, f"/security/decisions/new/?supersedes={decision.number}", f"{page}end/", f"{page}in-error/"]
    as_app_role()
    assert client.get("/search/?q=CE-1")["Location"] == "/equipment/?q=CE-1"
    for url in urls + modals:
        r = client.get(url, **({"HTTP_HX_REQUEST": "true"} if url in modals else {}))
        assert r.status_code == 200, (url, r.status_code)
        if hasattr(r, "streaming_content"):
            body = b"".join(r.streaming_content).decode()  # a CSV is read after the view returns, inside the request's tenant
            assert body, url
        else:
            body = r.content.decode()
        body = unescape(body)
        assert all(words in body for words in shown.get(url, [])), (url, [w for w in shown[url] if w not in body])
        assert url not in log or all(name in body for name in logged), (url, [n for n in logged if n not in body])
    for url in [u for u in modals if u != complete]:  # opened directly: the device's drawer, or the model's panel
        r = client.get(url)
        assert r.status_code == 302 and client.get(r["Location"]).status_code == 200, (url, r.status_code)


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
    # the fleet, the four new devices (slices 25 and 26), and the four rentals, loaner, and demo unit (slice 29: one returned)
    assert client.get("/api/v1/assets/").json()["count"] == 204
    # slice 35: the CE department's test equipment (never a device), the list and one item by id
    from apps.demo.management.commands import seed_demo
    from apps.testequipment.models import TestEquipment

    with tenant_context(seeded):
        analyzer = TestEquipment.objects.get(tag="TE-301")
    r = client.get("/api/v1/test-equipment/")
    assert r.status_code == 200 and r.json()["count"] == len(seed_demo.TEST_EQUIPMENT)
    r = client.get(f"/api/v1/test-equipment/{analyzer.pk}/")
    assert r.status_code == 200 and r.json()["tag"] == "TE-301"
    # slice 38: the software updates (the batch's work orders' records and the C-arm's patches recorded by hand), and an update work
    # order's software_update in the rules' words, read as the runtime role
    from apps.cybersecurity.models import SoftwareUpdate

    with tenant_context(seeded):
        installed = SoftwareUpdate.objects.select_related("work_order").get(outcome="installed", work_order__isnull=False)
        rows = SoftwareUpdate.objects.count()
    r = client.get("/api/v1/software-updates/")
    assert r.status_code == 200 and r.json()["count"] == rows >= 4
    assert client.get(f"/api/v1/software-updates/{installed.pk}/").json()["state"] == "done"
    data = client.get(f"/api/v1/work-orders/{installed.work_order.pk}/").json()
    assert data["software_update"]["words"].startswith("5.6 (demo) installed ")


def test_the_survey_binder_under_the_policies(client, seeded):
    """Slice 25: the binder reads every area's rows and their histories (historical tables have no policy: the sections filter them by
    this facility). As the runtime role the page, the print, the gaps CSV, every section's table CSVs (streamed after the view
    returns, inside the request's tenant), and both API endpoints answer, and the seeded binder has something in it."""
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    for url in ["/reports/survey/", "/print/survey/", "/reports/survey/?from=2026-01-01&to=2026-06-30"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
    r = client.get("/reports/survey/gaps.csv")
    assert r.status_code == 200 and b"".join(r.streaming_content).startswith("﻿Section,Kind".encode())
    binder = client.get("/api/v1/survey/").json()
    assert [s["key"] for s in binder["sections"]] == survey.section_keys() and binder["complete"] is True
    tables = 0
    for s in binder["sections"]:
        assert client.get(f"/api/v1/survey/{s['key']}/").status_code == 200, s["key"]
        for t in s["tables"]:
            r = client.get(f"/reports/survey/{s['key']}/{t['key']}.csv")
            assert r.status_code == 200 and b"".join(r.streaming_content), (s["key"], t["key"])
            tables += 1
    assert tables >= len(binder["sections"])  # every section lists something in a seeded facility
    # slice 35: the seeded test equipment, its gaps (the defibrillator analyzer past due, the infusion analyzer's review not recorded), and
    # its rows, read under the policies with the calibrations' and items' history
    section = client.get("/api/v1/survey/testequipment/").json()
    assert [(g["kind"], g["record"]) for g in section["gaps"]] == [("gap", "TE-301"), ("gap", "TE-401"), ("finding", "TE-301")]
    r = client.get("/reports/survey/testequipment/items.csv")
    assert r.status_code == 200 and "TE-301" in b"".join(r.streaming_content).decode()


def test_the_test_equipment_screens_under_the_policies(client, seeded):
    """Slice 35: the Equipment screen's Test equipment tab, its CSV, and a seeded item's page (its drawer opened directly, by its tag in
    any letter case) as the runtime role. Wave 2A builds them behind the scaffold's stubs, which answer 501: until those are merged this
    test is an expected failure, and it runs for real the moment the screens are there."""
    from django.urls import reverse

    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    if client.get(reverse("web:test_equipment")).status_code == 501:
        pytest.xfail("slice 35 wave 2A's screens are not merged yet")
    for url in [reverse("web:test_equipment"), reverse("web:test_equipment_csv"), reverse("web:test_equipment_item", args=["TE-301"]),
                reverse("web:test_equipment_item", args=["te-401"])]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        if hasattr(r, "streaming_content"):
            assert b"".join(r.streaming_content), url  # a CSV is read after the view returns, inside the request's tenant
    assert "TE-301" in client.get(reverse("web:test_equipment")).content.decode()


def _seeded_evaluation(tenant):
    from apps.evaluations.models import ProgramEvaluation

    with tenant_context(tenant):
        return ProgramEvaluation.objects.get()


def test_the_program_evaluation_api_under_the_policies(client, seeded):
    """Slice 32: the seeded draft evaluation over the API as the runtime role: the list, and the evaluation by number with its text,
    objectives, and every figure (Kim may see every area), its previous column and the binder's items read from the stored snapshot."""
    from apps.demo.management.commands import seed_demo
    from apps.evaluations import indicators

    ev = _seeded_evaluation(seeded)
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    listed = client.get("/api/v1/program-evaluations/").json()
    assert [e["number"] for e in listed["results"]] == [ev.number] and listed["results"][0]["status"] == "draft"
    r = client.get(f"/api/v1/program-evaluations/{ev.number}/")
    assert r.status_code == 200, r.content
    data = r.json()
    assert {t["field"]: t["text"] for t in data["text"] if t["text"]} == seed_demo.EVALUATION_TEXT
    assert len(data["objectives"]["review"]) == 2 and len(data["objectives"]["next"]) == 1
    figures = data["figures"]
    assert figures["left_out"] == [] and len(figures["indicators"]) == len(indicators.INDICATORS) and figures["binder_gaps"]
    assert figures["taken_by_name"] == "Rob Feldman"


def test_the_program_evaluation_screens_under_the_policies(client, seeded):
    """Slice 32: the seeded draft evaluation's screens as the runtime role (wave 2A's views): the list, the evaluation (by number and by
    version), and the committee's printable copy."""
    from django.urls import reverse

    ev = _seeded_evaluation(seeded)
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    for url in [reverse("web:evaluations"), reverse("web:evaluation", args=[ev.number]), reverse("web:evaluation_version", args=[ev.number, 1]),
                reverse("web:evaluation_print", args=[ev.number])]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)


def _seeded_vendor_review(tenant):
    from apps.evaluations.models import VendorReview

    with tenant_context(tenant):
        return VendorReview.objects.select_related("snapshot").get()


def test_the_vendor_review_api_under_the_policies(client, seeded):
    """Slice 34: the seeded Philips review over the API as the runtime role: the list, and the review by number with its fields, its
    figures (Kim may see them all), and the previous review (none)."""
    from apps.demo.management.commands import seed_demo

    review = _seeded_vendor_review(seeded)
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    listed = client.get("/api/v1/vendor-reviews/").json()
    assert [r["number"] for r in listed["results"]] == [review.number] and listed["results"][0]["status"] == "approved"
    r = client.get(f"/api/v1/vendor-reviews/{review.number}/")
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["summary"] == seed_demo.VENDOR_REVIEW_FIELDS["summary"] and data["signed_by_name"] == "Rob Feldman"
    assert data["figures"]["left_out"] == [] and data["figures"]["figures"] == review.snapshot.data["figures"] and data["previous"] is None
    assert client.get("/api/v1/vendor-reviews/?vendor=philips%20field%20service").json()["count"] == 1


# Wave 2A builds the Reviews tab's screens behind the scaffold's names; until it is merged they are the scaffold's stubs (this test is
# then expected to fail, strictly: it runs for real, unmarked, the moment the stubs are gone).
SCREENS_PENDING = "Scaffold: wave 2A builds the screens" in (views_vendor_reviews.__doc__ or "")


@pytest.mark.xfail(SCREENS_PENDING, reason="slice 34 wave 2A's screens are not merged yet", strict=True)
def test_the_vendor_review_screens_under_the_policies(client, seeded):
    """Slice 34: the seeded review's screens as the runtime role: the Reviews tab, the review's page and print, the contracted services
    summary print, and the Philips contract's drawer with its review line."""
    from django.urls import reverse

    from apps.contracts.models import Contract

    review = _seeded_vendor_review(seeded)
    with tenant_context(seeded):
        philips = Contract.objects.get(vendor="Philips")
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    for url in [reverse("web:vendor_reviews"), reverse("web:vendor_review", args=[review.number]),
                reverse("web:vendor_review_print", args=[review.number]), reverse("web:vendor_reviews_print"),
                reverse("web:contract", args=[philips.pk])]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        assert review.number in r.content.decode(), url


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


def test_software_updates_written_under_the_policies(client, seeded):
    """Slice 38 review, round 2: the slice's writes as the runtime role, each landing in this facility only (stamped with the request's
    tenant and passing WITH CHECK, with their history; the other facility's rows untouched): Open software update through its view on
    a pump with none, Open updates to this release (the batch) for one department, Confirm version on the update completed with the
    version not confirmed, Entered in error on the patches recorded by hand, and the API's transition completing an open update work
    order with software_outcome (the device's record moves to the release installed)."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease, SoftwareUpdate
    from apps.workorders.models import WoStatus, WoType

    north = Tenant.objects.exclude(pk=seeded.pk).get()  # the seed's second facility, Kim's too

    def counts(tenant) -> dict:
        """This facility's rows (the history managers are not tenant-scoped: filtered here, as core.history._rows does)."""
        with tenant_context(tenant):
            return {"work orders": WorkOrder.objects.count(), "updates": SoftwareUpdate.objects.count(),
                    "records": DeviceSecurity.objects.count(), "work order history": WorkOrder.history.filter(tenant_id=tenant.pk).count(),
                    "update history": SoftwareUpdate.history.filter(tenant_id=tenant.pk).count(),
                    "record history": DeviceSecurity.history.filter(tenant_id=tenant.pk).count()}

    def pump_on(asset) -> int | None:
        return DeviceSecurity.objects.filter(asset=asset).values_list("release_id", flat=True).first()

    with tenant_context(seeded):
        today = timezone.localdate()
        older = SoftwareRelease.objects.get(device_model__model="Alaris 8015 PCU", version="5.2 (demo)")
        newer = SoftwareRelease.objects.get(device_model__model="Alaris 8015 PCU", version="5.6 (demo)")
        updated = set(SoftwareUpdate.objects.values_list("asset_id", flat=True))
        idle = list(Asset.objects.filter(device_model=newer.device_model, status="in_service", incident_hold=False,
                                         cyber__release=older).exclude(pk__in=updated).order_by("tag")[:2])
        unconfirmed = SoftwareUpdate.objects.select_related("asset").get(work_order__status=WoStatus.COMPLETED, outcome="")
        hand = SoftwareUpdate.objects.get(work_order__isnull=True)
        open_wo = SoftwareUpdate.objects.select_related("work_order").filter(work_order__status=WoStatus.OPEN).order_by("work_order__number")[0].work_order
    assert len(idle) == 2  # one for Open software update, the other's department for the batch
    before = {t.slug: counts(t) for t in (seeded, north)}
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    hx = {"HTTP_HX_REQUEST": "true"}
    due = (today + timedelta(days=30)).isoformat()

    r = client.post(f"/security/devices/{idle[0].tag}/updates/open/", {"target": "release", "target_release": str(newer.pk),
                                                                       "due_on": due, "priority": "normal", "assign_to": ""}, **hx)
    assert r.status_code == 200 and r.get("HX-Retarget") == "#drawer", r.content.decode()[:500]
    with tenant_context(seeded):
        opened = SoftwareUpdate.objects.select_related("work_order").get(asset=idle[0])
        assert opened.tenant_id == opened.work_order.tenant_id == seeded.pk and opened.work_order.type == WoType.SOFTWARE_UPDATE
        batch_before = SoftwareUpdate.objects.count()

    r = client.post(f"/security/releases/{newer.pk}/open-updates/", {"from_releases": [str(older.pk)], "department": str(idle[1].department_id),
                                                                     "due_on": due, "priority": "normal", "assign_to": ""}, **hx)
    assert r.status_code == 200 and r.get("HX-Retarget") == "#drawer", r.content.decode()[:500]
    with tenant_context(seeded):
        batch = SoftwareUpdate.objects.count() - batch_before
        assert batch >= 1 and SoftwareUpdate.objects.filter(asset=idle[1], target_release=newer, work_order__status=WoStatus.OPEN).exists()

    r = client.post(f"/security/updates/{unconfirmed.pk}/confirm/", {"software_outcome": "installed", "software_release": str(newer.pk),
                                                                      "software_reference": "", "software_basis": "manufacturer_release"}, **hx)
    assert r.status_code == 200 and r.get("HX-Retarget") == "#drawer", r.content.decode()[:500]
    r = client.post(f"/security/updates/{hand.pk}/remove/", {}, **hx)
    assert r.status_code == 200 and r.get("HX-Retarget") == "#drawer", r.content.decode()[:500]
    r = client.post(f"/api/v1/work-orders/{open_wo.pk}/transition/", {"status": "completed", "software_outcome": "installed",
                                                                       "software_release": str(newer.pk), "software_basis": "manufacturer_release"},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["software_update"]["state"] == "done", r.content.decode()[:500]

    with tenant_context(seeded):
        assert SoftwareUpdate.objects.get(pk=unconfirmed.pk).outcome == "installed" and pump_on(unconfirmed.asset_id) == newer.pk
        assert SoftwareUpdate.objects.get(pk=hand.pk).removed_on == today
        done = SoftwareUpdate.objects.select_related("work_order").get(work_order=open_wo)
        assert done.outcome == "installed" and done.work_order.status == WoStatus.COMPLETED and pump_on(done.asset_id) == newer.pk
    after = {t.slug: counts(t) for t in (seeded, north)}
    assert after[north.slug] == before[north.slug]  # the other facility: nothing written, nothing changed
    mine, was = after[seeded.slug], before[seeded.slug]
    assert mine["work orders"] - was["work orders"] == mine["updates"] - was["updates"] == 1 + batch
    assert all(mine[k] > was[k] for k in ("work order history", "update history", "record history")), (was, mine)


def test_security_decisions_written_under_the_policies(client, seeded):
    """Slice 39: the decision doors' writes as the runtime role, each landing in this facility only (stamped with the request's tenant and
    passing WITH CHECK, with their history; the other facility's rows untouched): Record decision on a scope Needs attention offers (its
    reasons read from the modal's hidden values, as a browser sends them), Review and renew, Entered in error, Record decision again on
    the scope, and End."""
    import re

    from django.utils import timezone

    from apps.cybersecurity.models import DecisionEnd, DeviceSecurityDecision

    north = Tenant.objects.exclude(pk=seeded.pk).get()  # the seed's second facility, Kim's too

    def counts(tenant) -> dict:
        with tenant_context(tenant):
            return {"decisions": DeviceSecurityDecision.objects.count(),
                    "history": DeviceSecurityDecision.history.filter(tenant_id=tenant.pk).count()}

    with tenant_context(seeded):
        today = timezone.localdate()
        url = _record_address(_record_links(today)[0])
    before = {t.slug: counts(t) for t in (seeded, north)}
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    hx = {"HTTP_HX_REQUEST": "true"}
    body = client.get(url, **hx).content.decode()
    reasons = re.findall(r'<input type="hidden" name="reasons" value="(\w+)">', body)
    assert reasons and "<form" in body, body[:500]
    fields = {"reasons": reasons, "response": "keep_without_added_controls", "accepted_by": "it_security", "decided_on": today.isoformat()}

    def saved(r):
        assert r.status_code == 200 and r.get("HX-Retarget") == "#drawer", r.content.decode()[:500]

    def stored(reference):
        with tenant_context(seeded):
            row = DeviceSecurityDecision.objects.get(exception_reference=reference)
            assert row.tenant_id == seeded.pk
            return row

    saved(client.post(url, {**fields, "exception_reference": "DEMO-EX-9001"}, **hx))
    first = stored("DEMO-EX-9001")
    saved(client.post(f"/security/decisions/new/?supersedes={first.number}", {**fields, "exception_reference": "DEMO-EX-9002"}, **hx))
    renewed = stored("DEMO-EX-9002")
    saved(client.post(f"/security/decisions/{renewed.number}/in-error/", {}, **hx))
    saved(client.post(url, {**fields, "exception_reference": "DEMO-EX-9003"}, **hx))
    third = stored("DEMO-EX-9003")
    saved(client.post(f"/security/decisions/{third.number}/end/", {}, **hx))
    with tenant_context(seeded):
        ends = {d.number: (d.end_reason, d.supersedes_id) for d in DeviceSecurityDecision.objects.filter(pk__in=[first.pk, renewed.pk, third.pk])}
    assert ends == {first.number: (DecisionEnd.SUPERSEDED, None), renewed.number: (DecisionEnd.ENTERED_IN_ERROR, first.pk),
                    third.number: (DecisionEnd.NO_LONGER_NEEDED, None)}
    after = {t.slug: counts(t) for t in (seeded, north)}
    assert after[north.slug] == before[north.slug]  # the other facility: nothing written, nothing changed
    mine, was = after[seeded.slug], before[seeded.slug]
    assert mine["decisions"] - was["decisions"] == 3 and mine["history"] - was["history"] >= 6, (was, mine)


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


def test_a_person_in_two_facilities_under_the_policies(client, tenant, mailoutbox):
    """Slice 22's signed-out paths and command: bootstrap_tenant for an address another facility's director uses (linking reads and
    locks User rows only), signing in as the person (the landing account), a reset request (one link for the person), and the
    switch into the other facility, which joins it."""
    kim = services.invite_user(tenant, email="kim@health.example", first_name="Kim", last_name="Alvarez",
                               role=Role.unscoped.get(tenant=tenant, slug="director"))  # unscoped: test setup
    kim.set_password(PASSWORD)
    kim.save()
    as_app_role()
    call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "kim@health.example", "--invite",
                 stdout=StringIO())
    lakeside = User.objects.get(person=User.objects.get(pk=kim.pk).person, tenant__slug="lakeside")
    r = client.post("/login/", {"username": "KIM@health.example", "password": PASSWORD})
    assert r.status_code == 302 and int(client.session["_auth_user_id"]) == kim.pk
    assert "Lakeside General (invited)" in client.get("/").content.decode()
    r = client.post("/account/facility/", {"account": lakeside.pk})
    assert r.status_code == 302 and int(client.session["_auth_user_id"]) == lakeside.pk and client.get("/").status_code == 200
    client.post("/logout/")
    mailoutbox.clear()
    assert client.post("/password-reset/", {"email": "kim@health.example"}).status_code == 302
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["kim@health.example"]
