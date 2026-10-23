"""
Slice 39 review fixes (the core): decisions.refusal_for reads outside any transaction, as the Record decision modal calls it on every GET:
the decision to renew is read without a lock (FOR UPDATE outside a transaction is an error on PostgreSQL: Review and renew answered 500).
"""
import pytest
from pg_helpers import needs_postgres
from test_cyber_decisions_web import decide, floor, signed_in  # noqa: F401 (fixtures)

from apps.cybersecurity import decisions


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_review_and_renew_opens_outside_a_transaction(client, signed_in, floor):  # noqa: F811
    """No test transaction wraps the request here, as none wraps it in production (no ATOMIC_REQUESTS)."""
    old = decide(release=floor.r52)
    signed_in("manager")
    r = client.get(f"/security/decisions/new/?supersedes={old.number}", HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and old.number in r.content.decode()
    assert decisions.refusal_for(release=floor.r52, supersedes=old.number) is None  # in autocommit: no lock taken


@pytest.mark.django_db
def test_refusal_for_takes_no_lock(floor, monkeypatch):  # noqa: F811
    """Both databases: the read of the decision to renew is a plain read (SQLite has no FOR UPDATE to catch it)."""
    from django.db.models.query import QuerySet

    old = decide(release=floor.r52)
    locked = []
    real = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        locked.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    assert decisions.refusal_for(release=floor.r52, supersedes=old.number) is None
    assert decisions.refusal_for(release=floor.r52) is not None  # a live decision in force for the scope
    assert locked == []


@pytest.mark.django_db
def test_the_model_panel_offers_a_decision_for_every_device_of_ours_of_the_model(client, signed_in, floor):  # noqa: F811
    """Review F23: the model scope's door (build39 section 7: a release row's release or model scope). Approve with Equipment View sees
    it and its modal opens at model scope; Edit does not see it."""
    signed_in("manager")  # Device security Approve, Equipment Edit
    body = client.get(f"/security/models/{floor.model.pk}/", HTTP_HX_REQUEST="true").content.decode()
    assert f"Record decision for every {floor.model.model} of ours" in body and f"model={floor.model.pk}" in body
    r = client.get(f"/security/decisions/new/?model={floor.model.pk}&from=panel", HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and f"Every {floor.model.model} of ours" in r.content.decode()
    signed_in("technician")  # Device security Edit
    body = client.get(f"/security/models/{floor.model.pk}/", HTTP_HX_REQUEST="true").content.decode()
    assert "Record decision for every" not in body


@pytest.mark.django_db
def test_a_review_count_links_to_the_list_it_counts(client, signed_in, floor):  # noqa: F811
    """Review F20: Needs attention's "past their review date" counts only decisions that still hold a device here; its link narrows the
    list the same way (here=1), and the list says so, with the way to every one in that state."""
    from datetime import timedelta

    from apps.equipment.models import Asset, AssetStatus

    days = timedelta(days=1)
    held = decide(release=floor.r52, decided_on=floor.today - 400 * days, review_by=floor.today - 5 * days)
    retired = decide(asset=floor.d["P-1"], decided_on=floor.today - 400 * days, review_by=floor.today - 5 * days)
    Asset.objects.filter(pk=floor.d["P-1"].pk).update(status=AssetStatus.RETIRED)
    signed_in("manager")
    body = client.get("/security/decisions/").content.decode()
    assert "?state=review_due&amp;here=1" in body or "?state=review_due&here=1" in body
    listed = client.get("/security/decisions/?state=review_due&here=1").content.decode()
    table = listed.split('class="cy-tbl"')[1]  # the list itself (Needs attention above names the other under "no device here now")
    assert held.number in table and retired.number not in table and "Only the decisions whose scope still holds a device here" in listed
    everyone = client.get("/security/decisions/?state=review_due").content.decode().split('class="cy-tbl"')[1]
    assert held.number in everyone and retired.number in everyone


@pytest.mark.django_db
def test_renewing_a_decision_no_device_needs_offers_end(client, signed_in, floor):  # noqa: F811
    """Review F15: a decision whose scope needs none now (its device updated to a supported release) offers End, never a Review and
    renew that would be refused: on its page, and in the renew modal opened anyway."""
    from cyber_fixtures import facts

    old = decide(asset=floor.d["P-1"])
    facts(floor.d["P-1"], floor.r56)  # updated since: supported
    signed_in("manager")
    page = client.get(f"/security/decisions/{old.number}/", HTTP_HX_REQUEST="true").content.decode()
    assert f"/security/decisions/{old.number}/end/" in page and f"supersedes={old.number}" not in page
    modal = client.get(f"/security/decisions/new/?supersedes={old.number}", HTTP_HX_REQUEST="true").content.decode()
    assert f"End {old.number}" in modal and f"/security/decisions/{old.number}/end/" in modal


@pytest.mark.django_db
def test_end_never_says_why_a_decision_is_no_longer_needed(client, signed_in, floor):  # noqa: F811
    """Last skeptic: End is offered when each device of a release decision is under a decision of its own (GOVERNED_ELSEWHERE); its
    confirmation and the stored end's words claim nothing about the devices (never "updated, replaced, retired, or returned")."""
    from apps.cybersecurity import rules

    old = decide(release=floor.r52)
    for tag in ("P-1", "P-2", "P-3"):
        decide(asset=floor.d[tag])
    signed_in("manager")
    body = client.get(f"/security/decisions/{old.number}/end/", HTTP_HX_REQUEST="true").content.decode()
    assert f"End {old.number} as no longer needed." in body and "updated, replaced" not in body
    ended = decisions.end_decision(old, by=None)
    assert "updated, replaced" not in rules.end_words(ended)
