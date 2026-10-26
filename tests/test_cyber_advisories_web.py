"""
Slice 40 wave 2A, the web doors of security advisories (apps/web/views_cyber_advisories.py, forms_cyber_advisories.py, their templates,
and slice 38's two update doors opened from an advisory):

- Who: the Advisories tab, an advisory's page, and the list's CSV need Device security View; every device line, response, decision-derived
  count or word, "Decisions for this advisory", and the devices CSV also need Equipment View. Edit records advisories, sources, revisions,
  sets and confirms models, marks one not applicable and reopens it; Close needs Approve and Equipment View; reopening a closed one and
  Entered in error need Approve. Scoped users never.
- The tab: Needs attention (headings, counts, rows, links, Record decision answering with the advisory's page, Open software update) and
  the list with its filters and CSV; the body re-fetches itself (hx-disinherit).
- An advisory's page (drawer and full page): name, status words, CVE IDs and the advisories sharing them, sources, the models it names
  with their counts and device lines (needing a response first, the rest behind Show), responses, the per-line and per-row actions
  only where they can act, "Decisions for this advisory", Checks, History; its devices CSV with a row of each line kind.
- Every modal at each level, refusals said before they are tried (the lock-free readers) and the writers' words after, the CVE overlap's
  note and its two buttons, Set models' "Add a version not on file", and every save answering with the advisory's page (advisory_saved).
- Slice 38's update doors from an advisory: their initials, their answers, and `advisory` and `from` never carried forward.
- Fixed queries as advisories, rows, devices, decisions, and updates grow; rules.DECISION_NEVER_WORDS over every page, modal, CSV, and
  toast (a refusal echoing what was typed allowed for).
"""
import json
import re
from datetime import timedelta
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest
from csvutil import csv_rows
from cyber_fixtures import facts, make_advisory, model_row, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import DataScope, Level, Module, Role, User
from apps.cybersecurity import advisories, decisions, rules
from apps.cybersecurity.models import (
    AcceptedBy,
    AdvisoryModel,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisoryStatus,
    AdvisoryVersions,
    AppliesTo,
    Connects,
    DecisionResponse,
    DeviceSecurityDecision,
    SecurityAdvisory,
    SoftwareRelease,
    SoftwareUpdate,
)
from apps.equipment.models import Asset, Ownership
from apps.web import forms_cyber_advisories as forms_ad
from apps.web import views_cyber_advisories as views_ad
from apps.workorders.models import WorkOrder

HX = {"HTTP_HX_REQUEST": "true"}
MODAL = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "modal-card"}
DAY = timedelta(days=1)
TODAY = timezone.localdate()
SEEN: list = []  # every body, CSV, and toast these tests read, for the never-words test at the end


def html(r) -> str:
    body = unescape(r.content.decode())
    SEEN.append(body)
    return body


def never(text: str, words=rules.DECISION_NEVER_WORDS) -> list:
    return [w for w in words if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", unescape(text), re.IGNORECASE)]


def triggers(r) -> dict:
    out = {}
    for h in ("HX-Trigger", "HX-Trigger-After-Settle"):
        if h in r:
            out.update(json.loads(r[h]))
    return out


def toast_of(r) -> str:
    words = triggers(r).get("toast", {}).get("value", "")
    SEEN.append(words)
    return words


def errors(body: str) -> dict:
    """{field: its error} from an advisory modal (ids "ad-<name>_error"), "" for the one at the top."""
    out = {m.group(1): unescape(m.group(2)) for m in re.finditer(r'<span class="err" id="ad-(\w+)_error">([^<]*)</span>', body)}
    top = re.search(r'<span class="err" role="alert">([^<]*)</span>', body)
    if top:
        out[""] = unescape(top.group(1))
    return out


def blocker_of(body: str) -> str:
    m = re.search(r'<div class="full note" role="alert"[^>]*>([^<]*)</div>', body)
    return unescape(m.group(1)) if m else ""


def saved_page(r, number: str) -> str:
    """A save's answer: the advisory's page into #drawer, the lists told, the modal closed after settle."""
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer", r.content[:400]
    t = triggers(r)
    assert "cyber-changed" in t and "modal-close" in t
    body = html(r)
    assert f"<h2>{number}" in body
    return body


def custom_user(client, tenant, slug, levels, *, scope="", login=True, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    user = User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="Person", **fields)
    if login:
        client.force_login(user)
    return user


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def floor(ctx, dept, pump_model):
    """The pumps can connect (wireless, as disclosed). P-1, P-2 of ours on 5.2 (demo), P-3 on 5.4 (demo), P-4 on 5.6 (demo), P-5 with
    no release recorded; the rental T-1 on 5.2 (demo), due back in 20 days. AD-<yy>-0001, received 3 days ago (two CVE IDs, bulletin
    DEMO-ADV-0001), names the pumps on 5.2 (demo) with the fix 5.6 (demo), models confirmed."""
    release(pump_model, "", connects=Connects.WIRELESS)
    r52, r54, r56 = release(pump_model, "5.2 (demo)"), release(pump_model, "5.4 (demo)"), release(pump_model, "5.6 (demo)")
    d = {}
    for tag, rel in (("P-1", r52), ("P-2", r52), ("P-3", r54), ("P-4", r56), ("P-5", None)):
        d[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=dept, serial=f"SN-{tag}")
        if rel is not None:
            facts(d[tag], rel)
    d["T-1"] = Asset.objects.create(tag="T-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL,
                                    owner="Summit Medical Rentals", due_back_on=TODAY + 20 * DAY, serial="SN-T-1")
    facts(d["T-1"], r52)
    a = make_advisory(received_on=TODAY - 3 * DAY, cves=["CVE-2026-1001", "CVE-2026-1002"], reference="DEMO-ADV-0001",
                      published_on=TODAY - 5 * DAY)
    row = model_row(a, pump_model, releases=[r52], fix=r56)
    return SimpleNamespace(model=pump_model, r52=r52, r54=r54, r56=r56, d=d, dept=dept, a=a, row=row)


def page(client, number, **headers):
    return client.get(f"/security/advisories/{number}/", **(headers or HX))


def decide(adv, **fields) -> DeviceSecurityDecision:
    """An advisory decision through the service, as the system."""
    values = {"applies_to": AppliesTo.ADVISORY, "advisory": adv, "reasons": [], "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS,
              "accepted_by": AcceptedBy.CE_LEADER, "by": None, **fields}
    return decisions.record_decision(**values)


def read_urls(floor) -> list:
    """The reading doors: the tab, its CSV, the page (drawer and full)."""
    n = floor.a.number
    return ["/security/advisories/", "/security/advisories/export.csv", f"/security/advisories/{n}/"]


def modal_urls(floor) -> list:
    """Every modal at Edit (GET): Record advisory, Edit, the sources', Set models (both steps), Remove model, Confirm models, Not
    applicable here, Reopen."""
    n = floor.a.number
    src = AdvisorySource.objects.get(advisory=floor.a)
    base = f"/security/advisories/{n}"
    return ["/security/advisories/new/", f"{base}/edit/", f"{base}/sources/new/", f"{base}/sources/{src.pk}/edit/",
            f"{base}/sources/{src.pk}/revision/", f"{base}/sources/{src.pk}/remove/", f"{base}/models/",
            f"{base}/models/?model={floor.model.pk}", f"{base}/models/?os=1", f"{base}/models/{floor.row.pk}/remove/",
            f"{base}/confirm/", f"{base}/not-applicable/", f"{base}/reopen/"]


def approve_urls(floor) -> list:
    n = floor.a.number
    return [f"/security/advisories/{n}/close/", f"/security/advisories/{n}/in-error/"]


def update_door_urls(floor) -> list:
    n = floor.a.number
    return [f"/security/devices/P-2/updates/open/?advisory={n}&from=advisory",
            f"/security/releases/{floor.r56.pk}/open-updates/?advisory={n}&from=advisory"]


def line_tags(body: str) -> tuple[list, list]:
    """(the tags of the lines that need a response, the tags behind Show) on an advisory's page, in order."""
    found = r'<li class="cy-dev ad-line" data-kind="\w+">\s*<div class="cy-dev-h"><a class="tag" href="[^"]+"[^>]*>([^<]+)</a>'
    head, _, rest = body.partition("<details")
    return re.findall(found, head), re.findall(found, rest)


def line_actions(body: str) -> dict:
    """{tag: [the actions offered on its line]} on an advisory's page."""
    out = {}
    for block in re.findall(r'<li class="cy-dev ad-line".*?</li>', body, re.S):
        tag = re.search(r'<a class="tag"[^>]*>([^<]+)</a>', block).group(1)
        out[tag] = re.findall(r'<button class="link" type="button" hx-get="[^"]+" hx-target="#modal-card">([^<]+)</button>', block)
    return out


def headings(body: str) -> list:
    return re.findall(r'data-heading="(\w+)"', body)


# --- every door ---------------------------------------------------------------------------------------------------------------------

def test_every_door_opens_for_the_ce_manager(client, signed_in, floor):
    signed_in("manager")
    n = floor.a.number
    for url in (*read_urls(floor), f"/security/advisories/{n}/devices.csv"):
        assert client.get(url).status_code == 200, url
    for url in (f"/security/advisories/{n}/", *modal_urls(floor), *approve_urls(floor), *update_door_urls(floor)):
        r = client.get(url, **MODAL)
        assert r.status_code == 200, url
        assert never(html(r)) == [], (url, never(html(r)))


def test_the_advisories_tab_sits_between_models_and_decisions(client, signed_in, tenant, floor):
    signed_in("manager")
    body = html(client.get("/security/models/"))
    tabs = re.findall(r'role="tab" href="([^"]+)"', body)
    assert tabs == ["/security/", "/security/models/", "/security/advisories/", "/security/decisions/", "/security/changes/"]
    custom_user(client, tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW})
    body = html(client.get("/security/advisories/"))
    assert re.findall(r'role="tab" href="([^"]+)"', body) == ["/security/models/", "/security/advisories/"]
    assert 'aria-current="page">Advisories</a>' in body


