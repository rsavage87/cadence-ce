"""
Slice 39 skeptic fixes (the model panel's footer, review 39 S0, S1, S2): "Record decision for every <model> of ours" (model scope)

- is offered only while a device of ours of the model on site waits for a decision: a need a decision answers (past its support, or an
  end announced within 12 months), no decision governing it (its own, its release's, the model's), and no update answering it, as Needs
  attention reads it and on the release rows' own read: never for a model whose devices needing one are all governed (a model decision
  then would govern only devices needing none), never for rentals and loaners alone (each takes a decision of its own), and for devices
  with no release recorded, which no release row offers;
- carries the panel's filters in its address, so its save, and Review and renew from its modal (a model decision recorded meanwhile),
  answer with the panel as it was opened;
- reads the devices once (the panel's standings) and a fixed number of queries as devices and decisions grow.
"""
from datetime import timedelta

from cyber_fixtures import facts, release, update_wo
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from test_cyber_decisions_review39_web import seeded  # noqa: F401 (fixture)
from test_cyber_decisions_web import (  # noqa: F401 (fixtures)
    HX,
    KEEP_WITHOUT,
    MODAL,
    decide,
    floor,
    html,
    signed_in,
)

from apps.accounts.models import User
from apps.cybersecurity import decisions, rules
from apps.cybersecurity.models import AcceptedBy, Connects, DeviceSecurityDecision, SoftwareRelease
from apps.equipment.models import Asset, DeviceModel, Ownership
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
QUERY = "whose=ours&standing=updates_ended"


def footer(client, model, query: str = ""):
    """The model panel (HTMX) and its footer's Record decision address ("" when not offered)."""
    r = client.get(f"/security/models/{model.pk}/{query}", **HX)
    assert r.status_code == 200
    body = html(r)
    url = r.context["decide_model_url"]
    assert (f"Record decision for every {model.model} of ours" in body) is bool(url)
    return r, url


def attention_models(user, today) -> set:
    """The models Needs attention lists devices of ours of, under "no decision recorded" or ending (its rows per model and release,
    whose Record decision is the release's or the model's; a temporary device's rows name the device)."""
    tags = {tag for h in rules.needs_attention(today, user=user).headings if h.key in ("no_decision", "ending")
            for row in h.rows if row.asset is None for tag in row.tags}
    return set(Asset.objects.filter(tag__in=tags).values_list("device_model_id", flat=True))


def save(client, url, today):
    return client.post(url, {"reasons": ["security_updates"], "response": KEEP_WITHOUT, "accepted_by": AcceptedBy.CE_LEADER,
                             "decided_on": today.isoformat()}, **MODAL)


# --- S1: offered only while a device of ours waits -----------------------------------------------------------------------------------

def test_the_footer_is_offered_only_while_a_device_of_ours_waits_for_a_decision(client, signed_in, floor):  # noqa: F811
    kim = signed_in("director")
    today, model = floor.today, floor.model
    r, url = footer(client, model)
    assert url == f"/security/decisions/new/?model={model.pk}&from=panel" and model.pk in attention_models(kim, today)
    # 5.2's decision governs P-1, P-2, P-3; P-4 on 5.6 needs none; the rental T-1 waits on its own (Needs attention's temporary row)
    decide(release=floor.r52)
    assert footer(client, model)[1] == "" and model.pk not in attention_models(kim, today)
    assert "temporary" in {h.key for h in rules.needs_attention(today, user=kim).headings}
    # 5.6's end announced within 12 months: P-4 waits (ending); its release row and the footer both offer a decision
    SoftwareRelease.objects.filter(pk=floor.r56.pk).update(security_updates_end_on=today + 60 * DAY)
    r, url = footer(client, model)
    rows = {x["r"].version: x["decision"] for x in r.context["releases"]}
    assert url and rows["5.6 (demo)"]["record_url"] and model.pk in attention_models(kim, today)
    decide(asset=floor.d["P-4"])  # governed by its own
    assert footer(client, model)[1] == "" and model.pk not in attention_models(kim, today)
    # a device of ours with no release recorded, the model's profile past its security updates: no release row offers its decision
    SoftwareRelease.objects.filter(pk=floor.profile.pk).update(security_updates_end_on=today - 10 * DAY)
    p5 = Asset.objects.create(tag="P-5", device_model=model, department=floor.dept)
    r, url = footer(client, model)
    assert url and not any(x["decision"] and x["decision"]["record_url"] for x in r.context["releases"])
    assert model.pk in attention_models(kim, today)
    # an open update to a supported release answers it
    update_wo(p5, floor.r56)
    assert footer(client, model)[1] == "" and model.pk not in attention_models(kim, today)


