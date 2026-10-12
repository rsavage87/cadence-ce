"""
Slice 32, wave 2 A: the Program evaluation screens and the committee's print (apps/web/views_evaluations.py, forms_evaluations.py,
web/evaluations.html, web/_evaluation_*.html, web/print_evaluation.html). The list (newest period first, the next due, New evaluation
for Edit) and the Reports page's way in; New evaluation (the month, the shortcuts, refusals in words with what was typed kept, the areas
the figures need); the page whole and each part on its own; the figures cut to the reader on the page and the print (Incidents View,
Users View, Contracts View, and no Reports View: no figures at all); the text saved one block at a time; the objectives; each status's
actions (send to committee, back to draft, discard, record, correct, and remove approvals, amend) at its level (View reads, Edit
writes, Approve approves, a scoped user is refused whatever their levels); the print's stamps, its partial copy, and its signature
lines; History; junk input never a 500; the role matrix shows the module. The services' own rules are tests/test_evaluations.py.
"""
import json
import re
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from evaluation_fixtures import approved, draft, last_year, sent, year_before
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role
from apps.evaluations import services
from apps.evaluations.models import (
    OBJECTIVES_MAX,
    Body,
    EvaluationApproval,
    EvaluationObjective,
    ObjectiveKind,
    ObjectiveResult,
    ProgramEvaluation,
    Status,
)
from apps.pm.dates import add_months
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
EVERY_VIEW = {m: Level.VIEW for m in Module.values}


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


def day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    return last_year(today)