# --- who ----------------------------------------------------------------------------------------------------------------------------

def test_device_security_view_alone_reads_no_device_line_and_no_decision_derived_word(client, tenant, floor):
    """critique40_rules S2: the line kinds, update counts, and stored statuses at View; nothing derived from a decision or a device."""
    other = make_advisory(received_on=TODAY - DAY, reference="DEMO-ADV-0002")  # its models to confirm
    d = decide(floor.a, release=floor.r52)
    custom_user(client, tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW})
    tab = html(client.get("/security/advisories/"))
    assert floor.a.number in tab and other.number in tab and "<th>Responses</th>" not in tab
    assert headings(tab) == ["to_confirm"] and "Record advisory" not in tab
    assert '<span class="chip info ad-status">Open</span>' in tab
    body = html(page(client, floor.a.number))
    assert "On a release it names: <b>3</b>" in body and "Release not recorded: <b>1</b>" in body
    for word in ("P-1", "T-1", "SN-P-1", "Record decision", "Decisions for this advisory", "No response recorded", d.number, "Decided",
                 "Devices CSV", "/models/", "/close/", "/in-error/", "/edit/", "Open software update", "Record their releases"):
        assert word not in body, word
    rows = csv_rows(client.get("/security/advisories/export.csv"))
    assert rows[0] == views_ad.LIST_COLUMNS and {row[0]: row[5] for row in rows[1:]} == {floor.a.number: "Open",
                                                                                        other.number: "Models to confirm"}
    assert client.get(f"/security/advisories/{floor.a.number}/devices.csv").status_code == 403
    for url in (*modal_urls(floor), *approve_urls(floor), *update_door_urls(floor)):
        assert client.get(url, **MODAL).status_code == 403, url


