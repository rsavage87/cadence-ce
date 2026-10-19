"""
Slice 35, test equipment on the work order (wave 2B): the completion modal's "Test equipment used (optional)" (offered by type,
outcome, vendor, import, and scope; Yours, then Other test equipment by kind; never pre-ticked; only usable items, plus those already
on the work order, kept ones ticked and disabled with a hidden input; the shown_test_equipment marker: absent keeps, shown with none
ticked clears; ticks dropped with the vendor's report; refusals keyed test_equipment with the ticks kept and focused; Mark the rest pass
keeps every tick), the drawer's "Test equipment" section (rules' words on the day of the work, flagged ones, found out of tolerance
later with the review's state, never a scoped reader's) and its Change (Edit, Approve once closed, never scoped; a save answers with the
drawer), the print's line, the work order's History, and the fixed number of queries the drawer, the print, and the modal run. The
last test runs the screens under the row-level security policies.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import FINDING, checked_post
from testequipment_fixtures import (
    bench_tech,
    certificate,
    imported_work,
    in_date,
    item,
    no_record,
    out_of_tolerance,
    past_due,
    pm_with,
    repair_with,
    safety_with,
)

from apps.accounts.models import Level, Module, Role, User
from apps.tenants.context import tenant_context
from apps.testequipment import rules
from apps.testequipment import services as te_services
from apps.testequipment.models import Kind, Reason, Status, TestEquipmentUse
from apps.web import views_wo_test_equipment as v
from apps.web.forms_wo_complete import CompleteForm
from apps.workorders.models import PmResult, Priority, RepairCheck, WoStatus, WoType
from apps.workorders.repair_checks import POST_REPAIR_CHECKLIST
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
HX_HIST = {**HX, "HTTP_HX_TARGET": "hist"}
DAY = timedelta(days=1)
VENDOR = "Philips"
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, val in fields.items():
            setattr(user, k, val)
        user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def tech(ctx):
    return bench_tech("Dana Whitfield")


def open_work(asset, type_=WoType.PM, *, tech=None, vendor="", start=True):
    """In-house work of `type_` on `asset` (assigned to `tech`, else a bench technician), or vendor service with `vendor`; started."""
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Scheduled work",
                           assigned_to=None if vendor else (tech or bench_tech()))
    if vendor:
        assign(wo, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def url(wo, what="complete") -> str:
    return f"/work-orders/{wo.number}/{what}/"


def picker(body: str) -> str:
    m = re.search(r'<fieldset class="full wte".*?</fieldset>', body, re.S)
    return m.group(0) if m else ""


def boxes(body: str) -> list[tuple]:
    """(id, checked, disabled) of each checkbox in the picker, in order."""
    out = []
    for attrs in re.findall(r'<input type="checkbox"([^>]*)>', picker(body)):
        out.append((re.search(r'value="([^"]*)"', attrs).group(1), " checked" in attrs, " disabled" in attrs))
    return out


def ticked_ids(body: str) -> set:
    return {pk for pk, checked, _disabled in boxes(body) if checked}


def hidden_kept(body: str) -> list:
    return re.findall(r'<input type="hidden" name="test_equipment" value="([^"]*)">', picker(body))


def section(body: str, key: str) -> str:
    """The picker's section `key` ("yours", "others"), from its opening tag to the next section or the picker's end."""
    part = picker(body)
    m = re.search(rf'<(details|div) class="wte-sec wte-{key}"', part)
    if not m:
        return ""
    rest = part[m.start():]
    following = re.search(r'<(details|div) class="wte-sec ', rest[1:])
    return rest[:following.start() + 1] if following else rest


def row(body: str) -> str:
    """The drawer's "Test equipment" section ("" when there is none)."""
    m = re.search(r'<div class="sec wte-row" id="wo-te">.*?\n</div>', body, re.S)
    return m.group(0) if m else ""


def uses_of(wo) -> list:
    return sorted(TestEquipmentUse.objects.filter(work_order=wo).values_list("test_equipment__tag", flat=True))


def pm_post(*items, shown=True, **extra) -> dict:
    data = {"resolution": "Done per the procedure", "pm_result": PmResult.PASS, **extra}
    if shown:
        data["shown_test_equipment"] = "1"
    if items:
        data["test_equipment"] = [str(i.pk) for i in items]
    return data