def test_the_footer_never_offers_a_model_decision_for_rentals_and_loaners_alone(client, signed_in, floor):  # noqa: F811
    kim = signed_in("director")
    today = floor.today
    rented = DeviceModel.objects.create(manufacturer="Summit", model="Rentalonly", description="Infusion pump", category="Infusion pumps")
    release(rented, "", connects=Connects.WIRELESS, security_updates_end_on=today - 10 * DAY)
    t9 = Asset.objects.create(tag="T-9", device_model=rented, department=floor.dept, ownership=Ownership.RENTAL, owner="Summit Medical Rentals",
                              due_back_on=today + 10 * DAY)
    r, url = footer(client, rented)
    assert r.context["on_site"] == 1 and url == ""
    temporary = next(h for h in rules.needs_attention(today, user=kim).headings if h.key == "temporary")
    assert t9.tag in {tag for row in temporary.rows for tag in row.tags}  # its own decision, from Needs attention or its Security tab
    # one of ours arrives: it waits, the footer offers the model's decision
    Asset.objects.create(tag="R-1", device_model=rented, department=floor.dept)
    assert footer(client, rented)[1] == f"/security/decisions/new/?model={rented.pk}&from=panel"


def test_the_footer_is_for_approve_with_equipment_view_only(client, signed_in, floor):  # noqa: F811
    signed_in("technician")  # Device security Edit: the release rows' decisions read, never Record decision
    assert footer(client, floor.model)[1] == ""
    signed_in("manager")  # Device security Approve, Equipment Edit
    assert footer(client, floor.model)[1]


# --- S0, S2: the panel's filters kept --------------------------------------------------------------------------------------------------