def test_view_with_equipment_view_reads_the_devices_and_acts_on_none(client, tenant, floor):
    custom_user(client, tenant, "devices", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    tab = html(client.get("/security/advisories/"))
    assert "<th>Responses</th>" in tab and headings(tab) == ["no_response", "temporary"]
    assert "Record decision" not in tab and "Open software update" not in tab
    body = html(page(client, floor.a.number))
    assert line_tags(body) == (["P-1", "P-2", "P-5", "T-1"], ["P-3", "P-4"])
    assert "No response recorded" in body and "Decisions for this advisory" in body and "Devices CSV" in body
    assert set(map(tuple, line_actions(body).values())) == {()}
    for word in ("/models/", "/close/", "/in-error/", "/edit/", "/sources/new/", "/not-applicable/", "Record their releases",
                 "Open updates to"):
        assert word not in body, word
    assert client.get(f"/security/advisories/{floor.a.number}/devices.csv").status_code == 200
    for url in (*modal_urls(floor), *approve_urls(floor), *update_door_urls(floor)):
        assert client.get(url, **MODAL).status_code == 403, url


def test_edit_records_and_sets_models_and_never_closes_or_decides(client, signed_in, floor):
    signed_in("technician")  # Device security Edit, Equipment Edit, Work orders Edit
    n = floor.a.number
    body = html(page(client, n))
    for url in (f"/security/advisories/{n}/edit/", f"/security/advisories/{n}/sources/new/", f"/security/advisories/{n}/models/",
                f"/security/advisories/{n}/not-applicable/", "Record their releases", "Open updates to 5.6 (demo)"):
        assert url in body, url
    assert line_actions(body)["P-2"] == ["Open software update"]
    assert "/security/decisions/new/" not in body and "/close/" not in body and "/in-error/" not in body
    for url in approve_urls(floor):
        assert client.get(url, **MODAL).status_code == 403
    SecurityAdvisory.objects.filter(pk=floor.a.pk).update(status=AdvisoryStatus.CLOSED, status_on=TODAY)
    assert f"/security/advisories/{n}/reopen/" not in html(page(client, n))
    assert blocker_of(html(client.get(f"/security/advisories/{n}/reopen/", **MODAL))) == advisories.CORRECT_PERMISSION


def test_approve_without_equipment_view_corrects_and_never_closes(client, tenant, floor):
    custom_user(client, tenant, "itsec", {Module.CYBERSECURITY: Level.APPROVE})
    n = floor.a.number
    body = html(page(client, n))
    assert "P-1" not in body and f"/security/advisories/{n}/in-error/" in body and "/close/" not in body
    assert client.get(f"/security/advisories/{n}/close/", **MODAL).status_code == 403
    r = client.get(f"/security/advisories/{n}/in-error/", **MODAL)
    assert r.status_code == 200 and views_ad.IN_ERROR_WORDS.format(number=n) in html(r)
    assert client.get(f"/security/advisories/{n}/devices.csv").status_code == 403


def test_scoped_users_are_refused_everywhere(client, tenant, floor):
    custom_user(client, tenant, "scoped", {m: Level.FULL for m in Module.values}, scope=DataScope.DEPARTMENT, department="ICU")
    n = floor.a.number
    for url in (*read_urls(floor), f"/security/advisories/{n}/devices.csv", *modal_urls(floor), *approve_urls(floor),
                *update_door_urls(floor)):
        assert client.get(url, **MODAL).status_code == 403, url
        assert client.post(url, {}, **MODAL).status_code in (403, 405), url


# --- the tab --------------------------------------------------------------------------------------------------------------------------

def test_needs_attention_on_the_tab(client, signed_in, floor):
    other = make_advisory(received_on=TODAY - DAY, reference="DEMO-ADV-0002")
    signed_in("manager")
    n = floor.a.number
    body = html(client.get("/security/advisories/"))
    assert headings(body) == ["to_confirm", "no_response", "temporary"]
    assert 'href="/security/advisories/?to_confirm=1"' in body and f"{other.number} · DEMO-ADV-0002 · {rules.NO_MODEL_NAMED}" in body
    assert f"{n} · Alaris 8015 PCU: 3 devices of ours; 2 on a release it names, 1 with no release recorded" in body
    assert f'hx-get="/security/decisions/new/?advisory={n}&model={floor.model.pk}&from=advisory"' in body
    assert f'hx-get="/security/decisions/new/?advisory={n}&asset=T-1&from=advisory"' in body
    assert f'hx-get="/security/devices/T-1/updates/open/?advisory={n}&from=advisory"' in body
    # an advisory decision past its review date: a page link to the Decisions tab's list of them
    d = decide(floor.a, release=floor.r52)
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(decided_on=TODAY - 2 * DAY, review_by=TODAY - DAY)
    body = html(client.get("/security/advisories/"))
    assert headings(body)[-1] == "decisions_due"
    assert '<a class="link" href="/security/decisions/?why=advisory&state=review_due&here=1">' in body
    signed_in("technician")  # Open software update, never Record decision
    body = html(client.get("/security/advisories/"))
    assert "Record decision" not in body and f"/security/devices/T-1/updates/open/?advisory={n}&from=advisory" in body


def test_the_list_its_filters_and_its_search(client, signed_in, floor):
    closed = make_advisory(received_on=TODAY - 10 * DAY, reference="DEMO-ADV-0009", cves=["CVE-2025-7777"],
                           status=AdvisoryStatus.CLOSED, status_on=TODAY - DAY, models_confirmed_on=TODAY - 2 * DAY)
    na = make_advisory(received_on=TODAY - 8 * DAY, reference="DEMO-ADV-0010", status=AdvisoryStatus.NOT_APPLICABLE,
                       not_applicable_reason="no_model_here", status_on=TODAY - DAY)
    removed = make_advisory(received_on=TODAY - 9 * DAY, reference="DEMO-ADV-0011", removed_on=TODAY)
    fresh = make_advisory(received_on=TODAY - 4 * DAY, reference="DEMO-ADV-0012")
    signed_in("technician")

    def numbers(q: str = "") -> list:
        body = html(client.get(f"/security/advisories/?{q}", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="cy-advisories"))
        assert body.lstrip().startswith('<div id="cy-advisories"')
        return re.findall(r'<a class="tag" href="/security/advisories/([A-Z0-9-]+)/">', body)

    assert numbers() == [floor.a.number, fresh.number] and numbers("status=bogus") == [floor.a.number, fresh.number]
    assert numbers("status=closed") == [closed.number] and numbers("status=not_applicable") == [na.number]
    assert numbers("status=all") == [floor.a.number, fresh.number, na.number, removed.number, closed.number]
    assert numbers("to_confirm=1") == [fresh.number] and numbers("status=closed&to_confirm=1") == []
    assert numbers("q=cve-2025-7777&status=all") == [closed.number] and numbers("q=CVE-2026-1001") == [floor.a.number]
    assert numbers("q=demo-adv-0012") == [fresh.number] and numbers(f"q={floor.a.number.lower()}") == [floor.a.number]
    assert numbers("q=nothing") == [] and numbers("q=CVE-2026-10") == []  # a CVE ID matches whole, never a part of one
    body = html(client.get("/security/advisories/?status=closed&q=nothing"))
    assert "No advisory matches these filters." in body


def test_the_empty_words(client, signed_in, ctx):
    signed_in("technician")
    assert rules.ADVISORIES_EMPTY in html(client.get("/security/advisories/"))
    make_advisory(status=AdvisoryStatus.CLOSED, status_on=TODAY, models_confirmed_on=TODAY)
    body = html(client.get("/security/advisories/"))
    assert views_ad.NONE_OPEN in body and rules.ADVISORIES_EMPTY not in body and rules.ATTENTION_EMPTY in body


def test_the_list_csv(client, signed_in, tenant, floor):
    update_wo(floor.d["P-1"], floor.r56)
    signed_in("manager")
    r = client.get("/security/advisories/export.csv")
    assert r["Content-Disposition"].endswith(f'cadence-security-advisories-{TODAY:%Y-%m-%d}.csv"')
    rows = csv_rows(r)
    SEEN.append(json.dumps(rows))
    assert rows[0] == views_ad.LIST_COLUMNS + views_ad.DEVICE_COLUMNS_EXTRA
    assert rows[0][6:] == ["On a release it names", "Release not recorded", "On the fix it names", "On a release it does not name",
                           "Update open", "Version not confirmed", "No response recorded", "Decision past its review date",
                           "Patches, another list", "Decided", "Patches recorded"]
    assert rows[1] == [floor.a.number, "CVE-2026-1001, CVE-2026-1002", "Manufacturer bulletin DEMO-ADV-0001", "BD Alaris 8015 PCU",
                       (TODAY - 3 * DAY).isoformat(), "Open: 3 devices with no response recorded", "3", "1", "1", "1", "1", "0", "3", "0",
                       "0", "0", "0"]
    custom_user(client, tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW})
    rows = csv_rows(client.get("/security/advisories/export.csv?q=cve-2026-1001"))
    assert rows[0] == views_ad.LIST_COLUMNS and rows[1][5:] == ["Open", "3", "1", "1", "1", "1", "0"]


# --- an advisory's page -------------------------------------------------------------------------------------------------------------

def test_the_page_names_the_advisory_its_sources_and_the_models_it_names(client, signed_in, floor):
    other = make_advisory(received_on=TODAY - 2 * DAY, cves=["CVE-2026-1002"], reference="DEMO-ADV-0002")
    AdvisorySource.objects.filter(advisory=floor.a).update(url="https://example.org/advisories/demo-adv-0001")
    user = signed_in("manager")
    SecurityAdvisory.objects.filter(pk=floor.a.pk).update(recorded_by=user)
    n = floor.a.number
    body = html(page(client, n))
    assert f"<h2>{n} · DEMO-ADV-0001 · Alaris 8015 PCU</h2>" in body
    assert f"Received {(TODAY - 3 * DAY):%b} {(TODAY - 3 * DAY).day}, {(TODAY - 3 * DAY).year} · recorded by Manager User" in body
    assert '<span class="ad-cve">CVE-2026-1001</span>' in body
    other_url = f"/security/advisories/{other.number}/"
    assert (f'Also on <a class="link" href="{other_url}" hx-get="{other_url}" hx-target="#drawer">{other.number}</a>: CVE-2026-1002'
            in body)
    assert 'href="https://example.org/advisories/demo-adv-0001" target="_blank" rel="noopener noreferrer"' in body
    assert "The releases listed: 5.2 (demo)" in body and "A release from the manufacturer · the fix it names: 5.6 (demo)" in body
    assert line_tags(body) == (["P-1", "P-2", "P-5", "T-1"], ["P-3", "P-4"])
    assert "Show 2 more: on the fix it names, or on a release it does not name" in body
    assert "serial SN-P-1 · ICU · Can connect (wireless), as disclosed" in body and "Rental · Summit Medical Rentals" in body
    full = html(client.get(f"/security/advisories/{n}/"))
    assert 'id="cy-advisories"' in full and f"<h2>{n} · DEMO-ADV-0001" in full
    assert client.get("/security/advisories/ad-99-9999/", **HX).status_code == 404
    assert page(client, n.lower()).status_code == 200  # any letter case