def triggers(r) -> dict:
    return json.loads(r["HX-Trigger"]) if "HX-Trigger" in r else {}


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


# --- the completion modal: where it is offered -----------------------------------------------------------------------------------

@pytest.mark.parametrize("type_", [WoType.PM, WoType.SAFETY, WoType.REPAIR, WoType.RECALL])
def test_in_house_work_of_any_type_offers_the_picker_after_the_steps(client, signed_in, pump, tech, type_):
    te = in_date("TE-01")
    wo = open_work(pump, type_, tech=tech)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert "Test equipment used" in picker(body) and '<input type="hidden" name="shown_test_equipment" value="1">' in picker(body)
    assert boxes(body) == [(str(te.pk), False, False)]  # never ticked for the user
    assert body.index('class="full wte"') < body.index('name="resolution"')
    if type_ == WoType.REPAIR:
        assert body.index('name="repair_check"') < body.index('class="full wte"')


def test_an_inspection_offers_it_after_its_checklist(client, signed_in, pump, tech):
    in_date("TE-01")
    wo = open_work(pump, WoType.INSPECTION, tech=tech)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert body.index('class="mini-tbl wc-steps"') < body.index('class="full wte"') < body.index('name="inspection_result"')


def test_nothing_is_offered_without_test_equipment_vendor_work_or_a_scoped_user(client, signed_in, pump, tech, ctx):
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    assert picker(client.get(url(wo), **HX).content.decode()) == ""  # the facility keeps no test equipment in Cadence
    in_date("TE-01")
    assert picker(client.get(url(wo), **HX).content.decode()) != ""
    vendor_pm = open_work(pump, WoType.PM, vendor=VENDOR)
    signed_in("manager")
    assert picker(client.get(url(vendor_pm), **HX).content.decode()) == ""  # a vendor PM: the vendor's report names theirs
    vendor_fix = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    body = client.get(url(vendor_fix), **HX).content.decode()
    assert picker(body) != "" and v.VENDOR_HINT in picker(body)  # a repair: offered unless the outcome is the vendor's report
    signed_in("vendor", company=VENDOR)
    assert picker(client.get(url(vendor_fix), **HX).content.decode()) == ""  # never a scoped user's
    assert v.offer(imported_work(pump), None, timezone.localdate()) is None


def test_the_picker_hides_with_the_vendors_report_in_the_css():
    css = CSS.read_text()
    block = css[css.index("/* Slice 35 B: test equipment"):css.index("/* Slice 35 B: end */")]
    assert '.wc-form:has(input[name="repair_check"][value="vendor_report"]:checked) .wte{display:none}' in block
    assert "min-height:44px" in block  # a phone's rows


# --- the completion modal: what it lists ---------------------------------------------------------------------------------------

def test_yours_first_then_the_others_collapsed_by_kind(client, signed_in, pump, tech):
    mine = in_date("TE-01", held_by=tech)
    sim = in_date("TE-02", kind=Kind.PATIENT_SIMULATOR, model="ProSim 8")
    flow = in_date("TE-03", kind=Kind.GAS_FLOW, model="VT900A")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    yours, others = section(body, "yours"), section(body, "others")
    assert str(mine.pk) in yours and "Dana Whitfield&#x27;s kit" in yours
    assert others.startswith('<details class="wte-sec wte-others">') and "Other test equipment (2)" in others  # Yours has some: collapsed
    assert others.index("Patient simulator") < others.index(str(sim.pk)) < others.index("Gas flow analyzer") < others.index(str(flow.pk))


def test_others_open_when_yours_is_empty_and_they_are_few(client, signed_in, pump, tech):
    for n in range(12):
        in_date(f"TE-{n:02d}")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    assert section(client.get(url(wo), **HX).content.decode(), "others").startswith('<details class="wte-sec wte-others" open>')
    in_date("TE-12")  # thirteen: collapsed
    assert section(client.get(url(wo), **HX).content.decode(), "others").startswith('<details class="wte-sec wte-others">')


