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