def test_responses_with_their_links(client, signed_in, floor):
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    d = decide(floor.a, asset=floor.d["P-2"])
    signed_in("manager")
    body = html(page(client, floor.a.number))
    assert f'Update open to 5.6 (demo), the fix: <a class="link" href="/work-orders/{wo.number}/"' in body
    assert f'<a class="link" href="/security/decisions/{d.number}/" hx-get="/security/decisions/{d.number}/" hx-target="#drawer">{d.number}</a>: ' in body
    custom_user(client, floor.a.tenant, "devices", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    body = html(page(client, floor.a.number))  # no Work orders View: the number, never a link
    assert f"Update open to 5.6 (demo), the fix: {wo.number}" in body and f'href="/work-orders/{wo.number}/"' not in body


def test_each_line_offers_only_what_can_act(client, signed_in, floor):
    """practice N3: Open software update only where one can open (no update open on the device, no patches recorded or version to
    confirm, not held); Record decision only where nothing covers the line; Review and renew for its own device decision past review."""
    update_wo(floor.d["P-1"], floor.r56)  # an update open to the fix: a decision may still be recorded (interim controls)
    decide(floor.a, asset=floor.d["P-2"])  # covered
    own = decide(floor.a, asset=floor.d["T-1"])
    DeviceSecurityDecision.objects.filter(pk=own.pk).update(decided_on=TODAY - 2 * DAY, review_by=TODAY - DAY)  # its own, past review
    signed_in("manager")
    n = floor.a.number
    acts = line_actions(html(page(client, n)))
    assert acts == {"P-1": ["Record decision"], "P-2": ["Open software update"], "P-5": ["Record decision", "Open software update"],
                    "T-1": ["Review and renew", "Open software update"], "P-3": [], "P-4": []}
    body = html(page(client, n))
    assert f"/security/decisions/new/?supersedes={own.number}&from=advisory" in body
    assert f"/security/decisions/new/?advisory={n}&asset=P-5&from=advisory" in body
    Asset.objects.filter(pk=floor.d["P-5"].pk).update(incident_hold=True)  # held as evidence: no update opens on it
    assert line_actions(html(page(client, n)))["P-5"] == ["Record decision"]


def test_each_model_row_offers_only_what_can_act(client, signed_in, floor):
    signed_in("manager")
    n = floor.a.number
    body = html(page(client, n))
    assert f'hx-get="/security/releases/{floor.r56.pk}/open-updates/?advisory={n}&from=advisory"' in body
    assert f'hx-get="/security/decisions/new/?advisory={n}&release={floor.r52.pk}&from=advisory"' in body
    assert f'hx-get="/security/decisions/new/?advisory={n}&model={floor.model.pk}&from=advisory"' in body
    assert f'hx-get="/security/models/{floor.model.pk}/set-release/"' in body and "Record their releases" in body
    # every device needing a response has an update open: nothing more to open; ours covered by the model's decision: no decision
    for tag in ("P-1", "P-2", "P-5", "T-1"):
        update_wo(floor.d[tag], floor.r56)
    decide(floor.a, device_model=floor.model)
    body = html(page(client, n))
    assert "Open updates to" not in body and "Record decision for" not in body
    facts(floor.d["P-5"], floor.r52)  # every release recorded: nothing to record
    assert "Record their releases" not in html(page(client, n))


def test_decisions_for_this_advisory(client, signed_in, floor):
    d = decide(floor.a, release=floor.r52, exception_reference="DEMO-EX-1")
    signed_in("manager")
    section = html(page(client, floor.a.number)).split("Decisions for this advisory", 1)[1].split("</table>", 1)[0]
    assert f'>{d.number}</a><small><span class="chip ok">In force</span></small>' in section
    assert "Alaris 8015 PCU on 5.2 (demo)" in section and "DEMO-EX-1" in section and rules.review_words(d, TODAY) in section
    DeviceSecurityDecision.objects.filter(pk=d.pk).update(decided_on=TODAY - 2 * DAY, review_by=TODAY - DAY)
    section = html(page(client, floor.a.number)).split("Decisions for this advisory", 1)[1]
    assert '<span class="chip warn">Review due</span>' in section


def test_the_checks_and_the_history(client, signed_in, floor):
    user = signed_in("manager")
    release(floor.model, "5.8 (demo)")
    body = html(page(client, floor.a.number))
    assert "Release added since: 5.8 (demo), not listed or named as the fix" in body
    advisories.add_source(floor.a, source="other", reference="DEMO-ADV-0001-N", by=user, today=TODAY)
    r = client.get(f"/security/advisories/{floor.a.number}/?history=1", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="hist")
    assert r.status_code == 200 and "Source added" in html(r)
    assert "Show changes" in html(page(client, floor.a.number))


def test_the_devices_csv_a_row_of_each_line_kind(client, signed_in, floor):
    wo, _ = update_wo(floor.d["P-1"], floor.r56)
    d = decide(floor.a, asset=floor.d["P-2"], exception_reference="DEMO-EX-2")
    signed_in("manager")
    r = client.get(f"/security/advisories/{floor.a.number}/devices.csv")
    assert r["Content-Disposition"].endswith(f'cadence-security-advisory-{floor.a.number}-devices-{TODAY:%Y-%m-%d}.csv"')
    rows = csv_rows(r)
    SEEN.append(json.dumps(rows))
    assert rows[0] == ["Advisory", "CVE IDs", "Tag", "Serial", "Manufacturer", "Model", "Department", "Whose", "Connection", "Release",
                       "Line", "Response", "Decision", "IT exception number", "Review by"]
    by_tag = {row[2]: row for row in rows[1:]}
    assert list(by_tag) == ["P-1", "P-2", "P-3", "P-4", "P-5", "T-1"]
    assert by_tag["P-1"][7:13] == ["Ours", "Can connect (wireless), as disclosed", "5.2 (demo)", "On a release it names",
                                   f"Update open to 5.6 (demo), the fix: {wo.number}", ""]
    assert by_tag["P-2"][11:] == [rules.decision_words(d, TODAY), d.number, "DEMO-EX-2", d.review_by.isoformat()]
    assert by_tag["P-3"][10:] == ["On a release it does not name", "", "", "", ""]
    assert by_tag["P-4"][10] == "On the fix it names" and by_tag["P-5"][9:12] == ["Release not recorded", "Release not recorded",
                                                                                     "No response recorded"]
    assert by_tag["T-1"][:4] == [floor.a.number, "CVE-2026-1001, CVE-2026-1002", "T-1", "SN-T-1"] and by_tag["T-1"][7] == "Rental"


# --- Record advisory, Edit advisory -----------------------------------------------------------------------------------------------------

def test_record_advisory_with_a_cve_another_advisory_holds(client, signed_in, floor):
    user = signed_in("technician")
    url = "/security/advisories/new/"
    r = client.get(url, **MODAL)
    assert r.context["form"]["received_on"].value() == TODAY.isoformat() and 'id="ad-new-form"' in html(r)
    data = {"received_on": TODAY.isoformat(), "cves": "CVE-2026-2001 (Critical)", "source": "cisa_icsma", "reference": "ICSMA-26-001-01",
            "url": "", "published_on": ""}
    body = unescape(client.post(url, data, **MODAL).content.decode())  # the refusal names the piece typed: never scanned as Cadence's
    assert errors(body)["cves"] == "'(Critical)' is not a CVE ID: paste the IDs only."
    data["cves"] = "cve-2026-1001, CVE-2026-2001"
    body = html(client.post(url, data, **MODAL))
    assert f"CVE-2026-1001 is on {floor.a.number} (Manufacturer bulletin DEMO-ADV-0001)." in body and "cves" not in errors(body)
    assert f'hx-post="/security/advisories/{floor.a.number}/sources/new/"' in body and "Record as its own advisory" in body
    assert f'<input type="hidden" name="seen" value="{floor.a.number}">' in body and SecurityAdvisory.objects.count() == 1
    r = client.post(url, {**data, "seen": [floor.a.number]}, **MODAL)
    new = SecurityAdvisory.objects.exclude(pk=floor.a.pk).get()
    saved_page(r, new.number)
    assert toast_of(r) == f"{new.number} recorded." and new.cves == ["CVE-2026-1001", "CVE-2026-2001"] and new.recorded_by == user
    # the same kind of source and reference: the same publication, refused under the reference
    body = html(client.post(url, {**data, "source": "manufacturer_bulletin", "reference": "demo-adv-0001", "cves": ""}, **MODAL))
    assert errors(body)["reference"] == advisories.REFERENCE_RECORDED.format(reference="demo-adv-0001", number=floor.a.number)
    assert client.get(url)["Location"] == "/security/advisories/"


def test_add_as_a_source_of_the_advisory_sharing_its_cves(client, signed_in, floor):
    """The note's "Add as a source of AD-…" posts Record advisory's fields to that advisory's Add source: its CVE IDs merge."""
    signed_in("technician")
    n = floor.a.number
    data = {"received_on": TODAY.isoformat(), "cves": "CVE-2026-1002 CVE-2026-3003", "source": "cisa_icsma",
            "reference": "ICSMA-26-002-01", "url": "https://www.cisa.gov/news-events/ics-medical-advisories/icsma-26-002-01",
            "published_on": TODAY.isoformat(), "seen": [n]}
    r = client.post(f"/security/advisories/{n}/sources/new/", data, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"Source added to {n}."
    floor.a.refresh_from_db()
    assert floor.a.cves == ["CVE-2026-1001", "CVE-2026-1002", "CVE-2026-3003"] and SecurityAdvisory.objects.count() == 1
    assert AdvisorySource.objects.get(advisory=floor.a, reference="ICSMA-26-002-01").source == "cisa_icsma"


def test_edit_advisory(client, signed_in, floor):
    other = make_advisory(cves=["CVE-2026-5005"], reference="DEMO-ADV-0002")
    signed_in("technician")
    n = floor.a.number
    url = f"/security/advisories/{n}/edit/"
    r = client.get(url, **MODAL)
    assert r.context["form"]["cves"].value() == "CVE-2026-1001 CVE-2026-1002"
    data = {"received_on": (TODAY - 4 * DAY).isoformat(), "cves": "CVE-2026-1001 CVE-2026-1002 CVE-2026-5005"}
    body = html(client.post(url, data, **MODAL))
    assert f"CVE-2026-5005 is on {other.number} (Manufacturer bulletin DEMO-ADV-0002)." in body and views_ad.EDIT_OVERLAP_HINT in body
    r = client.post(url, {**data, "seen": [other.number]}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"{n} saved."
    floor.a.refresh_from_db()
    assert floor.a.received_on == TODAY - 4 * DAY and floor.a.cves[-1] == "CVE-2026-5005" and floor.a.models_confirmed_on is None
    body = html(client.post(url, {"received_on": (TODAY + DAY).isoformat(), "cves": ""}, **MODAL))
    assert "received_on" in errors(body)
    SecurityAdvisory.objects.filter(pk=floor.a.pk).update(removed_on=TODAY)
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.ENTERED_IN_ERROR.format(number=n)


# --- sources --------------------------------------------------------------------------------------------------------------------------

def test_edit_a_source_record_its_revisions_and_remove_one(client, signed_in, floor):
    user = signed_in("technician")
    n = floor.a.number
    src = AdvisorySource.objects.get(advisory=floor.a)
    base = f"/security/advisories/{n}/sources/{src.pk}"
    assert blocker_of(html(client.get(f"{base}/remove/", **MODAL))) == advisories.LAST_SOURCE
    edit = {"source": "manufacturer_bulletin", "reference": "DEMO-ADV-0001", "url": "https://intranet.local/demo", "published_on": ""}
    assert errors(html(client.post(f"{base}/edit/", edit, **MODAL)))["url"] == advisories.LINK_WORDS
    r = client.post(f"{base}/edit/", {**edit, "url": "https://example.org/demo"}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"Source saved on {n}." and AdvisorySource.objects.get(pk=src.pk).url == "https://example.org/demo"
    r = client.post(f"{base}/revision/", {"revision": "2", "updated_on": TODAY.isoformat(), "last_change": ["mitigations_changed"]}, **MODAL)
    assert toast_of(r) == f"Revision recorded on {n}."
    assert "revision 2, updated" in html(page(client, n)) and "what changed: Mitigations changed" in html(page(client, n))
    r = client.post(f"{base}/revision/", {"revision": "3", "updated_on": TODAY.isoformat(), "last_change": ["products_changed"]}, **MODAL)
    assert toast_of(r) == f"Revision recorded on {n}: confirm the models it names."
    assert "Models to confirm" in saved_page(r, n)
    body = html(client.post(f"{base}/revision/", {"revision": "", "updated_on": "", "last_change": []}, **MODAL))
    assert errors(body)["revision"] == advisories.REVISION_NEEDED
    second = advisories.add_source(floor.a, source="other", reference="DEMO-ADV-0001-N", by=user, today=TODAY)
    url = f"/security/advisories/{n}/sources/{second.pk}/remove/"
    assert views_ad.REMOVE_SOURCE_WORDS.format(source="Other DEMO-ADV-0001-N", number=n) in html(client.get(url, **MODAL))
    r = client.post(url, {}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"Source removed from {n}." and AdvisorySource.objects.get(pk=second.pk).removed_on == TODAY
    assert client.get(f"/security/advisories/{n}/sources/{second.pk}/edit/", **MODAL).status_code == 200  # says it was removed
    assert blocker_of(html(client.get(f"/security/advisories/{n}/sources/{second.pk}/edit/", **MODAL))) == (
        advisories.SOURCE_REMOVED_WORDS.format(number=n))


def test_a_revision_opens_an_advisory_marked_not_applicable_again(client, signed_in, ctx):
    a = make_advisory(reference="DEMO-ADV-0003")
    advisories.mark_not_applicable(a, reason="no_model_here", by=None, today=TODAY)
    signed_in("technician")
    src = AdvisorySource.objects.get(advisory=a)
    r = client.post(f"/security/advisories/{a.number}/sources/{src.pk}/revision/",
                    {"revision": "UpdateA", "updated_on": TODAY.isoformat(), "last_change": ["products_changed"]}, **MODAL)
    assert toast_of(r) == f"Revision recorded on {a.number}: it is open again, with the models it names to confirm."
    a.refresh_from_db()
    assert a.status == AdvisoryStatus.OPEN and a.models_confirmed_on is None


# --- Set models -------------------------------------------------------------------------------------------------------------------------

def test_set_models_choose_the_model_then_its_values(client, signed_in, floor, vent_model):
    signed_in("technician")
    n = floor.a.number
    base = f"/security/advisories/{n}/models/"
    body = html(client.get(base, **MODAL))
    assert f'<option value="{floor.model.pk}">BD Alaris 8015 PCU (named on it)</option>' in body
    assert f'<option value="{vent_model.pk}">Hamilton Medical Hamilton-G5</option>' in body
    release(vent_model, "", os_family="windows", os_name="Windows 10 IoT Enterprise")
    select = html(client.get(f"{base}?os=1", **MODAL)).split('id="ad-model"', 1)[1].split("</select>", 1)[0]
    assert "Hamilton Medical Hamilton-G5: Windows (Windows 10 IoT Enterprise)" in select and "Alaris" not in select
    r = client.get(f"{base}?model={floor.model.pk}", **MODAL)
    form = r.context["form"]
    assert (form["versions"].value(), form["release_ids"].value(), form["remedy"].value(), form["fix_release"].value()) == (
        "listed", [str(floor.r52.pk)], "release", str(floor.r56.pk))
    assert form["confirm"].value() is True
    assert [label for _v, label in form.fields["release_ids"].choices] == [
        "5.2 (demo) · 3 devices on site", "5.4 (demo) · 1 device on site", "5.6 (demo) · 1 device on site"]
    url = f"{base}?model={floor.model.pk}"
    data = {"versions": "listed", "release_ids": [str(floor.r52.pk), str(floor.r54.pk)], "remedy": "release", "fix_release": "",
            "patches_reference": "", "confirm": "on"}
    r = client.post(url, data, **MODAL)
    assert errors(html(r))["fix_release"] == advisories.FIX_NEEDED  # what was chosen kept
    assert r.context["form"]["release_ids"].value() == [str(floor.r52.pk), str(floor.r54.pk)]
    r = client.post(url, {**data, "fix_release": str(floor.r56.pk)}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"{n}: the Alaris 8015 PCU saved."
    assert {x.release_id for x in AdvisoryModel.objects.get(pk=floor.row.pk).releases.all()} == {floor.r52.pk, floor.r54.pk}
    r = client.post(f"{base}?model={vent_model.pk}", {"versions": "all", "remedy": "no_fix_yet", "confirm": "on"}, **MODAL)
    assert toast_of(r) == f"{n} names the Hamilton-G5." and AdvisoryModel.objects.filter(advisory=floor.a).count() == 2
    assert client.get(url)["Location"] == f"/security/advisories/{n}/"
    SecurityAdvisory.objects.filter(pk=floor.a.pk).update(status=AdvisoryStatus.CLOSED, status_on=TODAY)
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.REOPEN_TO_CHANGE.format(number=n)
    body = html(client.post(url, {**data, "fix_release": str(floor.r56.pk)}, **MODAL))  # posted anyway: the writer's words
    assert errors(body)[""] == advisories.REOPEN_TO_CHANGE.format(number=n)


def test_add_a_version_not_on_file(client, signed_in, floor):
    """scope B4: services.add_release alone, then Set models again from the POST with it chosen as the fix (a release remedy with none
    chosen) or ticked (releases listed); nothing else saved; its refusal on the box."""
    signed_in("technician")
    url = f"/security/advisories/{floor.a.number}/models/?model={floor.model.pk}"
    data = {"versions": "listed", "release_ids": [str(floor.r52.pk)], "remedy": "release", "fix_release": "", "patches_reference": "",
            "confirm": "on", "add_version": "5.8 (demo)", "action": "add_version"}
    r = client.post(url, data, **MODAL)
    new = SoftwareRelease.objects.get(device_model=floor.model, version="5.8 (demo)")
    assert views_ad.VERSION_ADDED.format(version="5.8 (demo)", model="Alaris 8015 PCU") in html(r)
    form = r.context["form"]
    assert form["fix_release"].value() == str(new.pk) and form["release_ids"].value() == [str(floor.r52.pk)]
    assert form["add_version"].value() == "" and "action" not in r.context.get("form").data
    assert AdvisoryModel.objects.get(pk=floor.row.pk).fix_release_id == floor.r56.pk  # nothing else saved
    r = client.post(url, {**data, "fix_release": str(floor.r56.pk), "add_version": "5.9 (demo)"}, **MODAL)
    nine = SoftwareRelease.objects.get(device_model=floor.model, version="5.9 (demo)")
    assert r.context["form"]["release_ids"].value() == [str(floor.r52.pk), str(nine.pk)]
    body = html(client.post(url, {**data, "add_version": ""}, **MODAL))
    assert errors(body)["add_version"] == "Enter the version as the manufacturer writes it."
    body = html(client.post(url, {**data, "add_version": "5.2 (demo)"}, **MODAL))
    assert "add_version" in errors(body)


# --- Remove model, Confirm models, Not applicable, Close, Reopen, Entered in error ---------------------------------------------------

def test_remove_model(client, signed_in, floor, vent_model):
    vent_row = model_row(floor.a, vent_model, versions="all", remedy="no_fix_yet")
    d = decide(floor.a, release=floor.r52)
    n = floor.a.number
    url = f"/security/advisories/{n}/models/{floor.row.pk}/remove/"
    signed_in("manager")
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.DECISION_FOR.format(decision=d.number, scope="5.2 (demo)")
    signed_in("technician")
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.DECISION_FOR_READ.format(decision=d.number, scope="5.2 (demo)")
    vurl = f"/security/advisories/{n}/models/{vent_row.pk}/remove/"
    assert views_ad.REMOVE_MODEL_WORDS.format(number=n, model="Hamilton-G5") in html(client.get(vurl, **MODAL))
    r = client.post(vurl, {}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"{n} no longer names the Hamilton-G5." and not AdvisoryModel.objects.filter(pk=vent_row.pk).exists()
    assert blocker_of(html(client.get(vurl, **MODAL))) == advisories.ROW_GONE.format(number=n)
    assert client.post(vurl, {}, **MODAL).status_code == 404


def test_confirm_models(client, signed_in, ctx, pump_model):
    a = make_advisory(reference="DEMO-ADV-0002")
    signed_in("technician")
    url = f"/security/advisories/{a.number}/confirm/"
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.NAME_A_MODEL
    model_row(a, pump_model, versions="all", remedy="no_fix_yet", confirm=False)
    body = html(client.get(url, **MODAL))
    assert views_ad.CONFIRM_WORDS.format(number=a.number, models="the Alaris 8015 PCU") in body
    assert f"/security/advisories/{a.number}/confirm/" in html(page(client, a.number))
    r = client.post(url, {}, **MODAL)
    saved_page(r, a.number)
    assert toast_of(r) == f"{a.number}: models confirmed."
    a.refresh_from_db()
    assert a.models_confirmed_on == TODAY
    assert blocker_of(html(client.get(url, **MODAL))) == views_ad.ALREADY_CONFIRMED.format(number=a.number, day=rules.day_words(TODAY))


def test_not_applicable_each_reason_checked_before_it_is_tried(client, signed_in, floor):
    signed_in("technician")
    n = floor.a.number
    body = html(client.get(f"/security/advisories/{n}/not-applicable/", **MODAL))
    refusals = dict(re.findall(r'<small class="ad-why" data-refusal="(\w+)">([^<]*)</small>', body))
    assert refusals == {k: v for k, v in advisories.not_applicable_refusals(floor.a, TODAY).items()}
    assert set(refusals) == {"no_model_here", "none_here", "releases_not_here"} and 'type="submit" disabled' in body
    r = client.post(f"/security/advisories/{n}/not-applicable/", {"not_applicable_reason": "none_here"}, **MODAL)
    assert errors(html(r))["not_applicable_reason"] == refusals["none_here"]
    b = make_advisory(reference="DEMO-ADV-0003")
    body = html(client.get(f"/security/advisories/{b.number}/not-applicable/", **MODAL))
    assert advisories.NOT_APPLICABLE_NOTE in body and 'data-refusal="no_model_here"' not in body
    r = client.post(f"/security/advisories/{b.number}/not-applicable/", {"not_applicable_reason": "no_model_here"}, **MODAL)
    body = saved_page(r, b.number)
    assert toast_of(r) == f"{b.number} marked not applicable here."
    assert f"Not applicable here (Technician User, {rules.day_words(TODAY)}): It names no model in this facility's catalog" in body
    assert blocker_of(html(client.get(f"/security/advisories/{b.number}/not-applicable/", **MODAL))) == (
        advisories.ALREADY_NOT_APPLICABLE.format(number=b.number))
    d = decide(floor.a, release=floor.r52)
    assert blocker_of(html(client.get(f"/security/advisories/{n}/not-applicable/", **MODAL))) == (
        advisories.DECISION_UNDER_READ.format(decision=d.number))


def test_close_says_why_before_it_is_tried_then_closes(client, signed_in, floor):
    user = signed_in("manager")
    n = floor.a.number
    url = f"/security/advisories/{n}/close/"
    words = blocker_of(html(client.get(url, **MODAL)))
    assert words == advisories.close_refusal(floor.a, TODAY, user=user) and "P-1, P-2, P-5, T-1" in words
    decide(floor.a, device_model=floor.model)
    decide(floor.a, asset=floor.d["T-1"])
    assert views_ad.CLOSE_WORDS.format(number=n) in html(client.get(url, **MODAL))
    r = client.post(url, {}, **MODAL)
    body = saved_page(r, n)
    assert toast_of(r) == f"{n} closed." and f"Closed (Manager User, {rules.day_words(TODAY)})" in body
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.ALREADY_CLOSED.format(number=n)


def test_reopen_from_closed_at_approve_and_from_not_applicable_at_edit(client, make_user, floor):
    n = floor.a.number
    tech, manager = make_user("technician"), make_user("manager")
    SecurityAdvisory.objects.filter(pk=floor.a.pk).update(status=AdvisoryStatus.CLOSED, status_on=TODAY)
    client.force_login(tech)
    r = client.post(f"/security/advisories/{n}/reopen/", {}, **MODAL)
    assert r.status_code == 200 and errors(html(r))[""] == advisories.CORRECT_PERMISSION  # refused in words, at the top
    floor.a.refresh_from_db()
    assert floor.a.status == AdvisoryStatus.CLOSED
    client.force_login(manager)
    assert views_ad.REOPEN_WORDS.format(number=n) in html(client.get(f"/security/advisories/{n}/reopen/", **MODAL))
    r = client.post(f"/security/advisories/{n}/reopen/", {}, **MODAL)
    saved_page(r, n)
    assert toast_of(r) == f"{n} reopened."
    assert blocker_of(html(client.get(f"/security/advisories/{n}/reopen/", **MODAL))) == advisories.ALREADY_OPEN.format(number=n)
    b = make_advisory(reference="DEMO-ADV-0003")
    advisories.mark_not_applicable(b, reason="no_model_here", by=None, today=TODAY)
    client.force_login(tech)
    r = client.post(f"/security/advisories/{b.number}/reopen/", {}, **MODAL)
    saved_page(r, b.number)
    b.refresh_from_db()
    assert b.status == AdvisoryStatus.OPEN and b.models_confirmed_on == TODAY  # the confirmation kept


def test_entered_in_error(client, signed_in, floor):
    d = decide(floor.a, release=floor.r52)
    signed_in("manager")
    n = floor.a.number
    url = f"/security/advisories/{n}/in-error/"
    assert blocker_of(html(client.get(url, **MODAL))) == advisories.DECISION_UNDER.format(decision=d.number)
    decisions.end_decision(d, by=None, today=TODAY)
    assert views_ad.IN_ERROR_WORDS.format(number=n) in html(client.get(url, **MODAL))
    r = client.post(url, {}, **MODAL)
    body = saved_page(r, n)
    assert toast_of(r) == f"{n} marked entered in error." and '<span class="chip neutral">Entered in error</span>' in body
    assert "/edit/" not in body and "/in-error/" not in body and "P-1" not in body
    assert html(client.get("/security/advisories/?status=all")).count(n) >= 1 and n not in html(client.get("/security/advisories/"))


def test_advisory_saved_takes_the_advisorys_number_too(rf, make_user, tenant, floor):
    """The answer Record decision `from=advisory` gives (slice 39's door, by the advisory's number, any letter case): the page into
    #drawer, the lists told, the toast, the modal closed after settle."""
    from django_htmx.middleware import HtmxDetails

    request = rf.post(f"/security/decisions/new/?advisory={floor.a.number}&asset=P-2&from=advisory", HTTP_HX_REQUEST="true")
    request.user, request.tenant = make_user("manager"), tenant
    request.htmx = HtmxDetails(request)
    r = views_ad.advisory_saved(request, floor.a.number.lower(), "SD-26-0001 recorded for P-2.")
    body = saved_page(r, floor.a.number)
    assert toast_of(r) == "SD-26-0001 recorded for P-2." and "P-2" in body


def test_a_modal_opened_directly_shows_the_advisory(client, signed_in, floor):
    signed_in("manager")
    for url in (*modal_urls(floor)[1:], *approve_urls(floor)):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"] == f"/security/advisories/{floor.a.number}/", url


# --- slice 38's update doors from an advisory ------------------------------------------------------------------------------------------

def test_open_software_update_from_an_advisory(client, signed_in, floor):
    signed_in("technician")
    n = floor.a.number
    url = f"/security/devices/P-2/updates/open/?advisory={n}&from=advisory"
    r = client.get(url, **MODAL)
    form = r.context["form"]
    assert (form["target"].value(), form["target_release"].value()) == ("release", str(floor.r56.pk))
    assert f'hx-post="{url}"' in html(r)
    r = client.post(url, {"target": "release", "target_release": str(floor.r56.pk), "due_on": (TODAY + 30 * DAY).isoformat(),
                          "priority": "normal"}, **MODAL)
    saved_page(r, n)
    assert "wo-changed" in triggers(r) and " opened for P-2. " in toast_of(r)
    assert SoftwareUpdate.objects.filter(asset=floor.d["P-2"], target_release=floor.r56).exists()
    assert client.get(url)["Location"] == f"/security/advisories/{n}/"
    assert client.get("/security/devices/P-5/updates/open/?advisory=AD-99-9999&from=advisory", **MODAL).status_code == 404
    r = client.get(f"/security/devices/P-5/updates/open/?advisory={n}", **MODAL)  # no from=advisory: slice 38's door as it was
    assert r.context["form"]["target_release"].value() in (None, "")


def test_open_software_update_from_an_advisory_naming_patches(client, signed_in, floor, vent_model, dept):
    Asset.objects.create(tag="V-1", device_model=vent_model, department=dept)
    b = make_advisory(reference="DEMO-ADV-0002")
    model_row(b, vent_model, versions=AdvisoryVersions.ALL, remedy=AdvisoryRemedy.PATCHES, patches_reference="DEMO-PATCH-2026-10")
    signed_in("technician")
    assert line_actions(html(page(client, b.number)))["V-1"] == ["Open software update"]
    form = client.get(f"/security/devices/V-1/updates/open/?advisory={b.number}&from=advisory", **MODAL).context["form"]
    assert (form["target"].value(), form["target_reference"].value()) == ("patches", "DEMO-PATCH-2026-10")


def test_open_updates_to_the_fix_from_an_advisory(client, signed_in, floor):
    signed_in("technician")
    n = floor.a.number
    url = f"/security/releases/{floor.r56.pk}/open-updates/?advisory={n}&from=advisory"
    r = client.get(url, **MODAL)
    form = r.context["form"]
    assert form["from_releases"].value() == [str(floor.r52.pk)] and form["include_not_recorded"].value() is True
    assert f'hx-post="{url}"' in html(r)
    assert client.get(url)["Location"] == f"/security/advisories/{n}/"
    r = client.post(url, {"from_releases": [str(floor.r52.pk)], "include_not_recorded": "on", "due_on": (TODAY + 30 * DAY).isoformat(),
                          "priority": "normal"}, **MODAL)
    saved_page(r, n)
    assert "wo-changed" in triggers(r) and toast_of(r).startswith("4 software updates opened to install 5.6 (demo).")
    assert WorkOrder.objects.count() == 4
    # the panel's door carries the panel's filters, never the advisory's
    r = client.get(f"/security/releases/{floor.r56.pk}/open-updates/?advisory={n}&whose=ours")
    assert r["Location"] == f"/security/models/{floor.model.pk}/?whose=ours"


# --- fixed queries ----------------------------------------------------------------------------------------------------------------------

def test_fixed_queries_as_advisories_rows_devices_decisions_and_updates_grow(client, make_user, tenant, floor, vent_model, dept):
    """For the CE manager (every part) and Device security View alone (the model rows only): before == after."""
    manager = make_user("manager")
    view_only = custom_user(client, tenant, "view-only", {Module.CYBERSECURITY: Level.VIEW}, login=False)
    n = floor.a.number
    update_wo(floor.d["P-1"], floor.r56)
    decide(floor.a, asset=floor.d["P-2"])
    make_advisory(received_on=TODAY - DAY, reference="DEMO-ADV-0002", cves=["CVE-2026-1001"])  # models to confirm, a shared CVE
    urls = [("/security/advisories/", {}), ("/security/advisories/", {**HX, "HTTP_HX_TARGET": "cy-advisories"}),
            ("/security/advisories/export.csv", {}), (f"/security/advisories/{n}/", HX), (f"/security/advisories/{n}/", {}),
            (f"/security/advisories/{n}/devices.csv", {}), (f"/security/advisories/{n}/models/?model={floor.model.pk}", MODAL),
            (f"/security/advisories/{n}/not-applicable/", MODAL), (f"/security/advisories/{n}/close/", MODAL),
            ("/security/advisories/new/", MODAL), (f"/security/advisories/{n}/models/{floor.row.pk}/remove/", MODAL),
            *((url, MODAL) for url in update_door_urls(floor))]
    reads = [("/security/advisories/", {}), ("/security/advisories/export.csv", {}), (f"/security/advisories/{n}/", HX)]

    def counts() -> list:
        out = []
        for user, which in ((manager, urls), (view_only, reads)):
            client.force_login(user)
            for url, headers in which:
                with CaptureQueriesContext(connection) as q:
                    r = client.get(url, **headers)
                    if r.streaming:
                        b"".join(r.streaming_content)
                assert r.status_code == 200, url
                out.append(len(q))
        return out

    before = counts()
    for i in range(3):
        a = make_advisory(received_on=TODAY - (i + 2) * DAY, reference=f"DEMO-ADV-01{i}", cves=[f"CVE-2026-90{i}0", "CVE-2026-1002"])
        model_row(a, floor.model, releases=[floor.r52, floor.r54], fix=floor.r56)
        model_row(a, vent_model, versions="all", remedy="patches", patches_reference=f"DEMO-PATCH-{i}")
        advisories.add_source(a, source="other", reference=f"DEMO-NOTICE-{i}", by=None, today=TODAY)
    advisories.add_source(floor.a, source="other", reference="DEMO-NOTICE-A", by=None, today=TODAY)
    for i in range(4):
        p = Asset.objects.create(tag=f"P-1{i}", device_model=floor.model, department=dept)
        facts(p, floor.r52 if i % 2 else None)
        Asset.objects.create(tag=f"V-1{i}", device_model=vent_model, department=dept)
    update_wo(floor.d["P-5"], floor.r56)
    decide(floor.a, asset=floor.d["T-1"])
    decide(floor.a, release=floor.r52)
    release(floor.model, "5.8 (demo)")
    assert counts() == before


def test_tables_scroll_in_their_frame_and_the_css_block(client, signed_in, floor):
    decide(floor.a, release=floor.r52)
    signed_in("manager")
    for url, headers in (("/security/advisories/", {}), (f"/security/advisories/{floor.a.number}/", HX)):
        body = html(client.get(url, **headers))
        assert body.count("<table") == body.count('<div class="tbl-wrap">') >= 1, url
    css = Path("apps/web/static/web/cadence.css").read_text()
    start, end, prints = css.index("/* Slice 40: security advisories"), css.index("/* Slice 40: end */"), css.index(
        "/* Slice 11: printing app pages */")
    block = css[start:end]
    assert start < end < prints and block.count("{") == block.count("}") and "@media (max-width:760px)" in block
    assert css.count("{") == css.count("}")


# --- words ------------------------------------------------------------------------------------------------------------------------------

def test_never_words_on_every_door_and_constant(client, signed_in, floor):
    """rules.DECISION_NEVER_WORDS over every body, CSV, and toast this file read (a refusal echoing what was typed is never read into
    SEEN), every door here again, every upper-case constant of the views and forms, and every label the forms offer."""
    update_wo(floor.d["P-1"], floor.r56)
    decide(floor.a, asset=floor.d["P-2"])
    signed_in("manager")
    n = floor.a.number
    for url in (*read_urls(floor), f"/security/advisories/{n}/devices.csv"):
        r = client.get(url)
        SEEN.append(b"".join(r.streaming_content).decode() if r.streaming else r.content.decode())
    for url in (f"/security/advisories/{n}/", *modal_urls(floor), *approve_urls(floor), *update_door_urls(floor)):
        html(client.get(url, **MODAL))
    texts = list(SEEN)
    for module in (views_ad, forms_ad):
        texts += [v for k, v in vars(module).items() if k.isupper() and isinstance(v, str)]
    form = forms_ad.ModelForm(releases=[])
    for f in (form, forms_ad.RecordAdvisoryForm(today=TODAY), forms_ad.RevisionForm(), forms_ad.SourceForm(adding=True)):
        for field in f.fields.values():
            texts += [str(field.label or ""), str(field.help_text or "")]
            texts += [str(label) for _v, label in getattr(field.widget, "choices", ()) or ()]
            texts += [str(label) for _v, label in getattr(field, "choices", ()) or ()]
    assert len(SEEN) > 20
    hits = sorted({(w, t[:80]) for t in texts for w in never(t)})
    assert hits == []