def test_what_that_technician_recorded_lately_joins_yours(client, signed_in, pump, vent, tech, today):
    used = in_date("TE-01")
    in_date("TE-02")
    pm_with(vent, used, tech=tech, completed_on=today - 10 * DAY)
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert str(used.pk) in section(body, "yours") and str(used.pk) not in section(body, "others")


def test_each_option_shows_its_standing_today_flagged_ones_in_the_warning_style(client, signed_in, pump, tech, today):
    good, late, none = in_date("TE-01"), past_due("TE-02"), no_record("TE-03")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    body = picker(client.get(url(wo), **HX).content.decode())
    words = {i.pk: rules.standing_on(i, today, i.calibrations.all()).words for i in (good, late, none)}
    assert f'<span class="chip neutral">{words[good.pk]}</span>' in body
    assert f'<span class="chip warn">{words[late.pk]}</span>' in body and "Calibration was due" in words[late.pk]
    assert '<span class="chip warn">No calibration on file</span>' in body
    assert "<b class=\"mono\">TE-01</b> <span class=\"wte-name\">Fluke Biomedical ESA612</span>" in body


def test_only_usable_items_are_offered_plus_those_on_the_work_order(client, signed_in, pump, tech, today):
    usable = in_date("TE-01")
    away = in_date("TE-02")
    te_services.set_status(away, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - 3 * DAY)
    broke_today = in_date("TE-03")
    te_services.set_status(broke_today, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=None)  # changed today: work this morning
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert [pk for pk, *_ in boxes(body)] == [str(usable.pk), str(broke_today.pk)]
    assert "Out of service (damaged)" in picker(body)


# --- the completion modal: what it sends -------------------------------------------------------------------------------------

def test_completing_records_the_items_ticked(client, signed_in, pump, tech):
    a, b = in_date("TE-01"), past_due("TE-02")
    in_date("TE-03")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    r = client.post(url(wo), pm_post(a, b), **HX)
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == ["TE-01", "TE-02"]
    body = r.content.decode()
    assert "TE-02 (Fluke Biomedical ESA612)</a>: calibration was due" in row(body)  # past due: recorded, and the record says so


def test_no_marker_keeps_what_was_recorded_and_the_marker_with_none_clears(client, signed_in, pump, tech, today):
    a = in_date("TE-01")
    wo = pm_with(pump, a, tech=tech, completed_on=today)
    change_status(wo, WoStatus.IN_PROGRESS)  # reopened
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert boxes(body) == [(str(a.pk), True, False)]  # on the work order: ticked, and it may be taken off
    r = client.post(url(wo), pm_post(shown=False), **HX)  # the picker was not on screen: kept
    assert r.get("HX-Retarget") == "#drawer", r.content.decode()[-3000:]
    assert uses_of(wo) == ["TE-01"]
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    client.post(url(wo), pm_post(), **HX)  # shown, nothing ticked: none
    assert uses_of(wo) == []


def test_a_kept_use_is_ticked_disabled_and_posted_hidden(client, signed_in, pump, tech, today):
    a, b = in_date("TE-01"), in_date("TE-02")
    wo = pm_with(pump, a, tech=tech, completed_on=today - 5 * DAY)
    certificate(a, today - 2 * DAY)  # dated after the work: it has been judged with it since
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    assert (str(a.pk), True, True) in boxes(body) and hidden_kept(body) == [str(a.pk)] and v.KEPT_WORDS in picker(body)
    r = client.post(url(wo), pm_post(a, b), **HX)  # the hidden input carries it with the new tick
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == ["TE-01", "TE-02"]


def test_leaving_a_kept_use_out_is_refused_with_the_ticks_kept_and_focused(client, signed_in, pump, tech, today):
    a, b = in_date("TE-01"), in_date("TE-02")
    wo = pm_with(pump, a, tech=tech, completed_on=today - 5 * DAY)
    certificate(a, today - 2 * DAY)
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    r = client.post(url(wo), pm_post(b), **HX)
    body = r.content.decode()
    assert "HX-Retarget" not in r and "TE-01 stays on" in picker(body) and 'id="wc-te-error"' in body
    assert ticked_ids(body) == {str(a.pk), str(b.pk)}
    assert re.search(r'<input type="checkbox" name="test_equipment" value="%s" checked autofocus>' % b.pk, body)
    assert 'autofocus' not in body.split('name="resolution"')[1].split(">")[0]
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and uses_of(wo) == ["TE-01"]