def test_the_footers_save_answers_with_the_panel_as_it_was_opened(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today, model = floor.today, floor.model
    r, url = footer(client, model, f"?{QUERY}")
    assert r.context["narrowed"] and url == f"/security/decisions/new/?model={model.pk}&from=panel&{QUERY}"
    m = client.get(url, **MODAL)
    assert m.status_code == 200 and m.context["blocker"] == "" and f"Every {model.model} of ours" in html(m)
    r = save(client, url, today)
    assert r["HX-Retarget"] == "#drawer" and r.context["narrowed"]
    assert (r.context["f"].whose, r.context["f"].standing) == ("ours", "updates_ended")
    assert [s.asset.tag for s in r.context["devices"]] == ["P-1", "P-2", "P-3"]  # never P-4 or the rental T-1: the panel as it was opened
    d = DeviceSecurityDecision.objects.get(ended_on__isnull=True)
    assert (d.device_model, d.release, d.asset) == (model, None, None)
    assert r.context["decide_model_url"] == ""  # the model's decision governs every device of ours now


def test_review_and_renew_from_the_footers_modal_answers_with_the_panel_as_it_was_opened(client, signed_in, floor):  # noqa: F811
    signed_in("director")
    today, model = floor.today, floor.model
    url = footer(client, model, f"?{QUERY}")[1]
    old = decide(device_model=model)  # someone else records it meanwhile
    m = client.get(url, **MODAL)
    renew = m.context["renew_url"]
    assert decisions.IN_FORCE.format(number=old.number) in html(m)
    assert renew == f"/security/decisions/new/?supersedes={old.number}&from=panel&{QUERY}"
    assert f'hx-post="{renew}"' in html(client.get(renew, **MODAL))
    r = save(client, renew, today)
    assert r["HX-Retarget"] == "#drawer" and r.context["narrowed"]
    assert (r.context["f"].whose, r.context["f"].standing) == ("ours", "updates_ended")
    assert [s.asset.tag for s in r.context["devices"]] == ["P-1", "P-2", "P-3"]
    assert DeviceSecurityDecision.objects.get(ended_on__isnull=True).supersedes == old


# --- fixed queries, the devices read once ------------------------------------------------------------------------------------------------

def _read(client, model) -> tuple:
    """(the panel's footer address, its queries, and of them: those reading the devices with their register rows, the decisions, and
    the update records one by one (rules.update_responses; the campaigns' counts are an aggregate))."""
    with CaptureQueriesContext(connection) as q:
        r = client.get(f"/security/models/{model.pk}/", **HX)
    sql = [x["sql"] for x in q.captured_queries]
    devices = sum('FROM "equipment_asset"' in s and '"cybersecurity_devicesecurity"' in s for s in sql)
    decided = sum('FROM "cybersecurity_devicesecuritydecision"' in s for s in sql)
    responses = sum('FROM "cybersecurity_softwareupdate"' in s and "COUNT(" not in s for s in sql)
    return r.context["decide_model_url"], len(sql), (devices, decided, responses)


def test_the_footer_reads_the_devices_once_and_a_fixed_number_of_queries(client, signed_in, floor, dept):  # noqa: F811
    signed_in("director")
    today, model = floor.today, floor.model
    url, before, reads = _read(client, model)
    # the panel's standings, never read again; the model's live decisions and those that may govern the waiting devices; their updates
    assert url and reads == (1, 2, 1)
    old = release(model, "5.0 (demo)", security_updates_end_on=today - 400 * DAY)
    for i in range(9):
        a = Asset.objects.create(tag=f"PX-{i}", device_model=model, department=dept)
        if i == 8:
            continue  # no release recorded, and the profile says no end: no need
        facts(a, floor.r52 if i % 2 else old)
        if i % 3 == 0:
            decide(asset=a)
        elif i % 3 == 1:
            update_wo(a, floor.r56)
    for i in range(3):
        t = Asset.objects.create(tag=f"TX-{i}", device_model=model, department=dept, ownership=Ownership.RENTAL, owner="Summit Medical Rentals",
                                 due_back_on=today + 20 * DAY)
        facts(t, old)
    decide(release=old)
    url, after, reads = _read(client, model)
    assert url and after == before and reads == (1, 2, 1)
    # the model's own decision: nothing waits, and nothing is read but the release rows' decisions
    decide(device_model=model)
    url, governed, reads = _read(client, model)
    assert url == "" and governed < before and reads == (1, 1, 0)


# --- the seeded demo -------------------------------------------------------------------------------------------------------------------

def test_the_seeded_footers_agree_with_needs_attention(client, seeded):  # noqa: F811
    """Every seeded model with devices on site offers the footer exactly when Needs attention lists a device of ours of it under "no
    decision recorded" or ending. The seed lists none: its pumps and C-arms needing one are governed (the pumps' release decision, the
    C-arm's own) or answered by the update batch, so neither panel offers a model decision (review 39 S1 found both offering one)."""
    with tenant_context(seeded):
        today = timezone.localdate()
        kim = User.objects.get(username="kim@riverside.example")
        client.force_login(kim)
        listed = attention_models(kim, today)
        models = DeviceModel.objects.filter(pk__in=rules.on_site().values("device_model_id"))
        assert len(models) > 3
        for model in models:
            assert bool(footer(client, model)[1]) is (model.pk in listed), model.model
        for name in ("Alaris 8015 PCU", "Cios Spin"):
            model = DeviceModel.objects.get(model=name)
            needing = [s for s in rules.standings(rules.on_site().filter(device_model=model, ownership=Ownership.OWNED), today).values()
                       if (n := rules.support_need(s, today)) is not None and n.decidable]
            assert needing and footer(client, model)[1] == "" and model.pk not in listed, name