@pytest.fixture
def users(make_user):
    return {slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst", "requester", "vendor")}


@pytest.fixture
def sign_in(client, users):
    def _as(who):
        user = users[who] if isinstance(who, str) else who
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def custom(make_user):
    """An account whose custom role holds only `levels` (every other module None)."""
    def _make(slug, levels):
        role = Role.objects.create(name=slug.title(), slug=slug)
        role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
        user = make_user("analyst", username=f"{slug}@riverside.example")
        user.role = Role.objects.get(pk=role.pk)
        user.save()
        return user

    return _make


@pytest.fixture
def ev(span, today):
    """A draft of the 12 months ending at the end of last month."""
    return draft(start=span[0], today=today)


def url(e, *rest) -> str:
    base = f"/reports/evaluations/{e.number}/v{e.version}/"
    return base + "".join(f"{r}/" for r in rest)


def page(client, e, **extra):
    return client.get(url(e), **extra)


# --- the list and the way in ----------------------------------------------------------------------------------------------------

def test_the_list_newest_first_with_the_next_due(client, sign_in, span, today):
    before = approved(start=year_before(span[0]), today=today, meeting_on=today, body=Body.EOC)
    services.record_approval(before, body=Body.QUALITY, meeting_on=today, by=None, today=today)
    current = draft(start=span[0], today=today)
    sign_in("analyst")
    r = client.get("/reports/evaluations/")
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/evaluations.html", "web/base.html"]
    assert r.context["nav_active"] == "reports"
    body = html(r)
    table = body[body.index('<table class="ev-tbl">'):]
    assert table.index(current.label) < table.index(before.label)  # newest period first
    row = table[table.index(f">{before.label}</a>"):]
    row = row[:row.index("</tr>")]
    assert services.period_label(before.period_start, before.period_end) in row and '<span class="chip ok">Approved</span>' in row
    assert day(today) in row and "Environment of care or safety committee; Quality oversight (QAPI)" in row
    due = services.period_end_of(before.period_end + timedelta(days=1)) + timedelta(days=90)  # the next 12 months' own end
    assert f"is due for approval by {day(due)}: 90 days after those months end" in body and "Overdue" not in body
    assert "New evaluation" not in body  # the analyst has View
    sign_in("manager")
    assert 'hx-get="/reports/evaluations/new/"' in html(client.get("/reports/evaluations/"))


def test_the_list_says_nothing_is_due_before_a_first_approval_and_counts_its_queries(client, sign_in, span, today):
    sign_in("director")
    assert "No evaluation is approved yet" in html(client.get("/reports/evaluations/"))
    draft(start=span[0], today=today)
    with CaptureQueriesContext(connection) as one:
        client.get("/reports/evaluations/")
    for n in (2, 3):
        e = draft(start=add_months(span[0], -12 * n), today=today)
        services.discard(e, by=None)
    with CaptureQueriesContext(connection) as three:
        assert client.get("/reports/evaluations/").status_code == 200
    assert len(one) == len(three)


def test_the_reports_page_offers_it_to_program_evaluation_view_only(client, sign_in):
    for who, shown in (("director", True), ("analyst", True), ("technician", False)):
        sign_in(who)
        assert ('href="/reports/evaluations/"' in html(client.get("/reports/"))) is shown, who
    assert client.get("/reports/evaluations/").status_code == 403  # the technician: Reports View, no Program evaluation


# --- who may do what ------------------------------------------------------------------------------------------------------------

def test_each_action_at_its_level(client, sign_in, ev, custom, today):
    results = {}
    obj = services.add_objective(ev, kind=ObjectiveKind.REVIEW, text="Cut PM backlog", by=None)
    gets = {"page": url(ev), "print": f"/print/evaluations/{ev.number}/v{ev.version}/", "new": "/reports/evaluations/new/",
            "send": url(ev, "send"), "discard": url(ev, "discard"), "text": url(ev, "text", "summary") + "?edit=1",
            "objective": url(ev, "objectives", obj.pk), "objective_new": url(ev, "objectives", "new") + "?kind=next",
            "approve": url(ev, "approvals", "new"), "amend": url(ev, "amend")}
    editor = custom("editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT})
    for who in ("analyst", editor, "manager", "technician", "requester", "vendor"):
        sign_in(who)
        results[getattr(who, "username", who)] = {k: client.get(v, **HX).status_code for k, v in gets.items()}
    assert results["analyst"] == {**{k: 403 for k in gets}, "page": 200, "print": 200}  # View
    assert results["editor@riverside.example"] == {**{k: 200 for k in gets}, "approve": 403, "amend": 403}  # Edit, not Approve
    assert results["manager"] == {k: 200 for k in gets}  # Approve
    for who in ("technician", "requester", "vendor"):
        assert set(results[who].values()) == {403}, who
    sign_in("analyst")  # posts refused at the door, nothing changed
    for path in (url(ev, "refresh"), url(ev, "send"), url(ev, "back"), url(ev, "discard"), url(ev, "text", "summary"), url(ev, "amend"),
                 url(ev, "approvals", "new"), url(ev, "objectives", obj.pk, "remove")):
        assert client.post(path, {"summary": "x"}, **HX).status_code == 403, path
    ev.refresh_from_db()
    assert ev.status == Status.DRAFT and ev.summary == "" and EvaluationObjective.objects.filter(pk=obj.pk).exists()


def test_a_scoped_user_is_refused_whatever_their_levels(client, sign_in, ev, users):
    vendor = Role.objects.get(slug="vendor")
    vendor.set_levels({m: Level.FULL for m in Module.values})
    for path in ("/reports/evaluations/", url(ev), f"/print/evaluations/{ev.number}/", url(ev, "approvals", "new"), "/reports/evaluations/new/"):
        sign_in("vendor")
        assert client.get(path, **HX).status_code == 403, path


# --- New evaluation -------------------------------------------------------------------------------------------------------------

def test_new_evaluation_offers_the_last_twelve_months_and_last_calendar_year(client, sign_in, today, span):
    sign_in("manager")
    r = client.get("/reports/evaluations/new/", **HX)
    body = html(r)
    assert r.status_code == 200 and [t.name for t in r.templates] == ["web/_evaluation_new.html"]
    assert f'name="start" value="{span[0]:%Y-%m}"' in body
    assert f"Covers {services.period_label(*span)}." in body
    assert f"Calendar year {today.year - 1}" in body and f'?start={today.year - 1}-01' in body
    r = client.get(f"/reports/evaluations/new/?start={today.year - 1}-01", **HX)
    assert f"Covers Jan 1, {today.year - 1} to Dec 31, {today.year - 1}." in html(r)
    full = client.get("/reports/evaluations/new/")  # opened directly: the list page with the form on it
    assert [t.name for t in full.templates[:2]] == ["web/evaluations.html", "web/base.html"] and 'action="/reports/evaluations/new/"' in html(full)


def test_the_shortcuts_are_offered_once_each():
    from apps.web.views_evaluations import presets

    october = presets(date(2026, 10, 12), None, date(2025, 10, 1))
    assert [p["label"] for p in october] == ["12 months ending Sep 2026", "Calendar year 2025"]
    after = presets(date(2026, 10, 12), date(2026, 1, 1), date(2026, 1, 1))  # after an evaluation of calendar 2025
    assert [p["label"] for p in after] == ["After the latest evaluation", "12 months ending Sep 2026", "Calendar year 2025"] and after[0]["active"]
    january = presets(date(2027, 1, 15), None, date(2026, 1, 1))  # the last 12 months are last calendar year
    assert [(p["label"], p["start"]) for p in january] == [("12 months ending Dec 2026", "2026-01")]


def test_new_evaluation_drafts_and_opens_it(client, sign_in, span):
    sign_in("manager")
    r = client.post("/reports/evaluations/new/", {"start": f"{span[0]:%Y-%m}"}, **HX)
    created = ProgramEvaluation.objects.get()
    assert r.status_code == 200 and r["HX-Redirect"] == f"/reports/evaluations/{created.number}/v1/"
    assert created.period_start == span[0] and created.status == Status.DRAFT and created.snapshot_id
    r = client.get(r["HX-Redirect"])
    assert r.status_code == 200 and created.label in html(r)
    # the default after it: the 12 months after the latest evaluation
    after = html(client.get("/reports/evaluations/new/", **HX))
    nxt = span[1] + timedelta(days=1)
    assert f'name="start" value="{nxt:%Y-%m}"' in after and "Those months end on" in after  # not ended yet: said before saving


def test_new_evaluation_without_scripts(client, sign_in, span):
    sign_in("director")
    r = client.post("/reports/evaluations/new/", {"start": f"{span[0]:%Y-%m}"})
    created = ProgramEvaluation.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/reports/evaluations/{created.number}/v1/"
    bad = client.post("/reports/evaluations/new/", {"start": "soon"})
    assert bad.status_code == 200 and "Choose the month the period starts." in html(bad) and 'value="soon"' in html(bad)


@pytest.mark.parametrize("typed, words", [
    ("abc", "Choose the month the period starts."),
    ("", "Choose the month the period starts."),
    ("2025-13", "Choose the month the period starts."),
    ("9999-01", "have not ended: an evaluation covers 12 months that have ended"),
    ("1999-01", "Choose a month from 2000 on."),
    ("2025-01-15", "An evaluation&#x27;s period starts on the first of a month."),
])
def test_new_evaluation_refusals_keep_what_was_typed(client, sign_in, ctx, typed, words):
    sign_in("manager")
    r = client.post("/reports/evaluations/new/", {"start": typed}, **HX)
    body = html(r)
    assert r.status_code == 200 and words in body and f'name="start" value="{typed}"' in body
    assert ProgramEvaluation.objects.count() == 0


def test_new_evaluation_refuses_this_months_period_and_an_overlap(client, sign_in, today, span):
    sign_in("manager")
    r = client.post("/reports/evaluations/new/", {"start": f"{today:%Y-%m}"}, **HX)
    assert "an evaluation covers 12 months that have ended. Choose an earlier month." in html(r)
    first = draft(start=span[0], today=today)
    later = add_months(span[0], 3)
    r = client.post("/reports/evaluations/new/", {"start": f"{add_months(span[0], -3):%Y-%m}"}, **HX)
    body = html(r)
    assert f"{first.label} (draft) covers" in body and "periods never overlap" in body
    assert f'value="{add_months(span[0], -3):%Y-%m}"' in body and later  # kept
    assert ProgramEvaluation.objects.count() == 1


def test_new_evaluation_says_why_someone_cannot_take_the_figures(client, sign_in, custom, span):
    writer = custom("writer", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.USERS: Level.NONE})
    sign_in(writer)
    body = html(client.get("/reports/evaluations/new/", **HX))
    assert "Taking the figures needs every area they cover: Users and access View" in body and "Draft it" not in body
    r = client.post("/reports/evaluations/new/", {"start": f"{span[0]:%Y-%m}"}, **HX)
    assert "Users and access View, which your role does not have" in html(r) and ProgramEvaluation.objects.count() == 0


# --- the page -------------------------------------------------------------------------------------------------------------------

def test_the_page_whole_and_each_part(client, sign_in, ev):
    sign_in("director")
    r = page(client, ev)
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/evaluations.html", "web/base.html"]
    assert client.get(f"/reports/evaluations/{ev.number}/").context["ev"].pk == ev.pk  # the number alone: services.current
    for target, template in (("ev-body", "web/_evaluation_body.html"), ("ev-head", "web/_evaluation_head.html"),
                             ("ev-figures", "web/_evaluation_figures.html"), ("ev-text", "web/_evaluation_text.html"),
                             ("ev-objectives", "web/_evaluation_objectives.html"), ("ev-approvals", "web/_evaluation_approvals.html")):
        r = page(client, ev, **part(target))
        assert r.status_code == 200 and r.templates[0].name == template, target
        assert f'id="{target}"' in html(r)
        assert "<html" not in html(r)


def test_the_page_shows_the_head_the_figures_and_the_rest(client, sign_in, ev, span, today):
    sign_in("director")
    body = html(page(client, ev))
    assert f"<h1>{ev.label} <span class=\"chip neutral\">Draft</span></h1>" in body
    assert f"{services.period_label(*span)} · Draft · figures taken {day(today)}" in body
    # the figures by group, each with its target, this period, the previous period, and the result
    for group in ("Scope of the program", "Scheduled maintenance", "Incoming inspections", "Repairs", "AEM program", "Recalls",
                  "Device incidents", "Staff and vendors", "Cost"):
        assert f'<tr class="ev-grp-h"><th colspan="5" scope="rowgroup">{group}</th></tr>' in body, group
    assert "PMs on time, life support and high risk" in body and "PMs on time by quarter" in body
    prev_start = year_before(span[0])
    assert f"The 12 months before<small>{services.period_label(prev_start, span[0] - timedelta(days=1))}</small>" in body
    assert f"counted {day(today)}, not approved" in body
    assert "Not recorded in Cadence" in body and "Use errors and no problem found" in body
    assert "The survey binder's items for the period" in body and f"/reports/survey/?from={span[0].isoformat()}&amp;to={span[1].isoformat()}" in body
    assert "Left out for your role" not in body
    # the actions a director has on a draft
    assert f'hx-post="{url(ev, "refresh")}"' in body and f'hx-get="{url(ev, "send")}"' in body and f'hx-get="{url(ev, "discard")}"' in body
    assert "Record approval" not in body and "Back to draft" not in body and f'hx-get="{url(ev, "amend")}"' not in body
    assert f'href="/print/evaluations/{ev.number}/v1/?facility=riverside" target="_blank"' in body
    # the text, each block with its own Write, and the warning; the objectives; History
    assert "About the program as a whole: no patient, requester, or incident details" in body
    for key in ("summary", "cybersecurity", "inventory", "plan"):
        assert f'hx-get="{url(ev, "text", key)}?edit=1"' in body, key
    assert "Add an objective for this period" in body and "Add an objective for the next period" in body
    assert f'hx-get="{url(ev)}?history=1"' in body


def test_a_view_reader_reads_without_controls(client, sign_in, ev):
    sign_in("analyst")
    body = html(page(client, ev))
    assert ev.label in body and "Write</button>" not in body and "Refresh figures" not in body and "Add an objective" not in body
    assert "Send to committee" not in body and "Discard" not in body


@pytest.mark.parametrize("path", ["/reports/evaluations/EV-99-0001/", "/reports/evaluations/EV-26-0001/v0/", "/reports/evaluations/EV-26-0001/v99999/",
                                  "/reports/evaluations/EV%20X/", "/print/evaluations/nope/v2/"])
def test_an_unknown_evaluation_is_a_404(client, sign_in, ev, path):
    sign_in("director")
    assert client.get(path).status_code == 404


# --- the cut --------------------------------------------------------------------------------------------------------------------

def _print(client, e):
    return html(client.get(f"/print/evaluations/{e.number}/v{e.version}/"))


@pytest.mark.parametrize("missing, gone, kept, words", [
    (Module.INCIDENTS, ["Incidents recorded", "Reports sent on time", "Decisions pending"], "Work completed without a credential for the device",
     "Device incidents (needs Incidents View)"),
    (Module.USERS, ["Work completed without a credential for the device"], "Vendor jobs with nothing on file",
     "Staff and vendors: Work completed without a credential for the device (needs Users and access View)"),
    (Module.CONTRACTS, ["Repair labor and parts", "Vendor jobs with nothing on file"], "Incidents recorded", "Cost (needs Contracts View)"),
])
def test_the_page_and_the_print_show_only_what_the_reader_may_see(client, sign_in, custom, ev, missing, gone, kept, words):
    reader = custom("reader", {**EVERY_VIEW, Module.EVALUATIONS: Level.VIEW, missing: Level.NONE})
    sign_in(reader)
    for body, row in ((html(page(client, ev)), '<span class="ev-ind">{}</span>'), (_print(client, ev), '<td class="ind">{}')):
        for label in gone:
            assert row.format(label) not in body, label
        assert row.format(kept) in body and words in body
    assert "Left out for your role" in html(page(client, ev))
    printed = _print(client, ev)
    assert "Partial copy: not the approved document" in printed and f"Printed by {reader}" in printed
    sign_in("director")
    assert "Partial copy" not in _print(client, ev) and "Left out" not in html(page(client, ev))


def test_without_reports_view_no_figures_at_all(client, sign_in, custom, ev, today):
    services.update_text(ev, "summary", "The program met most of its targets.", by=None)
    services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Close the PM gap on imaging", by=None)
    reader = custom("committee", {**EVERY_VIEW, Module.EVALUATIONS: Level.APPROVE, Module.REPORTS: Level.NONE})
    sign_in(reader)
    for body in (html(page(client, ev)), _print(client, ev)):
        assert "Every figure (needs Reports View)" in body
        for label in ("PMs on time", "Devices in use", "Not recorded in Cadence", "The survey binder", "counted", "Provisional"):
            assert label not in body, label
        assert "The program met most of its targets." in body and "Close the PM gap on imaging" in body
    assert "Partial copy: not the approved document" in _print(client, ev)
    assert "Refresh figures" not in html(page(client, ev))  # Approve, but the figures are not theirs to take


@pytest.fixture
def busy(span, today, vent):
    """A draft whose period has a life-support PM done on time and a repair completed in 3 days: two figures judged against a target."""
    s = span[0]
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=s + timedelta(days=10),
                           due_on=s + timedelta(days=30), source=Source.PM_PLANNER)
    WorkOrder.objects.filter(pk=pm.pk).update(status=WoStatus.COMPLETED, completed_on=s + timedelta(days=30))
    fix = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", opened_on=s + timedelta(days=40))
    WorkOrder.objects.filter(pk=fix.pk).update(status=WoStatus.COMPLETED, completed_on=s + timedelta(days=43))
    return draft(start=s, today=today)


