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
