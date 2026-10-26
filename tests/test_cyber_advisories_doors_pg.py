"""
Slice 40 wave 2A, the advisory doors' GETs on PostgreSQL (locks critique B1): every refusal a GET door shows is a lock-free reader, so each
door opens outside any transaction (no ATOMIC_REQUESTS: FOR UPDATE there is an error on PostgreSQL, invisible on SQLite), whether its
refusal holds or not; the same doors take no lock on either database (a spy on select_for_update); and the doors render as the runtime
role under the row-level security policies.
"""
import pytest
from cyber_fixtures import make_advisory, update_wo
from pg_helpers import as_app_role, needs_postgres
from test_cyber_advisories_web import (  # noqa: F401 (fixtures)
    MODAL,
    approve_urls,
    decide,
    floor,
    html,
    modal_urls,
    read_urls,
    signed_in,
    update_door_urls,
)

from apps.cybersecurity import advisories


def every_get(floor) -> list:  # noqa: F811
    n = floor.a.number
    return [(url, {}) for url in (*read_urls(floor), f"/security/advisories/{n}/devices.csv")] + [
        (url, MODAL) for url in (f"/security/advisories/{n}/", *modal_urls(floor), *approve_urls(floor), *update_door_urls(floor))]


def other_doors(b) -> list:
    """An advisory where no refusal holds: two live sources, no model named, no decision."""
    from apps.cybersecurity.models import AdvisorySource

    src = AdvisorySource.objects.filter(advisory=b).order_by("created_at").first()
    base = f"/security/advisories/{b.number}"
    return [f"{base}/", f"{base}/edit/", f"{base}/sources/{src.pk}/remove/", f"{base}/not-applicable/", f"{base}/in-error/",
            f"{base}/models/", f"{base}/confirm/", f"{base}/close/"]


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_every_get_door_opens_outside_a_transaction(client, signed_in, floor):  # noqa: F811
    """No test transaction wraps the request here, as none wraps it in production."""
    decide(floor.a, release=floor.r52)  # a live decision: Not applicable, Entered in error, Remove model are refused
    update_wo(floor.d["P-1"], floor.r56)
    signed_in("manager")
    n = floor.a.number
    for url, headers in every_get(floor):
        r = client.get(url, **headers)
        assert r.status_code == 200, url
    for url in (f"/security/advisories/{n}/not-applicable/", f"/security/advisories/{n}/in-error/",
                f"/security/advisories/{n}/models/{floor.row.pk}/remove/", f"/security/advisories/{n}/close/"):
        assert 'data-blocker' in html(client.get(url, **MODAL)), url
    b = make_advisory(reference="DEMO-ADV-0002")
    advisories.add_source(b, source="other", reference="DEMO-ADV-0002-N", by=None)
    for url in other_doors(b):
        assert client.get(url, **MODAL).status_code == 200, url


@pytest.mark.django_db
def test_the_get_doors_take_no_lock(client, signed_in, floor, monkeypatch):  # noqa: F811
    """Both databases: every GET door here reads without select_for_update (SQLite has no FOR UPDATE to catch it)."""
    from django.db.models.query import QuerySet

    decide(floor.a, release=floor.r52)
    signed_in("manager")
    b = make_advisory(reference="DEMO-ADV-0002")
    advisories.add_source(b, source="other", reference="DEMO-ADV-0002-N", by=None)
    locked = []
    real = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        locked.append(self.model.__name__)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)
    for url, headers in every_get(floor) + [(url, MODAL) for url in other_doors(b)]:
        r = client.get(url, **headers)
        if r.streaming:
            b"".join(r.streaming_content)
        assert r.status_code == 200, url
    assert locked == []


@needs_postgres
@pytest.mark.django_db
def test_the_doors_under_the_policies(client, signed_in, floor):  # noqa: F811
    """As the runtime role: the tab, the page (drawer and full), both CSVs, and every modal render this facility's advisory."""
    signed_in("manager")
    n = floor.a.number
    doors = every_get(floor)  # read as the suite's role: a request leaves no tenant set behind it
    as_app_role()
    assert n in html(client.get("/security/advisories/")) and "DEMO-ADV-0001" in html(client.get(f"/security/advisories/{n}/", **MODAL))
    assert "P-1" in html(client.get(f"/security/advisories/{n}/", **MODAL))
    for url, headers in doors:
        r = client.get(url, **headers)
        if r.streaming:
            b"".join(r.streaming_content)
        assert r.status_code == 200, url
    rows = b"".join(client.get(f"/security/advisories/{n}/devices.csv").streaming_content).decode()
    assert "P-1" in rows and "T-1" in rows