def test_the_tally_counts_only_what_is_shown(client, sign_in, custom, busy):
    from apps.evaluations import indicators

    full = indicators.tally(busy.snapshot.data["indicators"])
    assert full[1] == 2
    sign_in("director")
    assert f"<b>{full[0]} of {full[1]}</b> of the facility's targets met</p>" in html(page(client, busy))
    reader = custom("reader", {**EVERY_VIEW, Module.EVALUATIONS: Level.VIEW, Module.PM: Level.NONE})
    seen = indicators.tally(indicators.cut(busy.snapshot.data, reader)["indicators"])
    assert seen[1] == 1  # the PM figure has a target: the reader's tally leaves it out
    sign_in(reader)
    body = html(page(client, busy))
    assert f"<b>{seen[0]} of {seen[1]}</b> of the facility's targets met, of those shown to you" in body
    assert "PMs on time by quarter" not in body and "AEM effectiveness by model (needs PM schedule View)" in body


def test_the_figures_read_as_stored(client, sign_in, busy, span):
    """Each indicator with its target, this period (with its "n of m"), and Met; the PM quarters."""
    sign_in("director")
    body = html(page(client, busy))
    row = body[body.index('<span class="ev-ind">PMs on time, life support and high risk</span>'):]
    row = row[:row.index("</tr>")]
    assert '<td class="num" data-label="Target">100%</td>' in row and '<td class="num" data-label="This period"><b>100%</b><small>1 of 1</small>' in row
    assert '<td data-label="Result"><span class="chip ok">Met</span>' in row
    quarters = body[body.index("PMs on time by quarter"):]
    assert f"{span[0]:%b} to " in quarters and "None due" in quarters