def test_an_item_taken_out_of_use_meanwhile_is_refused_in_the_services_words(client, signed_in, pump, tech, today):
    a = in_date("TE-01")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    client.get(url(wo), **HX)
    te_services.set_status(a, to=Status.MISSING, by=None, today=today - DAY)  # as if yesterday: not usable today
    r = client.post(url(wo), pm_post(a), **HX)
    body = r.content.decode()
    assert "TE-01 is missing: only test equipment in use is recorded on work completed today." in body
    assert uses_of(wo) == [] and "HX-Retarget" not in r


def test_junk_ids_are_refused_never_a_500(client, signed_in, pump, tech):
    in_date("TE-01")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    for junk in (["not-an-id"], ["00000000-0000-0000-0000-000000000000"], ["'; drop table"]):
        r = client.post(url(wo), {**pm_post(), "test_equipment": junk}, **HX)
        assert r.status_code == 200 and te_services.CHOOSE in r.content.decode()
    assert uses_of(wo) == []


def test_another_facilitys_item_is_refused(client, signed_in, pump, tech, other_tenant):
    in_date("TE-01")
    with tenant_context(other_tenant):
        theirs = item("TE-99")
    wo = open_work(pump, tech=tech)
    signed_in("technician")
    r = client.post(url(wo), {**pm_post(), "test_equipment": [str(theirs.pk)]}, **HX)
    assert te_services.CHOOSE in r.content.decode() and uses_of(wo) == []


def test_ticks_are_dropped_with_the_vendors_report_and_kept_with_ours(client, signed_in, pump):
    a = in_date("TE-01")
    signed_in("manager")
    reported = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    data = {"resolution": "Vendor replaced the board", "repair_check": RepairCheck.VENDOR_REPORT, "checks_reference": "FSO-1",
            "repair_finding": FINDING, "vendor_engineer": "", "shown_test_equipment": "1", "test_equipment": [str(a.pk)]}
    r = client.post(url(reported), data, **HX)
    assert r["HX-Retarget"] == "#drawer" and uses_of(reported) == []
    ours = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    r = client.post(url(ours), checked_post(resolution="Checked by CE", vendor_engineer="", shown_test_equipment="1",
                                            test_equipment=[str(a.pk)]), **HX)
    assert r["HX-Retarget"] == "#drawer" and uses_of(ours) == ["TE-01"]


def test_a_scoped_user_never_records_test_equipment(client, signed_in, pump):
    a = in_date("TE-01")
    wo = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    signed_in("vendor", company=VENDOR)
    r = client.post(url(wo), checked_post(resolution="Fixed", vendor_engineer="", shown_test_equipment="1", test_equipment=[str(a.pk)]), **HX)
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == []  # the form never takes it from them


def test_the_picker_comes_after_the_steps_in_the_focus_order():
    steps = [("Visual", None), ("Leakage", "Record leakage µA")]

    def first(errors, **kwargs):
        form = CompleteForm({"resolution": ""}, steps=steps, is_pm=True, offer_test_equipment=True, offer_late_reason=True, **kwargs)
        form.is_valid()
        for name in errors:
            form.add_error(name, "x")
        form.focus_first_error()
        return form.first_error

    assert first(["test_equipment", "reading_2"]) == "reading_2"
    assert first(["test_equipment", "late_reason", "pm_result"], late_first=True) == "test_equipment"
    assert first(["test_equipment", "pm_result", "resolution"]) == "test_equipment"
    form = CompleteForm({"resolution": "x"}, steps=steps, is_pm=True)  # not offered: never taken from the post
    assert "test_equipment" not in form.fields and "shown_test_equipment" not in form.fields


def test_mark_the_rest_pass_keeps_both_ticks(client, signed_in, pump, tech):
    a, b = in_date("TE-01"), in_date("TE-02")
    in_date("TE-03")
    wo = open_work(pump, WoType.REPAIR, tech=tech)
    signed_in("technician")
    params = {**checked_post(), "fill": "pass", "shown_test_equipment": "1", "test_equipment": [str(a.pk), str(b.pk)]}
    for n in (1, 2):
        params.pop(f"step_{n}")
    body = client.get(url(wo), params, **HX).content.decode()
    assert ticked_ids(body) == {str(a.pk), str(b.pk)}
    assert 'name="step_1" value="pass" aria-label="Step 1: Pass" checked' in body


