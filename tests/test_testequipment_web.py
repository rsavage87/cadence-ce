"""
Slice 35, wave 2 A: the Equipment screen's Test equipment tab and an item's drawer (apps/web/views_testequipment.py,
forms_testequipment.py, web/test_equipment.html, web/_te_*.html, web/_equipment_tabs.html).

Who: Equipment View sees the list, its CSV, an item and its calibrations, and History; the work orders done with it (their numbers, days,
devices: "Work done with it", a review box's list, both CSVs, the uses in History) need Work orders View too. Equipment Edit adds,
changes, moves the everyday status, and records a calibration; Decide (Work orders Approve with Equipment View) marks a calibration
entered in error and records the tolerance review; Equipment Approve retires and reinstates. Scoped users are refused everything.
Each action at its level, refusals with what was typed kept, full page vs partial, the CSVs (formula text kept as text), junk input
never a 500, another facility's tag a 404, mobile-safe markup, and on PostgreSQL the screens under the policies. The rules and services
themselves are tests/test_testequipment.py.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
import testequipment_fixtures
from csvutil import csv_rows
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.html import escape
from pg_helpers import as_app_role, needs_postgres
from testequipment_fixtures import bench_tech, certificate, item, pm_with

from apps.accounts.models import Level, Module, Role, User
from apps.tenants.context import tenant_context
from apps.testequipment import services as tes
from apps.testequipment.models import AsFound, Basis, Calibration, Kind, Reason, Result, Status, TestEquipment, ToleranceReview
from apps.testequipment.services import default_due, due_cap
from apps.vendors.rules import fmt

te_world = testequipment_fixtures.te_world  # the shared pytest fixture (its docstring says what it holds)
HX = {"HTTP_HX_REQUEST": "true"}
DAY = timedelta(days=1)
LIST = "/test-equipment/"


def part(target):
    return {**HX, "HTTP_HX_TARGET": target}


def html(r) -> str:
    return r.content.decode()


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    return triggers(r).get("toast", {}).get("value", "")


def url(te, *rest) -> str:
    return f"{LIST}{te.tag}/" + "".join(f"{x}/" for x in rest)


def errors(body) -> list[str]:
    return re.findall(r'<span class="err"[^>]*>([^<]*)</span>', body)


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def _role(tenant, slug, levels, scope=""):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return role


def custom_user(client, tenant, slug, levels, **extra):
    role = _role(tenant, slug, levels, extra.pop("scope", ""))
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role, **extra)
    client.force_login(user)
    return user


# --- the tab and the list --------------------------------------------------------------------------------------------------------

def test_the_list_shows_each_item_with_its_standing_judged_today(client, signed_in, te_world):
    signed_in("analyst")  # Equipment View, read only
    r = client.get(LIST)
    body = html(r)
    assert r.status_code == 200 and r.context["nav_active"] == "equipment" and r.context["equipment_tab"] == "test_equipment"
    assert 'href="/test-equipment/" aria-current="page">Test equipment</a>' in body and ">Devices</a>" in body
    w = te_world
    for te in (w.in_date, w.past_due, w.no_record, w.failed, w.unreviewed.item, w.reviewed.item):
        assert f'href="/test-equipment/{te.tag}/"' in body and te.serial in body
    assert "Electrical safety analyzer" in body and "Fluke Biomedical" in body and "ESA612" in body
    standing = tes.list_with_standing(TestEquipment.objects.filter(pk=w.in_date.pk), te_world.today)[0].standing
    assert f'<span class="chip ok">{standing.words}</span>' in body and standing.words.startswith("Calibration due ")
    assert '<span class="chip crit">Calibration was due ' in body and '<span class="chip warn">No calibration on file</span>' in body
    assert f'<span class="chip crit">Failed calibration {fmt(te_world.today - 10 * DAY)}</span>' in body
    assert "Out of service (failed calibration)<small>since " in body and "Bench Technician" in body  # who holds TE-IN
    assert "Add test equipment" not in body  # Equipment Edit adds
    assert 'hx-trigger="te-changed from:body"' in body and 'hx-disinherit="hx-swap hx-target"' in body
    assert "6 test equipment items not retired" in body and '?standing=past_due">1 past due</a>' in body


def test_the_filters_and_the_search(client, signed_in, te_world):
    signed_in("technician")
    w = te_world

    def tags(query):
        return [row["i"].tag for row in client.get(LIST + query).context["rows"]]

    assert tags("?standing=past_due") == ["TE-PD"]
    assert tags("?standing=no_record") == ["TE-NR"]
    assert tags("?standing=failed") == ["TE-FL"]
    assert tags("?standing=due_soon") == []
    assert tags("?status=out_of_service") == ["TE-FL"] and tags("?kind=infusion") == ["TE-OT1", "TE-OT2"]
    assert tags(f"?held_by={w.tech.pk}") == ["TE-IN"] and "TE-IN" not in tags("?held_by=none")
    assert tags("?q=te-pd") == ["TE-PD"] and tags("?q=sn-te-nr") == ["TE-NR"]  # a tag or a serial, any letter case
    tes.set_status(w.no_record, to=Status.MISSING, by=None)
    tes.set_status(w.no_record, to=Status.RETIRED, reason=Reason.LOST, by=None)
    assert "TE-NR" not in tags("") and tags("?status=retired") == ["TE-NR"] and "TE-NR" in tags("?status=all")
    # junk filters are dropped, never a 500
    r = client.get(LIST + "?standing=zzz&status=nope&kind=x&held_by=not-an-id&page=abc")
    assert r.status_code == 200 and len(r.context["rows"]) == 5
    assert client.get(LIST + "?q=TE%00").status_code < 500 and client.get("/test-equipment/export.csv?q=TE%00").status_code < 500
    body = html(client.get(LIST + "?standing=past_due&kind=infusion"))
    assert "No test equipment matches these filters." in body


def test_a_due_soon_item_is_amber(client, signed_in, ctx, today):
    te = item("TE-SOON")
    certificate(te, today - 340 * DAY)  # due in about 25 days
    signed_in("technician")
    body = html(client.get(LIST + "?standing=due_soon"))
    assert [r["i"].tag for r in client.get(LIST + "?standing=due_soon").context["rows"]] == ["TE-SOON"]
    assert '<span class="chip warn">Calibration due ' in body


def test_the_list_and_its_csv_read_in_a_fixed_number_of_queries(client, signed_in, ctx, today):
    signed_in("analyst")
    tech = bench_tech()

    def counted(path):
        with CaptureQueriesContext(connection) as q:
            r = client.get(path)
            if path.endswith(".csv"):
                csv_rows(r)
        return len(q)

    for i in range(2):
        certificate(item(f"TE-A{i}", held_by=tech), today - 10 * DAY)
    small = counted(LIST), counted("/test-equipment/export.csv")
    for i in range(7):
        certificate(item(f"TE-B{i}", held_by=tech), today - 20 * DAY)
        item(f"TE-C{i}")
    assert (counted(LIST), counted("/test-equipment/export.csv")) == small


def test_the_table_refetches_itself_with_the_summary(client, signed_in, te_world):
    signed_in("technician")
    r = client.get(LIST + "?standing=past_due", **part("te-table"))
    body = html(r)
    assert r.status_code == 200 and body.lstrip().startswith("<div id=\"te-table\"") and "<html" not in body
    assert 'id="te-summary" hx-swap-oob="true"' in body and "TE-PD" in body and "TE-IN" not in body


def test_an_empty_facility_says_what_to_do(client, signed_in, ctx):
    signed_in("technician")
    body = html(client.get(LIST))
    assert "No test equipment on record." in body and "Add test equipment" in body


def test_the_list_csv(client, signed_in, te_world):
    signed_in("analyst")
    w = te_world
    r = client.get("/test-equipment/export.csv?standing=past_due")
    rows = csv_rows(r)
    assert r["Content-Disposition"].startswith('attachment; filename="cadence-test-equipment-')
    assert rows[0] == ["Tag", "Kind", "Manufacturer", "Model", "Serial", "Certificates", "Interval (months)", "Status", "Status since", "Held by",
                       "Standing today", "Due"]
    assert len(rows) == 2 and rows[1][0] == "TE-PD" and rows[1][10].startswith("Calibration was due ")
    assert rows[1][11] == (w.today - 35 * DAY).isoformat()
    every = csv_rows(client.get("/test-equipment/export.csv"))
    assert len(every) == 7 and ["TE-IN", "Bench Technician"] == [every[2][0], every[2][9]]


def test_csv_text_that_looks_like_a_formula_stays_text(client, signed_in, ctx):
    item("TE-F1", manufacturer="=HYPERLINK(\"http://x\")", model="+SUM(A1)")
    signed_in("analyst")
    row = csv_rows(client.get("/test-equipment/export.csv"))[1]
    assert row[2] == "'=HYPERLINK(\"http://x\")" and row[3] == "'+SUM(A1)"


# --- Add test equipment ---------------------------------------------------------------------------------------------------------

def new(**over):
    data = {"tag": "TE-NEW", "kind": Kind.ELECTRICAL_SAFETY, "manufacturer": "Fluke Biomedical", "model": "ESA615", "serial": "4455",
            "program": "calibration", "interval_months": "12", "held_by": "", "result": "passed"}
    data.update(over)
    return data


def test_add_test_equipment_at_edit(client, signed_in, ctx, today):
    signed_in("analyst")
    assert client.get("/test-equipment/new/", **HX).status_code == 403
    assert client.post("/test-equipment/new/", new(), **HX).status_code == 403
    signed_in("technician")
    body = html(client.get("/test-equipment/new/", **HX))
    assert "Add test equipment" in body and "Its latest certificate (optional)" in body
    assert "Certificate carries an ISO/IEC 17025 accreditation mark" in body and 'value="12"' in body
    r = client.post("/test-equipment/new/", new(), **HX)
    te = TestEquipment.objects.get(tag="TE-NEW")
    assert r["HX-Retarget"] == "#drawer" and toast_of(r) == "TE-NEW added: No calibration on file"
    assert "te-changed" in triggers(r) and "modal-close" in json.loads(r["HX-Trigger-After-Settle"])
    assert te.status == Status.IN_USE and not te.calibrations.exists() and "TE-NEW · Electrical safety analyzer" in html(r)


def test_add_with_its_latest_certificate(client, signed_in, ctx, today):
    tech = bench_tech()
    signed_in("technician")
    day = today - 30 * DAY
    r = client.post("/test-equipment/new/", new(held_by=str(tech.pk), calibrated_on=day.isoformat(), provider="Fluke Biomedical",
                                                certificate="CAL-889", accreditation="shown", as_found="in_tolerance"), **HX)
    te = TestEquipment.objects.get(tag="TE-NEW")
    cal = te.calibrations.get()
    assert cal.due_on == default_due(day, 12) and cal.certificate == "CAL-889" and te.held_by == tech
    assert toast_of(r) == f"TE-NEW added: Calibration due {fmt(cal.due_on)}"


def test_add_refusals_keep_what_was_typed(client, signed_in, ctx, vent, today):
    signed_in("technician")
    r = client.post("/test-equipment/new/", new(tag=vent.tag.lower(), manufacturer="  ", certificate="CAL 1", provider="Lab One"), **HX)
    body = html(r)
    assert r.status_code == 200 and not TestEquipment.objects.exists()
    assert "Enter the manufacturer." in body and "Enter the calibration day on the certificate." in body
    assert "Enter the certificate&#x27;s number as it is printed, without spaces." in body
    assert 'value="ce-10001"' in body and 'value="ESA615"' in body and 'value="Lab One"' in body and 'value="CAL 1"' in body
    assert re.search(r'<input[^>]*name="manufacturer"[^>]*autofocus', body) and body.count("autofocus") == 1  # the first one wrong
    # the device's tag, once the rest is right
    r = client.post("/test-equipment/new/", new(tag=vent.tag.lower()), **HX)
    assert f"{vent.tag.lower()} is a device&#x27;s tag." in html(r)
    item("TE-01")
    assert "te-01 is already another test equipment item&#x27;s tag." in html(client.post("/test-equipment/new/", new(tag="te-01"), **HX))


def test_add_takes_junk_without_a_500(client, signed_in, ctx):
    signed_in("technician")
    junk = new(tag="a/b", kind="zzz", program="x", interval_months="abc", held_by="not-a-uuid", calibrated_on="2026-13-45", result="maybe",
               accreditation="x", as_found="y", adjusted="on", due_on="soon")
    r = client.post("/test-equipment/new/", junk, **HX)
    body = html(r)
    assert r.status_code == 200 and not TestEquipment.objects.exists()
    for words in ("Tags cannot contain spaces or slashes.", "Choose the kind of test equipment.", "Enter the interval in months, from 1 to 60.",
                  "Choose a technician from this facility.", "Choose what its certificates are: calibration or verification."):
        assert words.replace("'", "&#x27;") in body, words
    nul = client.post("/test-equipment/new/", new(tag="TE\x00X", manufacturer="A\x00"), **HX)
    assert nul.status_code == 200 and not TestEquipment.objects.exists()


# --- the drawer -----------------------------------------------------------------------------------------------------------------

def test_the_drawer_partial_and_the_full_page(client, signed_in, te_world):
    signed_in("technician")
    te = te_world.in_date
    r = client.get(url(te), **HX)
    body = html(r)
    assert r.status_code == 200 and "<html" not in body and body.lstrip().startswith("<div class=\"dr-h\">")
    assert "TE-IN · Electrical safety analyzer" in body and "Fluke Biomedical ESA612 · serial SN-TE-IN" in body
    page = client.get("/test-equipment/te-in/")  # any letter case
    assert page.status_code == 200 and "<html" in html(page) and 'class="drawer open"' in html(page)
    assert "TE-IN · Electrical safety analyzer" in html(page) and 'id="te-table"' in html(page)


def test_another_facility_tag_and_unknown_tags_are_404(client, signed_in, te_world, other_tenant, make_user):
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
        theirs_cal = certificate(theirs, te_world.today - 5 * DAY)
    signed_in("director")
    for path in (url(theirs), url(theirs, "edit"), url(theirs, "calibrations/new"), f"{LIST}TE-THEIRS/uses.csv", f"{LIST}NOPE/"):
        assert client.get(path, **HX).status_code == 404, path
    assert client.get("/test-equipment/TE%00X/", **HX).status_code in (400, 404)  # a NUL never reaches the database
    assert client.post(url(theirs, "status"), {"to": "missing"}, **HX).status_code == 404
    mine = te_world.in_date
    for path in (f"{url(mine)}calibrations/{theirs_cal.pk}/error/", f"{url(mine)}calibrations/{theirs_cal.pk}/review/",
                 f"{url(mine)}calibrations/{theirs_cal.pk}/review.csv"):
        assert client.get(path, **HX).status_code == 404, path
    other_cal = te_world.past_due.calibrations.get()
    assert client.get(f"{url(mine)}calibrations/{other_cal.pk}/error/", **HX).status_code == 404  # another item's calibration
    assert client.get(f"{url(mine)}calibrations/not-a-uuid/error/", **HX).status_code == 404


def test_the_drawer_offers_each_action_at_its_level(client, signed_in, te_world):
    te = te_world.unreviewed.item

    def body_as(role):
        signed_in(role)
        return html(client.get(url(te), **HX))

    analyst = body_as("analyst")
    assert "Record calibration" not in analyst and ">Change</button>" not in analyst and "Out for calibration" not in analyst
    assert "Entered in error</button>" not in analyst and ">Decide</button>" not in analyst
    assert "Not reviewed" in analyst and te_world.unreviewed.work[0].number in analyst  # Work orders View: the list
    tech = body_as("technician")
    assert "Record calibration" in tech and ">Change</button>" in tech and ">Out for calibration</button>" in tech
    assert ">Missing</button>" in tech and ">Returned to its owner</button>" in tech and "Retire: worn out" not in tech
    assert "Entered in error</button>" not in tech and ">Decide</button>" not in tech
    manager = body_as("manager")
    assert "Entered in error</button>" in manager and ">Decide</button>" in manager and "Retire: worn out" not in manager
    director = body_as("director")
    assert "Retire: worn out or obsolete</button>" in director and 'hx-confirm="Retire TE-OT1 (worn out or obsolete)?"' in director


def test_the_calibrations_show_what_the_certificate_shows(client, signed_in, te_world):
    signed_in("technician")
    w = te_world.unreviewed
    body = html(client.get(url(w.item), **HX))
    assert "Calibrations <span>2 on record</span>" in body
    assert "Out of tolerance as found" in body and "In tolerance as found" in body and "Adjusted</dt><dd>Yes" in body
    assert w.found.certificate in body and w.before.certificate in body and "Certificate carries an ISO/IEC 17025 accreditation mark" in body
    assert f"due {fmt(w.found.due_on)}" in body and "Recorded</dt><dd>" in body


# --- status -----------------------------------------------------------------------------------------------------------------------

def test_status_moves_by_level(client, signed_in, te_world):
    te = te_world.in_date
    signed_in("analyst")
    assert client.post(url(te, "status"), {"to": "out_for_calibration"}, **HX).status_code == 403
    signed_in("technician")
    assert client.get(url(te, "status"), **HX).status_code == 405
    r = client.post(url(te, "status"), {"to": "out_for_calibration"}, **HX)
    te.refresh_from_db()
    assert te.status == Status.OUT_FOR_CALIBRATION and toast_of(r) == "TE-IN: out for calibration" and "te-changed" in triggers(r)
    assert "Out for calibration since " in html(r) and ">Back from calibration</button>" in html(r)
    r = client.post(url(te, "status"), {"to": "in_use"}, **HX)
    r = client.post(url(te, "status"), {"to": "retired", "reason": "worn_out"}, **HX)
    te.refresh_from_db()
    assert te.status == Status.IN_USE and toast_of(r) == tes.RETIRE_PERMISSION
    assert f'<div class="note warn" role="alert">{tes.RETIRE_PERMISSION}</div>' in html(r)
    signed_in("director")
    r = client.post(url(te, "status"), {"to": "retired", "reason": "worn_out"}, **HX)
    te.refresh_from_db()
    assert te.status == Status.RETIRED and toast_of(r) == "TE-IN: retired (worn out or obsolete)" and ">Reinstate</button>" in html(r)


def test_out_of_service_goes_back_only_by_a_passed_calibration(client, signed_in, te_world):
    te = te_world.failed
    signed_in("technician")
    body = html(client.get(url(te), **HX))
    assert tes.back_in_use_words(te).replace("'", "&#x27;") in body
    r = client.post(url(te, "status"), {"to": "in_use"}, **HX)
    assert toast_of(r) == tes.back_in_use_words(te)
    for junk in ({"to": "zzz"}, {"to": "missing", "reason": "zzz"}, {"to": "\x00"}, {}):
        assert client.post(url(te, "status"), junk, **HX).status_code == 200


# --- Change -----------------------------------------------------------------------------------------------------------------------

def test_change_details_and_the_locks(client, signed_in, te_world):
    w = te_world
    signed_in("technician")
    fresh = w.no_record
    body = html(client.get(url(fresh, "edit"), **HX))
    assert 'name="tag"' in body and 'name="interval_months"' in body
    data = {"tag": "TE-NR2", "kind": "multimeter", "manufacturer": "Fluke", "model": "87V", "serial": "SN-TE-NR", "program": "calibration",
            "interval_months": "24", "held_by": str(w.tech.pk)}
    r = client.post(url(fresh, "edit"), data, **HX)
    fresh.refresh_from_db()
    assert fresh.tag == "TE-NR2" and fresh.interval_months == 24 and fresh.held_by == w.tech and toast_of(r) == "TE-NR2: details saved"
    assert r["HX-Retarget"] == "#drawer" and "TE-NR2 · Multimeter" in html(r)
    # with a certificate on file: the tag stays, and the interval is Decide's
    body = html(client.get(url(w.in_date, "edit"), **HX))
    assert 'name="tag"' not in body and "TE-IN has calibrations or work on record, so its tag stays." in body
    assert 'name="interval_months"' not in body and tes.INTERVAL_PERMISSION.replace("'", "&#x27;") in body
    data = {"kind": "electrical_safety", "manufacturer": "Fluke Biomedical", "model": "ESA615", "serial": "SN-TE-IN",
            "program": "calibration", "held_by": str(w.tech.pk)}
    r = client.post(url(w.in_date, "edit"), data, **HX)  # neither is offered, so the modal posts neither: the rest saves
    w.in_date.refresh_from_db()
    assert toast_of(r) == "TE-IN: details saved" and (w.in_date.tag, w.in_date.interval_months, w.in_date.model) == ("TE-IN", 12, "ESA615")
    r = client.post(url(w.in_date, "edit"), {**data, "tag": "TE-IN", "interval_months": "12", "model": "ESA612"}, **HX)  # as stored
    w.in_date.refresh_from_db()
    assert toast_of(r) == "TE-IN: details saved" and (w.in_date.tag, w.in_date.interval_months, w.in_date.model) == ("TE-IN", 12, "ESA612")
    signed_in("manager")
    assert 'name="interval_months"' in html(client.get(url(w.in_date, "edit"), **HX))
    r = client.post(url(w.in_date, "edit"), {**data, "interval_months": "6"}, **HX)  # the tag left out, as the modal does
    w.in_date.refresh_from_db()
    assert toast_of(r) == "TE-IN: details saved" and w.in_date.interval_months == 6 and w.in_date.tag == "TE-IN"
    r = client.post(url(w.in_date, "edit"), {**data, "tag": "TE-IN", "interval_months": "12"}, **HX)  # the stored tag posted
    w.in_date.refresh_from_db()
    assert toast_of(r) == "TE-IN: details saved" and w.in_date.interval_months == 12 and w.in_date.tag == "TE-IN"


def test_change_refuses_a_changed_locked_tag_in_the_locks_words(client, signed_in, te_world):
    """A tag posted changed while it is locked (calibrations or work on record) is refused in the lock's words, and nothing in the same
    post is saved (review fix: never silently dropped)."""
    w = te_world
    data = {"tag": "TE-IN9", "kind": "electrical_safety", "manufacturer": "Fluke Biomedical", "model": "ESA615", "serial": "SN-TE-IN",
            "program": "calibration", "interval_months": "6", "held_by": str(w.tech.pk)}
    lock = "TE-IN has calibrations or work on record, so its tag stays."
    for role, words in (("technician", f"{lock} {tes.INTERVAL_PERMISSION}"), ("manager", lock)):
        signed_in(role)
        r = client.post(url(w.in_date, "edit"), data, **HX)
        assert r.status_code == 200 and toast_of(r) == "" and list(r.context["form"].non_field_errors()) == [words], role
        assert escape(words) in html(r)
        w.in_date.refresh_from_db()
        assert (w.in_date.tag, w.in_date.interval_months, w.in_date.model) == ("TE-IN", 12, "ESA612"), role


def test_change_refusals_keep_what_was_typed(client, signed_in, te_world):
    signed_in("technician")
    te = te_world.past_due
    data = {"kind": "electrical_safety", "manufacturer": "", "model": "ESA620", "serial": te_world.in_date.serial, "program": "calibration",
            "held_by": "", "interval_months": "12"}
    body = html(client.post(url(te, "edit"), data, **HX))
    assert "Enter the manufacturer." in body and 'value="ESA620"' in body
    data["manufacturer"] = "Fluke Biomedical"
    data["model"] = "ESA612"
    body = html(client.post(url(te, "edit"), data, **HX))
    assert "Fluke Biomedical ESA612 serial SN-TE-IN is already TE-IN: one item, one tag." in body
    signed_in("analyst")
    assert client.get(url(te, "edit"), **HX).status_code == 403


# --- Record calibration ----------------------------------------------------------------------------------------------------------

def test_record_calibration_prefills_the_due_day_and_says_the_cap(client, signed_in, te_world):
    te = te_world.no_record
    today = te_world.today
    signed_in("technician")
    body = html(client.get(url(te, "calibrations/new"), **HX))
    default, cap = default_due(today, 12), due_cap(today, 12)
    assert f'name="due_on" id="tec-due_on" value="{default.isoformat()}"' in body and f'name="due_default" value="{default.isoformat()}"' in body
    assert f"at most {fmt(cap)}" in body and 'hx-target="#tec-due"' in body
    # the due day follows a calibration day changed, unless a day was typed
    day = today - 40 * DAY
    frag = html(client.get(url(te, "calibrations/new"), {"calibrated_on": day.isoformat(), "due_on": default.isoformat(),
                                                         "due_default": default.isoformat()}, **part("tec-due")))
    assert f'value="{default_due(day, 12).isoformat()}"' in frag and f"at most {fmt(due_cap(day, 12))}" in frag and "<form" not in frag
    frag = html(client.get(url(te, "calibrations/new"), {"calibrated_on": day.isoformat(), "due_on": "2027-01-05",
                                                         "due_default": default.isoformat()}, **part("tec-due")))
    assert 'id="tec-due_on" value="2027-01-05"' in frag
    frag = html(client.get(url(te, "calibrations/new"), {"calibrated_on": "junk"}, **part("tec-due")))
    assert "months after the calibration day" in frag
    frag = html(client.get(url(te, "calibrations/new"), {"calibrated_on": "9999-12-31"}, **part("tec-due")))
    assert "<div" in frag  # beyond the calendar: no prefill, never a 500


def cal_post(day, **over):
    data = {"calibrated_on": day.isoformat(), "result": "passed", "as_found": "in_tolerance", "provider": "Fluke Biomedical",
            "certificate": "CAL-77", "accreditation": "shown"}
    data.update(over)
    return data


def test_record_calibration_brings_an_item_back_from_calibration(client, signed_in, te_world):
    te = te_world.past_due
    today = te_world.today
    signed_in("technician")
    client.post(url(te, "status"), {"to": "out_for_calibration"}, **HX)
    prefilled = default_due(today, 12).isoformat()
    r = client.post(url(te, "calibrations/new"), cal_post(today, due_on=prefilled, due_default=prefilled), **HX)
    te.refresh_from_db()
    cal = te.calibrations.get(certificate="CAL-77")
    assert cal.due_on == default_due(today, 12) and te.status == Status.IN_USE and cal.recorded_by.role.slug == "technician"
    assert toast_of(r) == f"TE-PD: calibration of {fmt(today)} recorded; now in use" and r["HX-Retarget"] == "#drawer"


def test_record_calibration_takes_the_day_on_the_label_up_to_the_cap(client, signed_in, te_world):
    te = te_world.no_record
    day = te_world.today - 20 * DAY
    signed_in("technician")
    cap = due_cap(day, 12)
    r = client.post(url(te, "calibrations/new"), cal_post(day, due_on=(cap + DAY).isoformat(), due_default="x"), **HX)
    body = html(r)
    assert not te.calibrations.exists() and f"(by {fmt(cap)})" in body and f'value="{(cap + DAY).isoformat()}"' in body
    assert 'value="CAL-77"' in body and "te-err" in body  # what was typed is kept; the due day shows its error
    client.post(url(te, "calibrations/new"), cal_post(day, due_on=cap.isoformat(), due_default="x"), **HX)
    assert te.calibrations.get().due_on == cap


def test_a_failed_calibration_drops_the_untouched_prefill(client, signed_in, te_world):
    te = te_world.in_date
    today = te_world.today
    signed_in("technician")
    prefilled = default_due(today, 12).isoformat()
    r = client.post(url(te, "calibrations/new"), cal_post(today, result="failed", as_found="out_of_tolerance", due_on=prefilled,
                                                          due_default=prefilled), **HX)
    te.refresh_from_db()
    cal = te.calibrations.get(certificate="CAL-77")
    assert cal.result == Result.FAILED and cal.due_on is None and te.status == Status.OUT_OF_SERVICE
    assert toast_of(r) == f"TE-IN: calibration of {fmt(today)} recorded; now out of service (failed calibration); review the work done with it"
    assert "Failed: review of the work done with it" in html(r)
    # a due day typed on a failed certificate is refused (the service's rule), and shows even though Failed hides the field
    body = html(client.post(url(te_world.no_record, "calibrations/new"), cal_post(today, result="failed", as_found="out_of_tolerance",
                                                                                  due_on="2027-03-31", due_default=prefilled), **HX))
    assert "A failed calibration has no due day: leave it blank." in body and "te-due te-err" in body


def test_record_calibration_refusals_and_junk(client, signed_in, te_world):
    te = te_world.no_record
    signed_in("analyst")
    assert client.get(url(te, "calibrations/new"), **HX).status_code == 403
    signed_in("technician")
    r = client.post(url(te, "calibrations/new"), {"calibrated_on": "2026-02-30", "result": "maybe", "as_found": "zz", "accreditation": "?",
                                                  "certificate": "a b", "provider": "", "adjusted": "yes"}, **HX)
    body = html(r)
    assert r.status_code == 200 and not te.calibrations.exists()
    assert "Enter the calibration day on the certificate as a date." in body and "Choose the result: passed or failed." in body
    assert "Enter the lab or manufacturer that issued the certificate." in body
    future = te_world.today + DAY
    assert "The calibration day cannot be in the future." in html(client.post(url(te, "calibrations/new"), cal_post(future), **HX))
    certificate(te, te_world.today - 3 * DAY, number="CAL-1")
    body = html(client.post(url(te, "calibrations/new"), cal_post(te_world.today - 3 * DAY), **HX))
    assert "Mark the other entered in error first." in body


# --- entered in error ------------------------------------------------------------------------------------------------------------

def test_entered_in_error_at_decide(client, signed_in, te_world):
    te = te_world.past_due
    cal = te.calibrations.get()
    path = f"{url(te)}calibrations/{cal.pk}/error/"
    signed_in("technician")
    assert client.get(path, **HX).status_code == 403 and client.post(path, **HX).status_code == 403
    signed_in("manager")
    body = html(client.get(path, **HX))
    assert "stays on record, marked entered in error" in body and "Its status (in use) stays as it is" in body
    r = client.post(path, **HX)
    cal.refresh_from_db()
    assert cal.entered_in_error and toast_of(r) == tes.entered_in_error_note(Calibration.objects.select_related("test_equipment").get(pk=cal.pk))
    body = html(r)
    assert '<span class="chip neutral">Entered in error</span>' in body and "Marked entered in error</dt>" in body and "te-void" in body
    assert '<span class="chip warn">No calibration on file</span>' in body  # the standing follows the rest
    again = html(client.get(path, **HX))
    assert f"Certificate {cal.certificate} is already marked entered in error." in again
    assert f"Certificate {cal.certificate} is already marked entered in error." in html(client.post(path, **HX))


# --- the tolerance review --------------------------------------------------------------------------------------------------------

def test_the_review_box_before_a_decision(client, signed_in, te_world):
    w = te_world.unreviewed
    signed_in("technician")
    body = html(client.get(url(w.item), **HX))
    assert "Found out of tolerance: review of the work done with it" in body and '<span class="chip crit">Not reviewed</span>' in body
    assert f"Work completed after {fmt(w.before.calibrated_on)} (the passed calibration before it) through {fmt(w.found.calibrated_on)}." in body
    assert "2 work orders in this window now." in body and all(wo.number in body for wo in w.work)
    assert body.index(w.work[1].number) < body.index(w.work[0].number)  # newest first
    assert f"calibrations/{w.found.pk}/review.csv" in body and ">Decide</button>" not in body


def test_decide_posts_the_version_it_was_drawn_with(client, signed_in, te_world):
    w = te_world.unreviewed
    path = f"{url(w.item)}calibrations/{w.found.pk}/review/"
    signed_in("technician")
    assert client.get(path, **HX).status_code == 403
    signed_in("manager")
    body = html(client.get(path, **HX))
    version = tes.review_summary(w.found).version
    assert f'name="version" value="{version}"' in body and "The decision covers the 2 work orders in this window now." in body
    assert all(escape(label) in body for _, label in Basis.choices)
    # work added to the window meanwhile: refused in REVIEW_CHANGED's words, and shown again as it is now
    late = pm_with(w.work[0].asset, w.item, completed_on=te_world.today - 30 * DAY)
    r = client.post(path, {"basis": "within_margin", "decided_on": te_world.today.isoformat(), "version": version}, **HX)
    body = html(r)
    assert not ToleranceReview.objects.filter(calibration=w.found).exists()
    assert tes.REVIEW_CHANGED in body and "The decision covers the 3 work orders in this window now." in body and late.number in body
    fresh = tes.review_summary(w.found).version
    assert f'name="version" value="{fresh}"' in body and re.search(r'value="within_margin"[^>]*checked', body)  # what was chosen is kept
    r = client.post(path, {"basis": "rechecked", "decided_on": te_world.today.isoformat(), "version": fresh, "quality_reference": "QR-12"}, **HX)
    review = ToleranceReview.objects.get(calibration=w.found)
    assert review.work_count == 3 and review.quality_reference == "QR-12"
    assert toast_of(r) == f"Review of certificate {w.found.certificate} recorded: 3 work orders"
    body = html(r)
    assert '<span class="chip ok">Reviewed</span>' in body and f"Reviewed {fmt(te_world.today)}: the affected devices were rechecked" in body
    assert "QR-12" in body
    assert "Work orders decided on</dt><dd>3" in body and "0 of 1 rechecked" in body and ">Decide</button>" not in body
    assert "already recorded" in html(client.get(path, **HX))


def test_after_a_decision_the_box_shows_work_added_and_the_recheck(client, signed_in, te_world):
    w = te_world.reviewed  # decided today, within margin
    signed_in("manager")
    body = html(client.get(url(w.item), **HX))
    assert '<span class="chip ok">Reviewed</span>' in body and "smaller than the margin to the limits used" in body
    assert "rechecked" not in body  # a within-margin decision has no recheck to follow
    added = pm_with(w.work[0].asset, w.item, completed_on=te_world.today - 20 * DAY)
    body = html(client.get(url(w.item), **HX))
    assert "1 work order added since the review: the decision does not cover it." in body and '<span class="chip warn">Reviewed</span>' in body
    assert added.number in body and "Added since the review</span>" in body


def test_a_recheck_decision_counts_the_devices_rechecked(client, signed_in, te_world):
    w = te_world.unreviewed
    tes.decide_review(w.found, basis=Basis.NEXT_WORK, decided_on=te_world.today, version=None, by=None)
    pm_with(w.work[0].asset, w.item)  # a PM on the vent today, with the item in date: rechecked
    signed_in("manager")
    body = html(client.get(url(w.item), **HX))
    assert "1 of 1 rechecked" in body and f"{w.work[0].asset.tag}: rechecked on " in body


def test_review_refusals_and_junk(client, signed_in, te_world):
    w = te_world.unreviewed
    path = f"{url(w.item)}calibrations/{w.found.pk}/review/"
    signed_in("manager")
    version = tes.review_summary(w.found).version
    body = html(client.post(path, {"basis": "zzz", "decided_on": "yesterday", "version": version, "quality_reference": "a b"}, **HX))
    assert "Choose the decision." in body and "Enter the day it was decided as a date." in body
    body = html(client.post(path, {"basis": "within_margin", "decided_on": "1999-01-01", "version": version}, **HX))
    assert "The decision comes on or after the calibration day" in body
    body = html(client.post(path, {"basis": "within_margin", "decided_on": te_world.today.isoformat()}, **HX))  # no version
    assert tes.REVIEW_CHANGED in body and not ToleranceReview.objects.filter(calibration=w.found).exists()
    passed = w.before
    assert "there is nothing to review" in html(client.get(f"{url(w.item)}calibrations/{passed.pk}/review/", **HX))


# --- work done with it, the CSVs --------------------------------------------------------------------------------------------------

def test_work_done_with_it(client, signed_in, te_world):
    w = te_world
    signed_in("technician")
    body = html(client.get(url(w.in_date), **HX))
    assert "1 work order since its last calibration (" in body and w.pm.number in body
    assert f'hx-get="/work-orders/{w.pm.number}/"' in body and f'hx-get="/equipment/{w.pm.asset.tag}/"' in body
    judged = tes.work_done(w.in_date)[0]
    assert judged.standing.words in body and "/test-equipment/TE-IN/uses.csv" in body
    rows = csv_rows(client.get("/test-equipment/TE-IN/uses.csv"))
    assert rows[0] == ["Work order", "Type", "Device", "Day of the work", "Standing that day", "Found out of tolerance later"]
    assert rows[1][:4] == [w.pm.number, "Preventive maintenance", w.pm.asset.tag, w.today.isoformat()] and rows[1][4] == judged.standing.words
    later = csv_rows(client.get(f"/test-equipment/{w.unreviewed.item.tag}/uses.csv"))
    assert len(later) == 3 and all(r[5] == f"found out of tolerance on {fmt(w.unreviewed.found.calibrated_on)}; not reviewed" for r in later[1:])
    assert "No completed work has recorded TE-NR yet." in html(client.get(url(w.no_record), **HX))


def test_the_review_csv(client, signed_in, te_world):
    w = te_world.reviewed
    signed_in("analyst")
    added = pm_with(w.work[0].asset, w.item, completed_on=te_world.today - 20 * DAY)
    r = client.get(f"{url(w.item)}calibrations/{w.found.pk}/review.csv")
    rows = csv_rows(r)
    assert rows[0] == ["Work order", "Type", "Device", "Day of the work", "In the review"]
    assert {(row[0], row[4]) for row in rows[1:]} == {(w.work[0].number, "Yes"), (w.work[1].number, "Yes"), (added.number, "Added since the review")}
    assert re.search(r'filename="cadence-TE-OT2-review-C-TE-OT2-\d{8}-\d{4}-\d{2}-\d{2}\.csv"', r["Content-Disposition"])
    assert client.get(f"{url(w.item)}calibrations/{w.before.pk}/review.csv").status_code == 404  # passed in tolerance: no review


def test_the_work_orders_need_work_orders_view(client, tenant, te_world):
    custom_user(client, tenant, "kit-keeper", {Module.EQUIPMENT: Level.EDIT})
    w = te_world.unreviewed
    body = html(client.get(url(w.item), **HX))
    assert "2 work orders in this window now." in body and w.work[0].number not in body
    assert "The work orders themselves need Work orders View." in body
    assert client.get(f"{url(w.item)}uses.csv").status_code == 403
    assert client.get(f"{url(w.item)}calibrations/{w.found.pk}/review.csv").status_code == 403


# --- History ---------------------------------------------------------------------------------------------------------------------

def test_history_carries_the_calibrations_reviews_and_uses(client, signed_in, te_world):
    w = te_world.reviewed
    signed_in("analyst")
    r = client.get(url(w.item) + "?history=1", **HX)
    body = html(r)
    assert ">Added</span>" in body and f"Calibration {w.found.certificate} added" in body and f"Review of calibration {w.found.certificate} added" in body
    assert f"Recorded on {w.work[0].number}" in body and "Recorded with the completion" in body
    part_ = client.get(url(w.item) + "?history=1", **part("hist"))
    assert "<ol class=\"hist\">" in html(part_) and "dr-h" not in html(part_)
    assert "Show changes" in html(client.get(url(w.item), **HX))


def test_history_leaves_the_uses_out_without_work_orders_view(client, tenant, te_world):
    custom_user(client, tenant, "kit-reader", {Module.EQUIPMENT: Level.VIEW})
    w = te_world.reviewed
    body = html(client.get(url(w.item) + "?history=1", **HX))
    assert f"Calibration {w.found.certificate} added" in body and "Recorded on" not in body and w.work[0].number not in body


# --- who may open these ----------------------------------------------------------------------------------------------------------

def test_scoped_users_are_refused_everything(client, signed_in, tenant, te_world):
    te = te_world.unreviewed.item
    cal = te_world.unreviewed.found
    paths = [LIST, "/test-equipment/new/", "/test-equipment/export.csv", url(te), url(te, "edit"), url(te, "calibrations/new"),
             f"{url(te)}uses.csv", f"{url(te)}calibrations/{cal.pk}/error/", f"{url(te)}calibrations/{cal.pk}/review/",
             f"{url(te)}calibrations/{cal.pk}/review.csv"]
    for role in ("vendor", "requester"):
        signed_in(role)
        for path in paths:
            assert client.get(path, **HX).status_code == 403, (role, path)
        assert client.post(url(te, "status"), {"to": "missing"}, **HX).status_code == 403
    custom_user(client, tenant, "acme", {Module.EQUIPMENT: Level.FULL, Module.WORKORDERS: Level.FULL}, scope="company", company="Acme")
    assert all(client.get(p, **HX).status_code == 403 for p in paths)


def test_the_tab_strip(client, signed_in, ctx):
    signed_in("technician")
    devices = html(client.get("/equipment/"))
    assert 'href="/equipment/" aria-current="page">Devices</a>' in devices and 'href="/test-equipment/">Test equipment</a>' in devices


# --- markup ----------------------------------------------------------------------------------------------------------------------

def test_mobile_safe_markup(client, signed_in, te_world):
    signed_in("director")
    page = html(client.get(LIST))
    assert '<div class="tbl-wrap">' in page and 'name="viewport"' in page
    css = Path("apps/web/static/web/cadence.css").read_text()
    block = css[css.index("/* Slice 35 A: the Equipment screen"):css.index("/* Slice 35 A: end */")]
    assert "@media (max-width:760px){" in block and ".te-cert-grid{grid-template-columns:1fr}" in block
    assert block.count("{") == block.count("}")
    phone = block[block.index("@media (max-width:760px){"):]
    assert phone.count("{") == phone.count("}")
    for template in ("_te_new.html", "_te_edit.html", "_te_calibrate.html", "_te_review.html"):
        assert '<div class="form">' in Path(f"apps/web/templates/web/{template}").read_text(), template  # one column on a phone


def test_the_words(client, signed_in, te_world):
    """Never "instrument", never "calibrated" or "uncalibrated" or "in date" as a status, and never that a standard requires it."""
    signed_in("director")
    w = te_world.unreviewed
    bodies = [html(client.get(LIST)), html(client.get(url(w.item), **HX)), html(client.get("/test-equipment/new/", **HX)),
              html(client.get(url(w.item, "calibrations/new"), **HX)), html(client.get(f"{url(w.item)}calibrations/{w.found.pk}/review/", **HX))]
    text = " ".join(re.sub(r"<[^>]+>", " ", b) for b in bodies).lower()
    for word in ("instrument", "uncalibrated", "in date", " calibrated ", "requires", "required by"):
        assert word not in text, word
    assert AsFound.OUT_OF_TOLERANCE.label.lower() in text and Kind.INFUSION.label.lower() in text


# --- under the policies -----------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_screens_under_the_policies(client, signed_in, tenant, te_world):
    """As the runtime role: the list, its CSV (streamed after the middleware has left the tenant), the drawer with its review boxes and
    History, a status move, Record calibration, the review's decision, and both CSVs."""
    w = te_world
    signed_in("director")
    as_app_role()
    assert "TE-PD" in html(client.get(LIST)) and len(csv_rows(client.get("/test-equipment/export.csv"))) == 7
    body = html(client.get(url(w.unreviewed.item), **HX))
    assert "Not reviewed" in body and w.unreviewed.work[0].number in body
    assert "Recorded on" in html(client.get(url(w.reviewed.item) + "?history=1", **HX))
    r = client.post(url(w.past_due, "status"), {"to": "out_for_calibration"}, **HX)
    assert toast_of(r) == "TE-PD: out for calibration"
    r = client.post(url(w.past_due, "calibrations/new"), cal_post(w.today), **HX)
    assert toast_of(r).endswith("recorded; now in use")
    with tenant_context(tenant):  # each request leaves the tenant as it found it: read the list inside it, as the modal does
        version = tes.review_summary(w.unreviewed.found).version
    assert f'name="version" value="{version}"' in html(client.get(f"{url(w.unreviewed.item)}calibrations/{w.unreviewed.found.pk}/review/", **HX))
    r = client.post(f"{url(w.unreviewed.item)}calibrations/{w.unreviewed.found.pk}/review/",
                    {"basis": "function_unused", "decided_on": w.today.isoformat(), "version": version}, **HX)
    assert toast_of(r).endswith("recorded: 2 work orders"), errors(html(r))
    assert len(csv_rows(client.get(f"{url(w.unreviewed.item)}uses.csv"))) == 3
    assert len(csv_rows(client.get(f"{url(w.unreviewed.item)}calibrations/{w.unreviewed.found.pk}/review.csv"))) == 3
    with tenant_context(tenant):
        assert ToleranceReview.objects.filter(calibration=w.unreviewed.found).exists()