# --- the figures ----------------------------------------------------------------------------------------------------------------

def test_refresh_figures_takes_them_again(client, sign_in, ev):
    sign_in("manager")
    first = ev.snapshot_id
    r = client.post(url(ev, "refresh"), **HX)
    ev.refresh_from_db()
    assert r.status_code == 200 and r.templates[0].name == "web/_evaluation_body.html" and r["HX-Retarget"] == "#ev-body"
    assert r["HX-Reswap"] == "outerHTML" and ev.snapshot_id != first and toast_of(r) == f"{ev.label}: figures taken again"


def test_refresh_is_refused_in_words_to_someone_who_cannot_see_every_area(client, sign_in, custom, ev):
    writer = custom("writer", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT, Module.INCIDENTS: Level.NONE})
    sign_in(writer)
    body = html(page(client, ev))
    assert "Refresh figures</button>" not in body and "Taking the figures needs every area they cover: Incidents View" in body
    first = ev.snapshot_id
    r = client.post(url(ev, "refresh"), **HX)
    ev.refresh_from_db()
    assert r.status_code == 200 and ev.snapshot_id == first
    assert "Incidents View, which your role does not have" in html(r) and "Incidents View" in toast_of(r)
    assert client.post(url(ev, "refresh")).status_code == 400  # without scripts: the page with the words