def test_keep_open_records_no_test_equipment(client, signed_in, pump, tech):
    a = in_date("TE-01")
    wo = open_work(pump, WoType.REPAIR, tech=tech)
    signed_in("technician")
    steps = ["pass"] * len(POST_REPAIR_CHECKLIST)
    steps[2] = "fail"
    r = client.post(url(wo, "keep-open"), checked_post(*steps, resolution="Leakage high", shown_test_equipment="1",
                                                        test_equipment=[str(a.pk)]), **HX)
    assert r.status_code == 200 and "kept open" in triggers(r)["toast"]["value"] and uses_of(wo) == []


# --- the drawer's section -----------------------------------------------------------------------------------------------------

def test_the_drawer_reads_each_item_on_the_day_of_the_work(client, signed_in, pump, tech, today):
    a, late = in_date("TE-01"), past_due("TE-02")
    wo = pm_with(pump, a, late, tech=tech, completed_on=today - 3 * DAY)
    signed_in("technician")
    body = row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())
    assert f"<h3>Test equipment <span>2 used on {day(wo.completed_on)}</span></h3>" in body
    judged = {j.item.tag: j.words for j in te_services.judged_for(wo)}
    for tag, words in judged.items():
        name = f"{tag} (Fluke Biomedical ESA612)"
        assert f'<a class="link" href="/test-equipment/{tag}/" hx-get="/test-equipment/{tag}/" hx-target="#drawer">{name}</a>{words[len(name):]}' in body
    assert "on the day of this work" in judged["TE-01"] and body.count('<li class="flag">') == 1
    assert 'hx-get="/work-orders/%s/test-equipment/"' % wo.number in body and ">Change</button>" in body


def test_found_out_of_tolerance_later_reads_with_its_reviews_state(client, signed_in, vent):
    unreviewed = out_of_tolerance(vent, "TE-OT1")
    reviewed = out_of_tolerance(vent, "TE-OT2", reviewed=True, model="IDA-6")
    signed_in("manager")
    first = row(client.get(f"/work-orders/{unreviewed.work[0].number}/", **HX).content.decode())
    assert f"found out of tolerance on {day(unreviewed.found.calibrated_on)}; not reviewed" in first and '<li class="flag">' in first
    second = row(client.get(f"/work-orders/{reviewed.work[0].number}/", **HX).content.decode())
    assert f"reviewed {day(reviewed.review.decided_on)}: the error was smaller than the margin to the limits used" in second


def test_none_recorded_offers_record_and_a_facility_without_test_equipment_sees_nothing(client, signed_in, pump, tech, today):
    wo = pm_with(pump, tech=tech, completed_on=today)
    signed_in("technician")
    assert row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode()) == ""
    in_date("TE-01")
    body = row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())
    assert "None recorded." in body and ">Record</button>" in body


def test_the_section_shows_only_once_done_and_never_to_a_scoped_reader(client, signed_in, pump, today):
    a = in_date("TE-01")
    open_one = open_work(pump)
    signed_in("technician")
    assert row(client.get(f"/work-orders/{open_one.number}/", **HX).content.decode()) == ""
    signed_in("manager")
    done = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    client.post(url(done), checked_post(resolution="Checked by CE", vendor_engineer="", shown_test_equipment="1", test_equipment=[str(a.pk)]), **HX)
    assert "TE-01" in row(client.get(f"/work-orders/{done.number}/", **HX).content.decode())
    signed_in("vendor", company=VENDOR)
    body = client.get(f"/work-orders/{done.number}/", **HX).content.decode()
    assert row(body) == "" and "TE-01" not in body


