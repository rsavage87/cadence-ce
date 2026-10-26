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
from apps.equipment.models import Asset, DeviceModel, Ownership
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


# --- smoke ------------------------------------------------------------------------------------------------------------------------------

def test_every_door_opens_for_the_ce_manager(client, signed_in, floor):
    signed_in("manager")
    n = floor.a.number
    src = AdvisorySource.objects.get(advisory=floor.a)
    for url in ("/security/advisories/", "/security/advisories/export.csv", f"/security/advisories/{n}/devices.csv"):
        assert client.get(url).status_code == 200, url
    assert client.get(f"/security/advisories/{n}/").status_code == 200
    for url in (f"/security/advisories/{n}/", "/security/advisories/new/", f"/security/advisories/{n}/edit/",
                f"/security/advisories/{n}/sources/new/", f"/security/advisories/{n}/sources/{src.pk}/edit/",
                f"/security/advisories/{n}/sources/{src.pk}/revision/", f"/security/advisories/{n}/sources/{src.pk}/remove/",
                f"/security/advisories/{n}/models/", f"/security/advisories/{n}/models/?model={floor.model.pk}",
                f"/security/advisories/{n}/models/?os=1", f"/security/advisories/{n}/models/{floor.row.pk}/remove/",
                f"/security/advisories/{n}/confirm/", f"/security/advisories/{n}/not-applicable/", f"/security/advisories/{n}/close/",
                f"/security/advisories/{n}/reopen/", f"/security/advisories/{n}/in-error/"):
        r = client.get(url, **MODAL)
        assert r.status_code == 200, url
        assert never(html(r)) == [], (url, never(html(r)))