def test_refresh_of_one_sent_to_committee_is_refused(client, sign_in, ev, today):
    sent(ev=ev, today=today)
    sign_in("manager")
    r = client.post(url(ev, "refresh"), **HX)
    assert "is sent to committee: only a draft&#x27;s figures are taken again. Take it back to draft first." in html(r)


# --- the text -------------------------------------------------------------------------------------------------------------------

def test_a_text_block_is_written_and_saved_on_its_own(client, sign_in, ev):
    services.update_text(ev, "went_well", "Recalls closed fast.", by=None)
    sign_in("manager")
    r = client.get(url(ev, "text", "summary") + "?edit=1", **HX)
    assert r.templates[0].name == "web/_evaluation_field.html" and "<textarea" in html(r) and "no patient, requester" in html(r)
    r = client.post(url(ev, "text", "summary"), {"summary": "  A good year.\nPMs on time rose.  "}, **HX)
    ev.refresh_from_db()
    assert ev.summary == "A good year.\nPMs on time rose." and ev.went_well == "Recalls closed fast."
    assert "A good year.<br>PMs on time rose." in html(r) and "<textarea" not in html(r) and toast_of(r) == "Summary saved"
    assert client.get(url(ev, "text", "summary"), **HX).status_code == 200  # Cancel: the block to read
    assert client.get(url(ev, "text", "summary")).status_code == 302  # opened directly: the page


def test_a_refused_text_keeps_what_was_typed(client, sign_in, ev, today):
    sign_in("manager")
    long = "x" * 2001
    r = client.post(url(ev, "text", "summary"), {"summary": long}, **HX)
    assert "At most 2,000 characters (2,001 entered)." in html(r) and long in html(r)
    r = client.post(url(ev, "text", "plan"), {"plan_title": "Medical equipment plan", "plan_version": "v 4", "plan_effective_on": "2026-01-01"}, **HX)
    body = html(r)
    assert "without spaces" in body and 'value="Medical equipment plan"' in body and 'value="v 4"' in body
    ev.refresh_from_db()
    assert ev.plan_title == "" and ev.plan_effective_on is None  # all of the block or none of it
    r = client.post(url(ev, "text", "inventory"), {"inventory_reviewed_on": "soon"}, **HX)
    assert "Enter a date as YYYY-MM-DD." in html(r) and 'value="soon"' in html(r)
    r = client.post(url(ev, "text", "inventory"), {"inventory_reviewed_on": (today + timedelta(days=1)).isoformat()}, **HX)
    assert "cannot be dated after today" in html(r)
    r = client.post(url(ev, "text", "plan"), {"plan_title": "MEMP", "plan_version": "v4", "plan_effective_on": "2026-01-01"}, **HX)
    ev.refresh_from_db()
    assert (ev.plan_title, ev.plan_version, ev.plan_effective_on) == ("MEMP", "v4", date(2026, 1, 1))
    assert "MEMP, version v4, effective Jan 1, 2026" in html(r)
    assert client.post(url(ev, "text", "bogus"), {"bogus": "x"}, **HX).status_code == 404


def test_the_text_of_one_sent_to_committee_is_frozen(client, sign_in, ev, today):
    sent(ev=ev, today=today)
    sign_in("manager")
    assert "Write</button>" not in html(page(client, ev))
    r = client.post(url(ev, "text", "summary"), {"summary": "Late change"}, **HX)
    assert "is sent to committee: only a draft&#x27;s text is written. Take it back to draft first." in html(r)
    assert "Late change" in html(r)  # kept, to copy
    ev.refresh_from_db()
    assert ev.summary == ""


# --- objectives -----------------------------------------------------------------------------------------------------------------

def test_objectives_are_added_changed_moved_and_removed(client, sign_in, ev):
    sign_in("manager")
    r = client.get(url(ev, "objectives", "new") + "?kind=review", **HX)
    assert "Result" in html(r) and "Comment" in html(r)
    assert "Result" not in html(client.get(url(ev, "objectives", "new") + "?kind=next", **HX))
    r = client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "PMs on time at 100%", "target": "100%", "result": "partly",
                                                                     "comment": "Two late in Q3"}, **HX)
    assert r["HX-Retarget"] == "#ev-objectives" and r["HX-Reswap"] == "outerHTML" and "modal-close" in triggers(r)
    assert '<span class="chip warn">Partly met</span>' in html(r) and toast_of(r) == "Objective added"
    first = EvaluationObjective.objects.get()
    assert (first.result, first.comment) == (ObjectiveResult.PARTLY, "Two late in Q3")
    client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "Train two BMETs on imaging"}, **HX)
    second = EvaluationObjective.objects.get(text="Train two BMETs on imaging")
    r = client.post(url(ev, "objectives", second.pk, "move"), {"up": "1"}, **HX)
    assert html(r).index("Train two BMETs") < html(r).index("PMs on time at 100%")
    r = client.post(url(ev, "objectives", first.pk), {"text": "PMs on time at 100%", "target": "100%", "result": "met", "comment": ""}, **HX)
    first.refresh_from_db()
    assert first.result == ObjectiveResult.MET and toast_of(r) == "Objective saved"
    r = client.post(url(ev, "objectives", second.pk, "remove"), **HX)
    assert "Train two BMETs" not in html(r) and not EvaluationObjective.objects.filter(pk=second.pk).exists()