def test_the_test_equipment_needs_equipment_view(client, pump, tech, today, ctx):
    """Merge rule (slice 35): the drawer's section and the print's line need Equipment View, as the API's field and the change log's
    area do; a work order clerk with Work orders View alone reads the work order without them."""
    a = in_date("TE-01")
    wo = pm_with(pump, a, tech=tech, completed_on=today)
    clerk = Role.objects.create(name="Work order clerk", slug="wo-clerk")
    clerk.set_levels({Module.WORKORDERS: Level.VIEW})
    user = User.objects.create_user(username="clerk@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=clerk)
    client.force_login(user)
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert "TE-01" not in body and "/test-equipment/" not in body
    assert "TE-01" not in client.get(f"/print/work-orders/{wo.number}/").content.decode()


def test_the_drawer_reads_any_number_of_uses_in_the_same_queries(client, signed_in, pump, tech, today):
    one = pm_with(pump, in_date("TE-01"), tech=tech, completed_on=today)
    many = pm_with(pump, in_date("TE-02"), past_due("TE-03"), no_record("TE-04"), tech=tech, completed_on=today)
    signed_in("technician")
    client.get(f"/work-orders/{one.number}/", **HX)
    with CaptureQueriesContext(connection) as a:
        client.get(f"/work-orders/{one.number}/", **HX)
    with CaptureQueriesContext(connection) as b:
        client.get(f"/work-orders/{many.number}/", **HX)
    assert len(a) == len(b)


# --- Change -------------------------------------------------------------------------------------------------------------------

def test_change_judges_the_options_on_the_day_of_the_work_and_saves(client, signed_in, pump, tech, today):
    a, b = in_date("TE-01"), in_date("TE-02")
    work_day = today - 30 * DAY
    wo = pm_with(pump, a, tech=tech, completed_on=work_day)
    signed_in("technician")
    body = client.get(url(wo, "test-equipment"), **HX).content.decode()
    assert f"Tick what was used on {day(work_day)}, the day of this work" in body
    assert boxes(body) == [(str(a.pk), True, False), (str(b.pk), False, False)]
    words = rules.standing_on(b, work_day, b.calibrations.all()).words
    assert f'<span class="chip neutral">{words}</span>' in picker(body)
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1", "test_equipment": [str(b.pk)]}, **HX)
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == ["TE-02"]
    events = triggers(r)
    assert events["toast"]["value"] == f"Test equipment on {wo.number}: TE-02" and "wo-changed" in events
    assert "modal-close" in json.loads(r["HX-Trigger-After-Settle"])
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX)
    assert uses_of(wo) == [] and triggers(r)["toast"]["value"] == f"Test equipment on {wo.number}: none recorded"


def test_change_keeps_a_kept_use_and_refuses_leaving_it_out(client, signed_in, pump, tech, today):
    a = in_date("TE-01")
    wo = pm_with(pump, a, tech=tech, completed_on=today - 5 * DAY)
    certificate(a, today - 2 * DAY)
    signed_in("technician")
    body = client.get(url(wo, "test-equipment"), **HX).content.decode()
    assert boxes(body)[0] == (str(a.pk), True, True) and hidden_kept(body) == [str(a.pk)]
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX)
    body = r.content.decode()
    assert "TE-01 stays on" in body and 'id="wt-te-error"' in body and uses_of(wo) == ["TE-01"] and "HX-Retarget" not in r


def test_change_at_each_level(client, signed_in, pump, tech, today):
    a = in_date("TE-01")
    wo = pm_with(pump, a, tech=tech, completed_on=today)
    signed_in("analyst")
    assert client.get(url(wo, "test-equipment"), **HX).status_code == 403
    signed_in("technician")
    assert 'id="wt-form"' in client.get(url(wo, "test-equipment"), **HX).content.decode()
    change_status(wo, WoStatus.CLOSED)
    body = client.get(url(wo, "test-equipment"), **HX).content.decode()
    assert v.CLOSED.format(number=wo.number) in body and 'id="wt-form"' not in body
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX)
    assert v.CLOSED.format(number=wo.number) in r.content.decode() and uses_of(wo) == ["TE-01"]
    drawer = row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())
    assert "Change</button>" not in drawer
    signed_in("manager")
    assert ">Change</button>" in row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())
    client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX)
    assert uses_of(wo) == []


@pytest.mark.parametrize("who", ["vendor", "requester"])
def test_change_refuses_a_scoped_user(client, signed_in, pump, tech, today, who):
    wo = pm_with(pump, in_date("TE-01"), tech=tech, completed_on=today)
    signed_in(who, company=VENDOR) if who == "vendor" else signed_in(who, department="ICU")
    assert client.get(url(wo, "test-equipment"), **HX).status_code == 403
    assert client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX).status_code == 403
    assert uses_of(wo) == ["TE-01"]


