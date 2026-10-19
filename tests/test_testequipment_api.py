"""
Slice 35, wave 2C: test equipment over the API (apps/api/views_testequipment.py, serializers_testequipment.py) and the work order's side
(apps/api/views.py's WorkOrderViewSet, serializers.WorkOrderSerializer.test_equipment).

GET /api/v1/test-equipment/ lists the items with their standing in rules' words judged on the facility's today, in a fixed number of
queries, filtered by ?standing (rules.FILTERS), ?status, ?kind, ?held_by, ?search; GET .../<id or tag>/ one item (by tag in any letter
case) with its calibrations and tolerance reviews (the window's work orders only with Work orders View). Equipment View; never a scoped
user (a custom scoped role with Full everywhere neither); every write a 405 with Edit, a 403 in words without. A work order reads the test
equipment it recorded ([{id, tag, words}] on the day of the work; null for a scoped user and without Equipment View; a page at once), the
transition to completed takes test_equipment (a list of ids as text: KEEP when left out, [] clears, a diff; type-checked, keyed
test_equipment; refused on other moves, and from a scoped user when not empty), POST work-orders/{id}/test-equipment/ corrects it
afterwards (Edit, Approve once closed, never scoped), and a work order with test equipment recorded is never deleted (a 400 in words).
Under the policies as the runtime role, by token.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import FINDING, POST_REPAIR_ALL_PASS
from rest_framework.authtoken.models import Token
from testequipment_fixtures import (
    bench_tech,
    certificate,
    in_date,
    item,
    out_of_tolerance,
    pm_with,
    te_world,  # noqa: F401 - a pytest fixture
)

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.api import views as api_views
from apps.api import views_testequipment as api
from apps.api.permissions import ModulePermission
from apps.tenants.context import tenant_context
from apps.testequipment import rules, services
from apps.testequipment.models import Basis, Kind, Program, Reason, Status, TestEquipmentUse
from apps.vendors.rules import fmt
from apps.workorders.models import PmResult, Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

API = "/api/v1/test-equipment/"
WOS = "/api/v1/work-orders/"
DAY = timedelta(days=1)
LIST_FIELDS = {"id", "tag", "kind", "kind_label", "manufacturer", "model", "serial", "program", "program_label", "interval_months", "status",
               "status_label", "status_reason", "status_reason_label", "status_since", "held_by", "held_by_name", "standing", "due_on",
               "last_calibrated_on", "url"}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Philips"
    user.save()
    return user


def custom_user(tenant, name, levels, scope=""):
    role = Role.objects.create(name=name.title(), slug=name, scope=scope)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)


def signed_in(client, user):
    client.force_login(user)
    return user


def ok(r):
    assert r.status_code == 200, r.content
    return r.json()


def post(client, url, body, **extra):
    return client.post(url, body, content_type="application/json", **extra)


def results(client, query=""):
    return ok(client.get(f"{API}{query}"))["results"]


def tags(client, query=""):
    return [row["tag"] for row in results(client, query)]


def wo_url(wo, tail=""):
    return f"{WOS}{wo.id}/{tail}"


def shown(wo) -> list:
    """What the work order's field reads: rules' words on the day of the work (services.judged_for), never composed here."""
    return [{"id": str(j.item.pk), "tag": j.item.tag, "words": j.words} for j in services.judged_for(wo)]


def tags_on(wo) -> list:
    return sorted(TestEquipmentUse.objects.filter(work_order=wo).values_list("test_equipment__tag", flat=True))


def open_pm(asset, *, tech=None):
    wo = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled work", assigned_to=tech or bench_tech())
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def open_vendor(asset, type_=WoType.REPAIR):
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Vendor service visit")
    assign(wo, vendor_name="Philips")
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def reopen(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS, note="Reopened")


def complete_pm(client, wo, **extra):
    return post(client, wo_url(wo, "transition/"), {"status": "completed", "resolution": "Done per the procedure", "pm_result": PmResult.PASS,
                                                    **extra})


CHECKED_BODY = {"status": "completed", "resolution": "Replaced the board", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS,
                "repair_finding": FINDING}


# --- the list -----------------------------------------------------------------------------------------------------------------------

def test_the_list_reads_each_item_with_its_standing_in_the_rules_words(client, make_user, te_world, today):  # noqa: F811
    signed_in(client, make_user("analyst"))  # Equipment View
    rows = {row["tag"]: row for row in results(client)}
    assert list(rows) == sorted(rows) and set(rows) == {"TE-FL", "TE-IN", "TE-NR", "TE-OT1", "TE-OT2", "TE-PD"}
    for row in rows.values():
        assert set(row) == LIST_FIELDS
    every = {i.tag: i for i in services.list_with_standing(type(te_world.in_date).objects.all(), today)}
    for tag, row in rows.items():  # the rules' words, never composed here
        assert row["standing"] == every[tag].standing.words, tag
    cal_due = today - 100 * DAY
    in_date_row = rows["TE-IN"]
    assert in_date_row["standing"].startswith("Calibration due ") and in_date_row["due_on"] == services.default_due(cal_due, 12).isoformat()
    assert in_date_row["last_calibrated_on"] == cal_due.isoformat()
    assert (in_date_row["held_by"], in_date_row["held_by_name"]) == (str(te_world.tech.pk), "Bench Technician")
    assert (in_date_row["kind"], in_date_row["kind_label"], in_date_row["program"], in_date_row["program_label"]) == (
        "electrical_safety", "Electrical safety analyzer", "calibration", "Calibration")
    assert in_date_row["url"] == "/test-equipment/TE-IN/?facility=riverside"
    assert rows["TE-PD"]["standing"] == f"Calibration was due {fmt(today - 35 * DAY)}" and rows["TE-PD"]["due_on"] == (today - 35 * DAY).isoformat()
    assert (rows["TE-NR"]["standing"], rows["TE-NR"]["due_on"], rows["TE-NR"]["last_calibrated_on"]) == ("No calibration on file", None, None)
    failed = rows["TE-FL"]
    assert failed["standing"] == f"Failed calibration {fmt(today - 10 * DAY)}" and failed["due_on"] is None
    assert (failed["status"], failed["status_label"], failed["status_reason"], failed["status_reason_label"], failed["status_since"]) == (
        "out_of_service", "Out of service", "failed_calibration", "Failed calibration", today.isoformat())
    assert (rows["TE-IN"]["status_reason"], rows["TE-IN"]["status_reason_label"]) == ("", "")
    body = client.get(API).content.decode()
    assert "in date" not in body.lower() and "uncalibrated" not in body.lower() and "instrument" not in body.lower()


def test_a_verification_item_reads_verification(client, make_user, ctx, today):
    signed_in(client, make_user("analyst"))
    spo2 = item("TE-SP", kind=Kind.SPO2, model="Index 2XLFE", program=Program.VERIFICATION)
    certificate(spo2, today - 30 * DAY)
    row = results(client)[0]
    assert row["standing"].startswith("Verification due ") and row["program_label"] == "Verification"


def test_the_list_filters(client, make_user, te_world, today):  # noqa: F811
    signed_in(client, make_user("technician"))
    soon = item("TE-SOON", kind=Kind.PATIENT_SIMULATOR, model="ProSim 8")
    certificate(soon, today - 300 * DAY, due_on=today + 20 * DAY)
    assert tags(client, "?standing=past_due") == ["TE-PD"]
    assert tags(client, "?standing=no_record") == ["TE-NR"]
    assert tags(client, "?standing=failed") == ["TE-FL"]
    assert tags(client, "?standing=due_soon") == ["TE-SOON"]
    assert tags(client, "?status=out_of_service") == ["TE-FL"]
    assert tags(client, "?kind=infusion") == ["TE-OT1", "TE-OT2"]
    assert tags(client, "?kind=patient_simulator&standing=due_soon") == ["TE-SOON"]
    assert tags(client, f"?held_by={te_world.tech.pk}") == ["TE-IN"]
    assert tags(client, "?search=sn-te-pd") == ["TE-PD"]  # a serial, any letter case
    assert tags(client, "?search=te-ot") == ["TE-OT1", "TE-OT2"]
    assert tags(client, "?ordering=-tag")[:2] == ["TE-SOON", "TE-PD"]
    for query, field in (("?standing=overdue", "standing"), ("?status=broken", "status"), ("?kind=wrench", "kind"),
                         ("?held_by=dana", "held_by"), ("?standing=past_due&status=x", "status")):
        r = client.get(f"{API}{query}")
        assert r.status_code == 400 and field in r.json(), query
    assert client.get(f"{API}?standing=in_date").json() == {"standing": [f"One of {', '.join(rules.FILTERS)}."]}


def test_the_list_reads_a_fixed_number_of_queries(client, make_user, te_world, today, pump):  # noqa: F811
    signed_in(client, make_user("director"))

    def queries(query="") -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(f"{API}{query}").status_code == 200
        return len(q)

    few, filtered = queries(), queries("?standing=past_due")
    tech = bench_tech("Second Technician")
    for n in range(6):
        extra = item(f"TE-X{n}", held_by=tech if n % 2 else None, kind=Kind.MULTIMETER, model=f"M-{n}")
        certificate(extra, today - (50 + n) * DAY)
        certificate(extra, today - (500 + n) * DAY, due_on=today - (400 + n) * DAY)
    pm_with(pump, te_world.past_due)
    assert len(results(client)) == 12
    assert queries() == few and queries("?standing=past_due") == filtered


def test_the_browsable_api_renders(client, make_user, te_world):  # noqa: F811
    signed_in(client, make_user("technician"))
    for url in (API, f"{API}TE-IN/"):
        assert client.get(url, HTTP_ACCEPT="text/html").status_code == 200, url


# --- one item -----------------------------------------------------------------------------------------------------------------------

def test_one_item_by_id_or_by_tag_in_any_letter_case(client, make_user, te_world, other_tenant):  # noqa: F811
    signed_in(client, make_user("analyst"))
    by_id = ok(client.get(f"{API}{te_world.in_date.pk}/"))
    assert by_id["tag"] == "TE-IN" and set(LIST_FIELDS) <= set(by_id) and {"calibrations", "reviews"} <= set(by_id)
    assert ok(client.get(f"{API}te-in/"))["id"] == str(te_world.in_date.pk)
    dotted = item("TE.05")
    assert ok(client.get(f"{API}te.05/"))["id"] == str(dotted.pk)  # a tag may hold a dot
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
    for value in ("TE-NOPE", theirs.pk, "TE-THEIRS", "00000000-0000-0000-0000-000000000000"):
        r = client.get(f"{API}{value}/")
        assert r.status_code == 404 and r.json() == {"detail": api.NOT_FOUND}, value


def test_one_item_reads_its_calibrations_and_reviews(client, make_user, te_world, today, vent):  # noqa: F811
    manager = signed_in(client, make_user("manager"))
    ot = te_world.unreviewed
    data = ok(client.get(f"{API}{ot.item.tag}/"))
    assert data["standing"] == rules.standings([ot.item], today)[ot.item.pk].words
    found, before = data["calibrations"]  # newest first
    assert (found["id"], before["id"]) == (str(ot.found.pk), str(ot.before.pk))
    assert (found["as_found"], found["as_found_label"], found["adjusted"], found["result"], found["result_label"]) == (
        "out_of_tolerance", "Out of tolerance as found", True, "passed", "Passed")
    assert (found["accreditation"], found["accreditation_label"]) == ("shown", "Carries an ISO/IEC 17025 accreditation mark")
    assert (found["needs_review"], before["needs_review"], found["entered_in_error"], found["entered_in_error_by"]) == (True, False, False, None)
    assert found["entered_on"] == today.isoformat() and found["provider"] == "Fluke Biomedical" and found["certificate"] == ot.found.certificate
    [review] = data["reviews"]
    assert (review["calibration"], review["state"], review["words"], review["decision"]) == (str(ot.found.pk), "not_reviewed", "Not reviewed", None)
    assert (review["window_start"], review["window_end"]) == ((today - 200 * DAY).isoformat(), (today - 10 * DAY).isoformat())
    assert review["work_count"] == 2 and [w["number"] for w in review["work_orders"]] == [w.number for w in ot.work]
    assert {w["asset_tag"] for w in review["work_orders"]} == {vent.tag} and not any(w["in_review"] for w in review["work_orders"])
    assert review["recheck"] == services.recheck_progress(ot.found).words
    # decided: the basis, who, the frozen count; then work added in the window since
    services.decide_review(ot.found, basis=Basis.FUNCTION_UNUSED, quality_reference="QR-26-0007", decided_on=today,
                           version=services.review_version(ot.work), by=manager)
    later = pm_with(vent, ot.item, completed_on=today - 30 * DAY)
    [review] = ok(client.get(f"{API}{ot.item.tag}/"))["reviews"]
    assert review["state"] == "reviewed" and review["words"] == services.review_summary(ot.found).words
    assert review["decision"] == {"basis": "function_unused", "basis_label": Basis.FUNCTION_UNUSED.label, "quality_reference": "QR-26-0007",
                                  "decided_on": today.isoformat(), "decided_by": "Manager User",
                                  "window_start": (today - 200 * DAY).isoformat(), "window_end": (today - 10 * DAY).isoformat(),
                                  "work_count": 2}
    assert (review["work_count"], review["added_since_review"], review["added_words"]) == (3, 1, "1 work order added since the review")
    assert [(w["number"], w["in_review"]) for w in review["work_orders"]] == [(ot.work[0].number, True), (ot.work[1].number, True),
                                                                              (later.number, False)]  # by the day of the work
    # a certificate entered in error stays, marked; its review stops counting
    services.mark_entered_in_error(ot.found, by=manager)
    data = ok(client.get(f"{API}{ot.item.tag}/"))
    assert data["reviews"] == [] and data["calibrations"][0]["entered_in_error"] is True
    assert (data["calibrations"][0]["entered_in_error_by"], data["calibrations"][0]["entered_in_error_on"]) == ("Manager User", today.isoformat())


def test_the_windows_work_orders_need_work_orders_view_too(client, tenant, te_world):  # noqa: F811
    signed_in(client, custom_user(tenant, "kit", {Module.EQUIPMENT: Level.VIEW}))
    [review] = ok(client.get(f"{API}{te_world.unreviewed.item.tag}/"))["reviews"]
    assert review["work_orders"] is None and review["work_count"] == 2  # the count, never which work orders
    assert "WO-" not in client.get(f"{API}{te_world.unreviewed.item.tag}/").content.decode()


# --- who reads it, and nothing is written ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("slug", ["director", "manager", "technician", "analyst"])
def test_equipment_view_reads(client, make_user, te_world, slug):  # noqa: F811
    signed_in(client, make_user(slug))
    assert client.get(API).status_code == 200 and client.get(f"{API}TE-IN/").status_code == 200


def test_scoped_users_and_roles_without_equipment_view_are_refused(client, make_user, tenant, te_world, vendor_user):  # noqa: F811
    unit_full = custom_user(tenant, "unitfull", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT)
    for user in (vendor_user, make_user("requester"), unit_full):
        signed_in(client, user)
        for url in (API, f"{API}TE-IN/", f"{API}{te_world.in_date.pk}/"):
            r = client.get(url)
            assert r.status_code == 403 and r.json()["detail"] == ModulePermission.SCOPED_REFUSAL, (user.username, url)
    signed_in(client, custom_user(tenant, "nothing", {Module.WORKORDERS: Level.FULL}))
    r = client.get(API)
    assert r.status_code == 403 and r.json()["detail"] == api.VIEW_REFUSAL


def test_every_write_is_refused(client, make_user, te_world):  # noqa: F811
    item_url = f"{API}{te_world.in_date.pk}/"
    writes = [("post", API), ("put", item_url), ("patch", item_url), ("delete", item_url)]
    signed_in(client, make_user("technician"))  # Equipment Edit: no write here
    for method, url in writes:
        r = getattr(client, method)(url, {"tag": "TE-NEW"}, content_type="application/json")
        assert r.status_code == 405 and r.json()["detail"] == api.READ_ONLY, (method, url)
    signed_in(client, make_user("analyst"))  # Equipment View
    for method, url in writes:
        r = getattr(client, method)(url, {"tag": "TE-NEW"}, content_type="application/json")
        assert r.status_code == 403 and r.json()["detail"] == api.WRITE_REFUSAL, (method, url)
    te_world.in_date.refresh_from_db()
    assert te_world.in_date.tag == "TE-IN"


def test_a_token_reads_only_its_own_facility(client, make_user, te_world, other_tenant):  # noqa: F811
    from apps.accounts.models import create_default_roles

    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        item("TE-IN")  # the same tag in another facility
        theirs = Role.objects.get(slug="director")
    them = User.objects.create_user(username="director@other.example", password="Test-Pass-2026-x", tenant=other_tenant, role=theirs)
    key = Token.objects.create(user=them).key
    data = client.get(API, HTTP_AUTHORIZATION=f"Token {key}").json()
    assert [r["tag"] for r in data["results"]] == ["TE-IN"] and data["results"][0]["id"] != str(te_world.in_date.pk)
    assert data["results"][0]["url"] == "/test-equipment/TE-IN/?facility=other"
    assert client.get(f"{API}{te_world.in_date.pk}/", HTTP_AUTHORIZATION=f"Token {key}").status_code == 404


# --- the work order's test_equipment --------------------------------------------------------------------------------------------

def test_a_work_order_reads_its_test_equipment_on_the_day_of_the_work(client, make_user, te_world, pump, today):  # noqa: F811
    signed_in(client, make_user("technician"))
    data = ok(client.get(wo_url(te_world.pm)))
    assert data["test_equipment"] == shown(te_world.pm) and len(data["test_equipment"]) == 1
    [row] = data["test_equipment"]
    assert (row["id"], row["tag"]) == (str(te_world.in_date.pk), "TE-IN")
    assert row["words"].startswith("TE-IN (Fluke Biomedical ESA612): calibration due ") and row["words"].endswith(" on the day of this work")
    later = ok(client.get(wo_url(te_world.unreviewed.work[0])))["test_equipment"]
    assert later == shown(te_world.unreviewed.work[0]) and "found out of tolerance on" in later[0]["words"] and "not reviewed" in later[0]["words"]
    assert ok(client.get(wo_url(te_world.vendor)))["test_equipment"] == []
    # reopened: judged on the day it is completed again
    change_status(te_world.pm, WoStatus.IN_PROGRESS, note="Reopened")
    assert ok(client.get(wo_url(te_world.pm)))["test_equipment"][0]["words"] == "TE-IN (Fluke Biomedical ESA612): judged on the day the work is completed"


def test_the_list_reads_the_same_as_each_work_order(client, make_user, te_world):  # noqa: F811
    signed_in(client, make_user("manager"))
    rows = ok(client.get(WOS))["results"]
    assert len(rows) == WorkOrder.objects.count()
    for row in rows:
        assert row["test_equipment"] == ok(client.get(f"{WOS}{row['id']}/"))["test_equipment"], row["number"]
    assert sum(len(row["test_equipment"]) for row in rows) == TestEquipmentUse.objects.count()


def test_the_work_order_list_reads_test_equipment_once_a_page(client, make_user, te_world, pump, vent, today):  # noqa: F811
    signed_in(client, make_user("manager"))

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(WOS).status_code == 200
        return len(q)

    one = queries()  # te_world: uses with certificates, one found out of tolerance and one reviewed
    extra = in_date("TE-IN2")
    pm_with(pump, extra, te_world.in_date)
    pm_with(vent, te_world.past_due, completed_on=today - 3 * DAY)
    out_of_tolerance(pump, "TE-OT3", reviewed=True, model="IDA-7")
    assert queries() == one


def test_scoped_users_and_roles_without_equipment_view_read_null(client, make_user, tenant, te_world, pump, vendor_user):  # noqa: F811
    signed_in(client, vendor_user)
    assert ok(client.get(wo_url(te_world.vendor)))["test_equipment"] is None
    assert all(row["test_equipment"] is None for row in ok(client.get(WOS))["results"])
    signed_in(client, custom_user(tenant, "wo-only", {Module.WORKORDERS: Level.EDIT}))
    assert ok(client.get(wo_url(te_world.pm)))["test_equipment"] is None
    rows = ok(client.get(WOS))["results"]
    assert rows and all(row["test_equipment"] is None for row in rows)
    assert "TE-IN" not in client.get(WOS).content.decode()


# --- completing with test equipment ---------------------------------------------------------------------------------------------

def test_completing_records_the_test_equipment_sent(client, make_user, te_world, pump):  # noqa: F811
    signed_in(client, make_user("technician"))
    wo = open_pm(pump)
    r = complete_pm(client, wo, test_equipment=[str(te_world.in_date.pk), str(te_world.past_due.pk)])
    assert r.status_code == 200, r.content
    wo.refresh_from_db()
    assert tags_on(wo) == ["TE-IN", "TE-PD"] and r.json()["test_equipment"] == shown(wo)
    assert "calibration was due" in r.json()["test_equipment"][1]["words"]  # recorded truthfully; its words flag it
    history = TestEquipmentUse.history.filter(work_order_id=wo.pk)
    assert sorted(history.values_list("history_change_reason", flat=True)) == ["Recorded with the completion"] * 2
    # reopened and completed again: left out keeps; a list is a diff; [] clears
    reopen(wo)
    assert complete_pm(client, wo).status_code == 200 and tags_on(wo) == ["TE-IN", "TE-PD"]
    reopen(wo)
    assert complete_pm(client, wo, test_equipment=[str(te_world.in_date.pk)]).status_code == 200 and tags_on(wo) == ["TE-IN"]
    reopen(wo)
    assert complete_pm(client, wo, test_equipment=[]).status_code == 200 and tags_on(wo) == []


@pytest.mark.parametrize("value", [None, "", "TE-IN", 7, {"id": "x"}, [7], [{"id": "x"}], [["x"]], [None]])
def test_test_equipment_is_a_list_of_ids_as_text(client, make_user, te_world, pump, value):  # noqa: F811
    signed_in(client, make_user("technician"))
    wo = open_pm(pump)
    r = complete_pm(client, wo, test_equipment=value)
    assert r.status_code == 400 and r.json() == {"test_equipment": [api_views.TEST_EQUIPMENT_IDS + api_views.TEST_EQUIPMENT_KEPT]}
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and tags_on(wo) == []


def test_completing_refuses_in_the_services_words(client, make_user, te_world, pump, other_tenant, today):  # noqa: F811
    signed_in(client, make_user("technician"))
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
    damaged = item("TE-DMG")
    services.set_status(damaged, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=None, today=today - 3 * DAY)
    wo = open_pm(pump)
    for ids, words in (([str(theirs.pk)], services.CHOOSE), (["not-an-id"], services.CHOOSE),
                       ([str(damaged.pk)], services.NOT_USABLE.format(tag="TE-DMG", status="out of service (damaged)"))):
        r = complete_pm(client, wo, test_equipment=ids)
        assert r.status_code == 400 and r.json() == {"test_equipment": [words]}, ids
    # with the rest of the completion's refusals at once
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "pm_result": "", "resolution": "", "test_equipment": ["not-an-id"]})
    assert r.status_code == 400 and {"test_equipment", "pm_result"} <= set(r.json())
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and tags_on(wo) == []


def test_vendor_service_records_test_equipment_only_on_a_repair_we_checked(client, make_user, te_world, pump, vent):  # noqa: F811
    signed_in(client, make_user("manager"))
    ids = [str(te_world.in_date.pk)]
    vendor_pm = open_vendor(pump, WoType.PM)
    r = complete_pm(client, vendor_pm, test_equipment=ids)
    assert r.status_code == 400 and r.json()["test_equipment"] == [services.VENDOR_WORK.format(number=vendor_pm.number)]
    repair = open_vendor(vent)
    r = post(client, wo_url(repair, "transition/"), {**CHECKED_BODY, "repair_check": "vendor_report", "checks_reference": "FSO-2201",
                                                     "results": None, "test_equipment": ids})
    assert r.status_code == 400 and r.json()["test_equipment"] == [services.VENDOR_CHECKED]
    r = post(client, wo_url(repair, "transition/"), {**CHECKED_BODY, "test_equipment": ids})
    assert r.status_code == 200, r.content
    assert tags_on(repair) == ["TE-IN"] and r.json()["test_equipment"] == shown(repair)


def test_a_scoped_user_completes_with_none(client, te_world, vent, vendor_user):  # noqa: F811
    signed_in(client, vendor_user)
    repair = open_vendor(vent)
    r = post(client, wo_url(repair, "transition/"), {**CHECKED_BODY, "test_equipment": [str(te_world.in_date.pk)]})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.SCOPED]}
    r = post(client, wo_url(repair, "transition/"), {**CHECKED_BODY, "test_equipment": []})
    assert r.status_code == 200, r.content
    assert r.json()["test_equipment"] is None and tags_on(repair) == []


@pytest.mark.parametrize("value", [[], ["x"], None])
def test_other_moves_refuse_test_equipment(client, make_user, te_world, pump, value):  # noqa: F811
    signed_in(client, make_user("manager"))
    wo = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled work", assigned_to=bench_tech())
    r = post(client, wo_url(wo, "transition/"), {"status": "in_progress", "test_equipment": value})
    assert r.status_code == 400 and r.json() == {"test_equipment": [api_views.TEST_EQUIPMENT_RECORDED]}
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN


# --- after completion: POST work-orders/{id}/test-equipment/ -----------------------------------------------------------------------

def test_the_action_corrects_a_completed_work_order(client, make_user, te_world, today):  # noqa: F811
    signed_in(client, make_user("technician"))
    wo, url = te_world.pm, wo_url(te_world.pm, "test-equipment/")
    r = post(client, url, {"test_equipment": [str(te_world.in_date.pk), str(te_world.no_record.pk)]})
    assert r.status_code == 200, r.content
    assert tags_on(wo) == ["TE-IN", "TE-NR"] and r.json()["test_equipment"] == shown(wo)
    assert "no calibration on file on the day of this work" in r.json()["test_equipment"][1]["words"]
    assert TestEquipmentUse.history.filter(work_order_id=wo.pk, history_change_reason="Recorded after completion").count() == 1
    assert ok(post(client, url, {"test_equipment": []}))["test_equipment"] == [] and tags_on(wo) == []
    assert TestEquipmentUse.history.filter(work_order_id=wo.pk, history_change_reason="Taken off after completion").count() == 2


def test_the_action_refuses_in_words(client, make_user, te_world, pump, today):  # noqa: F811
    signed_in(client, make_user("technician"))
    url = wo_url(te_world.pm, "test-equipment/")
    for body, field, words in (({}, "test_equipment", api_views.TEST_EQUIPMENT_IDS),
                               ({"test_equipment": "TE-IN"}, "test_equipment", api_views.TEST_EQUIPMENT_IDS),
                               ({"test_equipment": None}, "test_equipment", api_views.TEST_EQUIPMENT_IDS),
                               ({"test_equipment": [], "note": "x"}, "note", "Unknown field.")):
        r = post(client, url, body)
        assert r.status_code == 400 and r.json() == {field: [words]}, body
    r = post(client, url, ["x"])
    assert r.status_code == 400 and r.json() == {"detail": "Send the fields as a JSON object."}
    # still open: recorded when completing it
    wo = open_pm(pump)
    r = post(client, wo_url(wo, "test-equipment/"), {"test_equipment": [str(te_world.in_date.pk)]})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.NOT_DONE.format(number=wo.number)]}
    # imported work records none
    r = post(client, wo_url(te_world.imported, "test-equipment/"), {"test_equipment": [str(te_world.in_date.pk)]})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.IMPORTED.format(number=te_world.imported.number)]}
    # the kept-use rule: a calibration dated on or after the day of the work is on file
    kept = te_world.unreviewed.work[1]
    r = post(client, wo_url(kept, "test-equipment/"), {"test_equipment": []})
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.KEPT.format(tag="TE-OT1", number=kept.number,
                                                                                         day=fmt(kept.completed_on))]}
    assert tags_on(kept) == ["TE-OT1"] and tags_on(te_world.pm) == ["TE-IN"]


def test_the_action_needs_approve_once_closed_and_is_never_scoped(client, make_user, te_world, vendor_user):  # noqa: F811
    wo, url = te_world.pm, wo_url(te_world.pm, "test-equipment/")
    change_status(wo, WoStatus.CLOSED)
    body = {"test_equipment": [str(te_world.in_date.pk), str(te_world.past_due.pk)]}
    signed_in(client, make_user("technician"))
    r = post(client, url, body)
    assert r.status_code == 403 and r.json()["detail"] == f"{wo.number} is closed: recording the test equipment used now needs Work orders Approve."
    signed_in(client, make_user("analyst"))  # Work orders View
    assert post(client, url, body).status_code == 403
    assert tags_on(wo) == ["TE-IN"]
    signed_in(client, make_user("manager"))
    assert post(client, url, body).status_code == 200 and tags_on(wo) == ["TE-IN", "TE-PD"]
    signed_in(client, vendor_user)
    vendor_url = wo_url(te_world.vendor, "test-equipment/")
    r = post(client, vendor_url, {"test_equipment": []})
    assert r.status_code == 403 and r.json()["detail"] == ModulePermission.SCOPED_REFUSAL
    assert "test_equipment" not in api_views.WorkOrderViewSet.scoped_actions


# --- edits, adding, and deleting ------------------------------------------------------------------------------------------------

def test_an_edit_never_writes_the_test_equipment(client, make_user, te_world, pump):  # noqa: F811
    signed_in(client, make_user("manager"))
    wo = te_world.pm
    got = ok(client.get(wo_url(wo)))["test_equipment"]
    for sent in (got, [row["id"] for row in got]):  # what a GET returned, or its ids, sends back fine
        assert client.patch(wo_url(wo), {"test_equipment": sent}, content_type="application/json").status_code == 200
    for sent in ([], [str(te_world.past_due.pk)], None, "TE-IN", [{"tag": "TE-IN"}]):
        r = client.patch(wo_url(wo), {"test_equipment": sent}, content_type="application/json")
        assert r.status_code == 400 and r.json() == {"test_equipment": [api_views.TEST_EQUIPMENT_RECORDED]}, sent
    assert tags_on(wo) == ["TE-IN"]
    body = {"asset": str(pump.pk), "type": "repair", "problem": "Alarm", "due_on": te_world.today.isoformat(),
            "test_equipment": [str(te_world.in_date.pk)]}
    before = WorkOrder.objects.count()
    r = post(client, WOS, body)
    assert r.status_code == 400 and r.json() == {"test_equipment": [api_views.TEST_EQUIPMENT_RECORDED]}
    assert WorkOrder.objects.count() == before
    r = post(client, WOS, {**body, "test_equipment": []})
    assert r.status_code == 201 and r.json()["test_equipment"] == []


def test_an_edit_by_a_reader_without_equipment_view_sends_back_null(client, tenant, te_world):  # noqa: F811
    signed_in(client, custom_user(tenant, "wo-edit", {Module.WORKORDERS: Level.EDIT}))
    wo = create_work_order(asset=te_world.pm.asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    assert client.patch(wo_url(wo), {"test_equipment": None, "priority": "high"}, content_type="application/json").status_code == 200
    r = client.patch(wo_url(wo), {"test_equipment": []}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"test_equipment": [api_views.TEST_EQUIPMENT_RECORDED]}


def test_a_work_order_with_test_equipment_recorded_is_never_deleted(client, make_user, te_world, pump):  # noqa: F811
    signed_in(client, make_user("director"))
    r = client.delete(wo_url(te_world.pm))
    assert r.status_code == 400 and r.json() == {"detail": services.delete_refusal(te_world.pm)}
    assert r.json()["detail"] == f"{te_world.pm.number} has test equipment recorded, so it is not deleted: it is the record of the work done with it."
    assert WorkOrder.objects.filter(pk=te_world.pm.pk).exists() and tags_on(te_world.pm) == ["TE-IN"]
    plain = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    assert client.delete(wo_url(plain)).status_code == 204
    # an incident's investigation with test equipment recorded: the incident's words first
    incident = make_incident(pump, work_order=te_world.pm, hold=False)
    r = client.delete(wo_url(te_world.pm))
    assert r.status_code == 400 and r.json() == {"detail": f"{te_world.pm.number} is the investigation of incident {incident.number}, so it "
                                                           "cannot be deleted: the incident's file keeps it."}


# --- under the policies ---------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_api_under_the_policies_by_token(client, make_user, te_world, pump, other_tenant, today):  # noqa: F811
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
    key = Token.objects.create(user=make_user("manager")).key
    auth = {"HTTP_AUTHORIZATION": f"Token {key}"}
    director = Token.objects.create(user=make_user("director")).key
    wo = open_pm(pump)  # made first: a token's request leaves the session's facility unset in the database afterwards
    as_app_role()
    rows = client.get(API, **auth).json()["results"]
    assert [r["tag"] for r in rows] == ["TE-FL", "TE-IN", "TE-NR", "TE-OT1", "TE-OT2", "TE-PD"]
    assert [r["tag"] for r in client.get(f"{API}?standing=past_due", **auth).json()["results"]] == ["TE-PD"]
    data = client.get(f"{API}te-ot1/", **auth).json()
    assert data["reviews"][0]["work_count"] == 2 and len(data["calibrations"]) == 2
    assert client.get(f"{API}{theirs.pk}/", **auth).status_code == 404
    assert client.get(wo_url(te_world.pm), **auth).json()["test_equipment"][0]["tag"] == "TE-IN"
    r = post(client, wo_url(wo, "transition/"), {"status": "completed", "resolution": "Done", "pm_result": "pass",
                                                 "test_equipment": [str(te_world.in_date.pk)]}, **auth)
    assert r.status_code == 200, r.content
    r = post(client, wo_url(wo, "test-equipment/"), {"test_equipment": [str(te_world.in_date.pk), str(theirs.pk)]}, **auth)
    assert r.status_code == 400 and r.json() == {"test_equipment": [services.CHOOSE]}
    assert post(client, wo_url(wo, "test-equipment/"), {"test_equipment": []}, **auth).status_code == 200
    r = client.delete(wo_url(te_world.pm), HTTP_AUTHORIZATION=f"Token {director}")
    assert r.status_code == 400 and "has test equipment recorded" in r.json()["detail"]
    assert client.get(WOS, **auth).status_code == 200
    with tenant_context(te_world.pm.tenant):
        assert WorkOrder.objects.filter(pk=te_world.pm.pk).exists() and tags_on(wo) == []