def test_objective_refusals_keep_what_was_typed(client, sign_in, ev, today):
    sign_in("manager")
    r = client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "  ", "target": "Fewer late PMs"}, **HX)
    assert "Enter the objective." in html(r) and 'value="Fewer late PMs"' in html(r) and EvaluationObjective.objects.count() == 0
    r = client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "y" * 301}, **HX)
    assert "At most 300 characters (301 entered)." in html(r)
    r = client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "Something", "result": "bogus"}, **HX)
    assert "Choose met, partly met, not met, or carried forward." in html(r) and EvaluationObjective.objects.count() == 0
    for n in range(OBJECTIVES_MAX):
        services.add_objective(ev, kind=ObjectiveKind.NEXT, text=f"Next {n}", by=None)
    r = client.post(url(ev, "objectives", "new") + "?kind=next", {"text": "One too many"}, **HX)
    assert f"at most {OBJECTIVES_MAX} of next period" in html(r) and 'One too many' in html(r)
    assert client.get(url(ev, "objectives", "new") + "?kind=bogus", **HX).status_code == 404
    sent(ev=ev, today=today)
    obj = EvaluationObjective.objects.filter(kind=ObjectiveKind.NEXT).first()
    r = client.post(url(ev, "objectives", obj.pk, "remove"), **HX)
    assert r["HX-Retarget"] == "#ev-objectives" and "only a draft&#x27;s objectives are changed" in html(r)
    assert EvaluationObjective.objects.filter(pk=obj.pk).exists()


def test_an_objective_of_another_evaluation_is_a_404(client, sign_in, ev, span, today):
    other = draft(start=year_before(span[0]), today=today)
    obj = services.add_objective(other, kind=ObjectiveKind.NEXT, text="Elsewhere", by=None)
    sign_in("manager")
    assert client.get(url(ev, "objectives", obj.pk), **HX).status_code == 404
    assert client.post(url(ev, "objectives", obj.pk, "remove"), **HX).status_code == 404


# --- statuses and approvals -----------------------------------------------------------------------------------------------------

def test_send_to_committee_back_to_draft_and_send_again(client, sign_in, ev, today):
    obj = services.add_objective(ev, kind=ObjectiveKind.REVIEW, text="Cut PM backlog", by=None)
    sign_in("manager")
    r = client.get(url(ev, "send"), **HX)
    assert "frozen while it is with the committee" in html(r) and "Record a result for every objective of this period first: 1 of them has none yet." in html(r)
    assert "Send to committee</button>" not in html(r)  # what keeps it from going is said first, with no button
    r = client.post(url(ev, "send"), **HX)
    assert "Record a result for every objective of this period before sending it to committee" in html(r)
    services.update_objective(obj, result=ObjectiveResult.MET, by=None)
    r = client.post(url(ev, "send"), **HX)
    ev.refresh_from_db()
    assert ev.status == Status.READY and r["HX-Retarget"] == "#ev-body" and "modal-close" in triggers(r)
    body = html(r)
    assert f"Sent to committee {day(today)}" in body and "frozen" in body
    assert f'hx-post="{url(ev, "back")}"' in body and f'hx-get="{url(ev, "approvals", "new")}"' in body and "Send to committee</button>" not in body
    r = client.post(url(ev, "back"), **HX)
    ev.refresh_from_db()
    assert ev.status == Status.DRAFT and toast_of(r) == f"{ev.label} is a draft again"
    r = client.post(url(ev, "back"), **HX)  # a draft already: the words at the top of the page's body
    assert "only one sent to committee goes back to draft" in html(r) and r["HX-Retarget"] == "#ev-body"


def test_discard(client, sign_in, ev, span, today):
    sign_in("manager")
    assert "kept, marked discarded" in html(client.get(url(ev, "discard"), **HX))
    r = client.post(url(ev, "discard"), **HX)
    ev.refresh_from_db()
    assert ev.status == Status.DISCARDED and "Discarded: kept, and counted nowhere." in html(r)
    assert "Send to committee</button>" not in html(r) and "Discard</button>" not in html(r)
    done = approved(start=year_before(span[0]), today=today)
    r = client.post(url(done, "discard"), **HX)
    assert "an approved evaluation is never discarded. Amend it instead." in html(r)