def test_change_says_why_on_work_that_records_none(client, signed_in, pump, today):
    in_date("TE-01")
    signed_in("manager")
    open_one = open_work(pump)
    assert te_services.NOT_DONE.format(number=open_one.number) in client.get(url(open_one, "test-equipment"), **HX).content.decode()
    reported = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    client.post(url(reported), {"resolution": "Vendor fixed it", "repair_check": RepairCheck.VENDOR_REPORT, "checks_reference": "FSO-1",
                                "repair_finding": FINDING, "vendor_engineer": ""}, **HX)
    assert v.VENDOR_CHECKED.format(number=reported.number) in client.get(url(reported, "test-equipment"), **HX).content.decode()
    imported = imported_work(pump)
    assert v.IMPORTED.format(number=imported.number) in client.get(url(imported, "test-equipment"), **HX).content.decode()
    cancelled = open_work(pump, start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert v.CANCELLED.format(number=cancelled.number) in client.get(url(cancelled, "test-equipment"), **HX).content.decode()


def test_change_is_only_a_modal_and_another_facilitys_number_is_a_404(client, signed_in, pump, tech, today, other_tenant, make_user):
    wo = pm_with(pump, in_date("TE-01"), tech=tech, completed_on=today)
    signed_in("technician")
    r = client.get(url(wo, "test-equipment"))
    assert r.status_code == 302 and r["Location"] == f"/work-orders/{wo.number}/"
    assert client.get("/work-orders/WO-00-9999/test-equipment/", **HX).status_code == 404
    from apps.accounts.models import create_default_roles
    create_default_roles(other_tenant)
    client.force_login(make_user("technician", tenant_=other_tenant, username="t@other.example"))
    assert client.get(url(wo, "test-equipment"), **HX).status_code == 404


def test_change_with_junk_is_refused_never_a_500(client, signed_in, pump, tech, today):
    wo = pm_with(pump, in_date("TE-01"), tech=tech, completed_on=today)
    signed_in("technician")
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1", "test_equipment": ["nope", "' or 1=1"]}, **HX)
    assert r.status_code == 200 and te_services.CHOOSE in r.content.decode() and uses_of(wo) == ["TE-01"]
    r = client.post(url(wo, "test-equipment"), {"test_equipment": ["x"]}, **HX)  # no marker: nothing changes
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == ["TE-01"]
    r = client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1", "test_equipment": ["", " "]}, **HX)  # blanks: none ticked
    assert r["HX-Retarget"] == "#drawer" and uses_of(wo) == []


# --- the print ---------------------------------------------------------------------------------------------------------------

def test_the_print_has_lines_while_open_and_the_record_once_done(client, signed_in, pump, tech, today):
    a, late = in_date("TE-01"), past_due("TE-02")
    open_one = open_work(pump, tech=tech)
    signed_in("technician")
    body = client.get(f"/print/work-orders/{open_one.number}/").content.decode()
    assert "<b>Test equipment used</b>" in body and '<div class="ruled te-rules"><div class="rule"></div><div class="rule"></div></div>' in body
    done = pm_with(pump, a, late, tech=tech, completed_on=today - DAY)
    body = client.get(f"/print/work-orders/{done.number}/").content.decode()
    assert "<b>Test equipment:</b>" in body and "te-rules" not in body
    for j in te_services.judged_for(done):
        assert f"<li>{j.words}</li>" in body.replace("&#x27;", "'")
    none = pm_with(pump, tech=tech, completed_on=today)
    assert "<b>Test equipment:</b> none recorded" in client.get(f"/print/work-orders/{none.number}/").content.decode()


def test_the_print_never_shows_it_to_a_scoped_reader(client, signed_in, pump):
    in_date("TE-01")
    wo = open_work(pump, WoType.REPAIR, vendor=VENDOR)
    signed_in("manager")
    assert "Test equipment used" in client.get(f"/print/work-orders/{wo.number}/").content.decode()
    signed_in("vendor", company=VENDOR)
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "Test equipment" not in body


def test_the_print_runs_the_same_queries_for_one_use_or_many(client, signed_in, pump, tech, today):
    one = pm_with(pump, in_date("TE-01"), tech=tech, completed_on=today)
    many = pm_with(pump, in_date("TE-02"), past_due("TE-03"), no_record("TE-04"), tech=tech, completed_on=today)
    signed_in("technician")
    client.get(f"/print/work-orders/{one.number}/")
    with CaptureQueriesContext(connection) as a:
        client.get(f"/print/work-orders/{one.number}/")
    with CaptureQueriesContext(connection) as b:
        client.get(f"/print/work-orders/{many.number}/")
    assert len(a) == len(b)


# --- the modal's queries, History ---------------------------------------------------------------------------------------------

def test_the_modal_runs_the_same_queries_for_few_items_or_many(client, signed_in, pump, tech):
    wo = open_work(pump, tech=tech)
    in_date("TE-01", held_by=tech)
    signed_in("technician")
    client.get(url(wo), **HX)
    with CaptureQueriesContext(connection) as few:
        client.get(url(wo), **HX)
    for n in range(2, 12):
        in_date(f"TE-{n:02d}", held_by=tech if n % 2 else None, kind=Kind.MULTIMETER if n % 3 else Kind.ELECTRICAL_SAFETY)
    with CaptureQueriesContext(connection) as many:
        client.get(url(wo), **HX)
    assert len(few) == len(many)


def test_the_work_orders_history_names_the_test_equipment_added_and_removed(client, signed_in, pump, tech, today, ctx):
    a = in_date("TE-01")
    wo = pm_with(pump, a, tech=tech, completed_on=today)
    signed_in("manager")
    client.post(url(wo, "test-equipment"), {"shown_test_equipment": "1"}, **HX)
    body = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST).content.decode()
    assert '<span class="t">Test equipment TE-01 removed</span><span class="rs">Taken off after completion</span>' in body
    assert '<span class="t">Test equipment TE-01 added</span><span class="rs">Recorded with the completion</span>' in body
    clerk = Role.objects.create(name="Work order clerk", slug="wo-clerk")
    clerk.set_levels({Module.WORKORDERS: Level.VIEW})
    client.force_login(User.objects.create_user(username="clerk@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=clerk))
    body = client.get(f"/work-orders/{wo.number}/?history=1", **HX_HIST).content.decode()
    assert "Test equipment TE-01" not in body and "hist-e" in body  # the uses' area needs Equipment View too


def test_a_safety_check_and_a_repair_record_through_the_fixtures_and_read_alike(client, signed_in, pump, tech, today):
    a = in_date("TE-01")
    signed_in("technician")
    for wo in (safety_with(pump, a, tech=tech, completed_on=today), repair_with(pump, a, tech=tech, completed_on=today)):
        assert "TE-01 (Fluke Biomedical ESA612)</a>: calibration due" in row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())


# --- PostgreSQL: under the row-level security policies -------------------------------------------------------------------------

@needs_postgres
def test_the_work_order_side_under_row_level_security(client, signed_in, pump, tech, today):
    a, b = in_date("TE-01"), in_date("TE-02")
    wo = open_work(pump, tech=tech)
    done = pm_with(pump, a, tech=tech, completed_on=today - DAY)
    signed_in("technician")
    as_app_role()
    body = client.get(url(wo), **HX).content.decode()
    assert {pk for pk, *_ in boxes(body)} == {str(a.pk), str(b.pk)}
    assert client.post(url(wo), pm_post(b), **HX)["HX-Retarget"] == "#drawer"
    assert "TE-01 (Fluke Biomedical ESA612)" in row(client.get(f"/work-orders/{done.number}/", **HX).content.decode())
    r = client.post(url(done, "test-equipment"), {"shown_test_equipment": "1", "test_equipment": [str(a.pk), str(b.pk)]}, **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert "<b>Test equipment:</b>" in client.get(f"/print/work-orders/{done.number}/").content.decode()
    history = client.get(f"/work-orders/{done.number}/?history=1", **HX_HIST).content.decode()
    assert "Test equipment TE-02 added" in history
    with tenant_context(pump.tenant):
        assert uses_of(wo) == ["TE-02"] and uses_of(done) == ["TE-01", "TE-02"]