def test_record_approval_refusals_keep_what_was_typed(client, sign_in, ev, today):
    sent(ev=ev, today=today)
    sign_in("manager")
    r = client.get(url(ev, "approvals", "new"), **HX)
    assert f"sent to it on {day(today)}: this approves {ev.label}" in html(r)
    cases = [
        ({"body": "", "meeting_on": today.isoformat()}, "Choose the committee that approved it."),
        ({"body": "bogus", "meeting_on": today.isoformat()}, "Choose the committee that approved it."),
        ({"body": "other", "body_name": "", "meeting_on": today.isoformat()}, "Name the committee."),
        ({"body": "eoc", "meeting_on": "last week"}, "Enter the day the committee met as a date."),
        ({"body": "eoc", "meeting_on": "2026-02-30"}, "Enter the day the committee met as a date."),
        ({"body": "eoc", "meeting_on": (today - timedelta(days=1)).isoformat()}, "the meeting that approved it is on or after that day"),
        ({"body": "eoc", "meeting_on": (today + timedelta(days=1)).isoformat()}, "The meeting cannot be after today"),
        ({"body": "eoc", "meeting_on": today.isoformat(), "minutes_reference": "EOC 2026"}, "without spaces"),
    ]
    for data, words in cases:
        r = client.post(url(ev, "approvals", "new"), {"chair_name": "Dr. Patel", **data}, **HX)
        assert r.status_code == 200 and words in html(r), words
        assert 'value="Dr. Patel"' in html(r)
    ev.refresh_from_db()
    assert ev.status == Status.READY and EvaluationApproval.objects.count() == 0


def test_approvals_recorded_corrected_and_removed(client, sign_in, ev, today):
    sent(ev=ev, today=today)
    sign_in("manager")
    r = client.post(url(ev, "approvals", "new"), {"body": "eoc", "meeting_on": today.isoformat(), "chair_name": "Dr. Patel",
                                                  "chair_title": "EOC chair", "minutes_reference": "EOC-2026-10"}, **HX)
    ev.refresh_from_db()
    assert ev.status == Status.APPROVED and ev.approved_on == today and toast_of(r) == f"{ev.label} approved"
    body = html(r)
    assert f"Approved {day(today)} by Environment of care or safety committee" in body and "Approved it</span>" in body
    assert "chair Dr. Patel, EOC chair · minutes EOC-2026-10 · recorded by" in body
    assert f'hx-get="{url(ev, "amend")}"' in body and "Discard</button>" not in body
    r = client.post(url(ev, "approvals", "new"), {"body": "other", "body_name": "Board quality committee", "meeting_on": today.isoformat()}, **HX)
    assert toast_of(r) == "Approval recorded" and "Board quality committee" in html(r)
    first, later = EvaluationApproval.objects.order_by("created_at")
    r = client.post(url(ev, "approvals", first.pk, "remove"), **HX)
    assert r["HX-Retarget"] == "#ev-approvals" and "so it is never removed" in html(r)
    r = client.get(url(ev, "approvals", first.pk), **HX)
    assert 'value="EOC-2026-10"' in html(r) and "Its history keeps what it said before." in html(r)
    r = client.post(url(ev, "approvals", first.pk), {"body": "eoc", "meeting_on": today.isoformat(), "chair_name": "Dr. A. Patel",
                                                     "minutes_reference": "EOC-2026-10"}, **HX)
    first.refresh_from_db()
    assert first.chair_name == "Dr. A. Patel" and toast_of(r) == "Approval corrected"
    r = client.post(url(ev, "approvals", later.pk, "remove"), **HX)
    assert toast_of(r) == "Approval removed" and not EvaluationApproval.objects.filter(pk=later.pk).exists()
    assert client.post(url(ev, "approvals", "new"), {"body": "eoc", "meeting_on": today.isoformat()}, HTTP_HX_REQUEST="true").status_code == 200
    editor = Role.objects.get(slug="technician")
    editor.set_levels({Module.EVALUATIONS: Level.EDIT})
    sign_in("technician")
    assert client.post(url(ev, "approvals", first.pk), {"body": "eoc", "meeting_on": today.isoformat()}, **HX).status_code == 403


def test_amend_opens_the_amendment_and_supersedes_on_approval(client, sign_in, ev, today):
    sent(ev=ev, today=today)
    services.record_approval(ev, body=Body.EOC, meeting_on=today, by=None, today=today)
    sign_in("manager")
    assert f"A new draft, {ev.number} v2, starts from {ev.label}" in html(client.get(url(ev, "amend"), **HX))
    r = client.post(url(ev, "amend"), **HX)
    v2 = ProgramEvaluation.objects.get(number=ev.number, version=2)
    assert r["HX-Redirect"] == f"/reports/evaluations/{ev.number}/v2/"
    assert f"Amends <a class=\"link\" href=\"{url(ev)}\">{ev.label}</a>, which stays the approved version" in html(client.get(url(v2)))
    old = html(page(client, ev))
    assert f"Being amended: <a class=\"link\" href=\"{url(v2)}\">{v2.label}</a> (draft)" in old and "Amend</button>" not in old
    assert client.get(f"/reports/evaluations/{ev.number}/").context["ev"].pk == ev.pk  # the version of record
    assert "already amends" in html(client.post(url(ev, "amend"), **HX))
    sent(ev=v2, today=today)
    services.record_approval(v2, body=Body.EOC, meeting_on=today, by=None, today=today)
    old = html(page(client, ev))
    assert f"Superseded by <a class=\"link\" href=\"{url(v2)}\">{v2.label}</a>" in old and "Record approval</button>" not in old
    assert f"superseded by {v2.label}" in _print(client, ev)
    assert client.get(f"/reports/evaluations/{ev.number}/").context["ev"].pk == v2.pk


# --- the print ------------------------------------------------------------------------------------------------------------------

def test_the_print_is_stamped_by_status(client, sign_in, ev, today):
    services.update_text(ev, "summary", "The year in short.", by=None)
    services.update_text(ev, "cybersecurity", "Patched every networked pump.", by=None)
    services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Imaging PMs on the OEM schedule", target="100%", by=None)
    sign_in("director")
    r = client.get(f"/print/evaluations/{ev.number}/v1/")
    printed = html(r)
    assert [t.name for t in r.templates[:2]] == ["web/print_evaluation.html", "web/print_base.html"]
    assert '<p class="evp-stamp draft">Draft</p>' in printed and "Partial copy" not in printed and "Chair's signature" not in printed
    assert "Medical equipment program evaluation" in printed and "Riverside Regional" in printed and f"Figures taken</dt><dd>{day(today)}" in printed
    assert "The year in short." in printed and "Patched every networked pump." in printed and "Imaging PMs on the OEM schedule" in printed
    assert printed.count(f"figures taken {day(today)} · Draft") == 3  # every page after the cover
    sent(ev=ev, today=today)
    printed = _print(client, ev)
    assert f'<p class="evp-stamp ready">Sent to committee {day(today)}</p>' in printed and "Chair's signature" in printed
    sign_in("analyst")  # no Users View: a partial copy, never signed
    printed = _print(client, ev)
    assert "Partial copy: not the approved document" in printed and "Chair's signature" not in printed
    assert "Signature lines are printed only on a complete copy" in printed
    sign_in("director")
    services.record_approval(ev, body=Body.QUALITY, meeting_on=today, chair_name="Dr. Lee", by=None, today=today)
    printed = html(client.get(f"/print/evaluations/{ev.number}/"))
    assert f'<p class="evp-stamp approved">Approved {day(today)} by Quality oversight (QAPI)</p>' in printed
    assert "Dr. Lee" in printed and "Chair's signature" not in printed


def test_a_discarded_copy_is_stamped_so(client, sign_in, ev):
    services.discard(ev, by=None)
    sign_in("director")
    assert '<p class="evp-stamp discarded">Discarded</p>' in _print(client, ev)


# --- History, junk, the role matrix ---------------------------------------------------------------------------------------------

def test_history_carries_the_objectives_and_approvals(client, sign_in, ev, today):
    services.add_objective(ev, kind=ObjectiveKind.NEXT, text="Fewer late PMs", by=None)
    services.update_text(ev, "summary", "Short year.", by=None)
    sent(ev=ev, today=today)
    services.record_approval(ev, body=Body.EOC, meeting_on=today, by=None, today=today)
    sign_in("analyst")
    r = client.get(url(ev) + "?history=1", **part("hist"))
    body = html(r)
    assert r.templates[0].name == "web/_history_entries.html"
    assert "Objective added" in body and "Approval added" in body and "Short year." in body and "Sent to committee" in body
    full = client.get(url(ev) + "?history=1")
    assert full.status_code == 200 and "Objective added" in html(full)


@pytest.mark.parametrize("method, path, data", [
    ("post", "objectives/new/?kind=review", {"text": "x", "result": "<script>"}),
    ("post", "objectives/00000000-0000-0000-0000-000000000000/move/", {"up": "sideways"}),
    ("post", "approvals/new/", {"body": "eoc", "meeting_on": "0000-00-00"}),
    ("post", "text/plan/", {"plan_effective_on": "1999-12-31", "plan_version": "v" * 41}),
    ("post", "text/inventory_reviewed_on/", {"inventory_reviewed_on": "x"}),
    ("get", "objectives/new/?kind=", {}),
    ("get", "?older=garbage&history=1", {}),
])
def test_junk_is_never_a_500(client, sign_in, ev, method, path, data):
    sign_in("director")
    r = getattr(client, method)(url(ev) + path, data, **HX)
    assert r.status_code in (200, 400, 404), (path, r.status_code)


def test_the_role_matrix_shows_the_module(client, sign_in):
    sign_in("director")
    body = html(client.get("/users/roles/"))
    assert "Program evaluation" in body and 'aria-label="CE manager, evaluations"' in body


def test_service_refuses_a_start_in_year_9999_in_words(ctx, today):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.create_draft(period_start=date(9999, 1, 1), by=None, today=today)


# --- under PostgreSQL's row-level security -------------------------------------------------------------------------------------

@needs_postgres
def test_the_screens_under_the_policies(client, sign_in, ev, today):
    """The list, the page and its parts, a text block, an objective, sending, recording the approval, and the print, as the runtime
    role inside this facility."""
    sign_in("manager")
    as_app_role()
    assert ev.label in html(client.get("/reports/evaluations/"))
    assert "PMs on time, life support and high risk" in html(page(client, ev))
    assert client.post(url(ev, "text", "summary"), {"summary": "Under the policies."}, **HX).status_code == 200
    r = client.post(url(ev, "objectives", "new") + "?kind=review", {"text": "Kept", "result": "met"}, **HX)
    assert toast_of(r) == "Objective added"
    assert client.post(url(ev, "send"), **HX)["HX-Retarget"] == "#ev-body"
    r = client.post(url(ev, "approvals", "new"), {"body": "eoc", "meeting_on": today.isoformat()}, **HX)
    assert "Approved" in toast_of(r) or toast_of(r).endswith("approved")
    assert "Under the policies." in _print(client, ev) and "Objective added" in html(client.get(url(ev) + "?history=1", **part("hist")))
    assert re.search(r"evp-stamp approved", _print(client, ev))
