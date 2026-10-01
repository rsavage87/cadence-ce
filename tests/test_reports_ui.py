"""Reports screen (slice 7): the page shell, its list and partial swap, the CSV download, the API, and server-side permission checks.
The reports themselves are tested next to their numbers (test_reports_cost.py, test_reports_fleet.py, test_reports_operations.py)."""
import pytest
from csvutil import csv_text

from apps.reports.services import REPORT_KEYS, REPORTS

HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "rep-body"}


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def test_catalog_matches_the_mock_in_order():
    assert REPORT_KEYS == ["cosr", "compliance", "mtbf", "replace", "spend", "contract", "tech", "recall"]
    assert all(r["template"] == f"web/_report_{r['key']}.html" for r in REPORTS)


def test_every_report_renders_as_a_page_with_its_list(client, signed_in, vent, pump):
    signed_in("director")
    for meta in REPORTS:
        r = client.get(f"/reports/{meta['key']}/")
        assert r.status_code == 200, meta["key"]
        body = r.content.decode()
        assert "Reports and analytics" in body and f"<h2>{meta['title']}</h2>" in body
        active = f'href="/reports/{meta["key"]}/" hx-get="/reports/{meta["key"]}/" hx-target="#rep-body" hx-swap="outerHTML" hx-push-url="true"'
        assert f'{active} aria-current="page"' in body
        assert f'href="/reports/{meta["key"]}.csv"' in body
        assert r.context["nav_active"] == "reports" and r.context["report"]["key"] == meta["key"]


def test_the_index_shows_the_first_report_and_unknown_keys_404(client, signed_in):
    signed_in("director")
    r = client.get("/reports/")
    assert r.status_code == 200 and r.context["report"]["key"] == "cosr"
    assert client.get("/reports/nope/").status_code == 404
    assert client.get("/reports/nope.csv").status_code == 404


def test_list_links_swap_the_body_only(client, signed_in):
    signed_in("director")
    r = client.get("/reports/spend/", **BODY)
    body = r.content.decode()
    assert "<html" not in body and "<h2>Repair spend trend</h2>" in body
    # htmx retitles the document only from a <title> at the fragment's root, so it comes before #rep-body, never inside it
    title = "<title>Repair spend trend · Reports · Riverside Regional</title>"
    assert body.lstrip().startswith(title) and body.index("<div id=\"rep-body\"") > body.index(title) and body.count("<title>") == 1
    assert "<title>" not in client.get("/reports/spend/").content.decode().split("<body")[1]  # never inside the full page's body
    assert '<nav class="panel rep-list" aria-label="Reports">' in body and 'role="tab' not in body
    assert r.templates[0].name == "web/_reports_body.html"


def test_csv_download_carries_the_reports_columns(client, signed_in, monkeypatch):
    from apps.reports import services as rs

    signed_in("analyst")
    monkeypatch.setattr(rs, "run_report", lambda key, today=None: {"columns": ["Model", "Ratio"], "rows": [["G5", 1.234], ["Pump, large", None]]})
    r = client.get("/reports/mtbf.csv")
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert r["Content-Disposition"].startswith('attachment; filename="cadence-mtbf-') and r["Content-Disposition"].endswith('.csv"')
    assert csv_text(r).splitlines() == ["Model,Ratio", "G5,1.23", '"Pump, large",']
    assert b"".join(client.get("/reports/mtbf.csv").streaming_content).startswith("\ufeff".encode())  # Excel reads it as UTF-8


@pytest.mark.parametrize("role, status", [("director", 200), ("manager", 200), ("technician", 200), ("analyst", 200), ("requester", 403), ("vendor", 403)])
def test_reports_need_reports_view(client, signed_in, role, status):
    signed_in(role)
    assert client.get("/reports/").status_code == status
    assert client.get("/reports/cosr.csv").status_code == status
    assert client.get("/api/v1/reports/").status_code == status
    assert client.get("/api/v1/reports/cosr/").status_code == (200 if status == 200 else 403)


def test_nav_lists_reports_only_for_roles_that_can_view_them(client, signed_in):
    signed_in("manager")
    keys = [i["key"] for i in client.get("/reports/").context["shell"]["nav"]]
    assert keys.index("recalls") < keys.index("reports") < keys.index("users") or "users" not in keys
    item = next(i for i in client.get("/reports/").context["shell"]["nav"] if i["key"] == "reports")
    assert item["url"] == "/reports/" and item["count"] is None
    client.logout()
    signed_in("requester")
    assert "reports" not in [i["key"] for i in client.get("/equipment/").context["shell"]["nav"]]


def test_overview_tiles_link_to_their_reports(client, signed_in, vent):
    signed_in("director")
    tiles = {t["label"]: t.get("url") for t in client.get("/").context["tiles"]}
    assert tiles["Fleet uptime"] == "/reports/mtbf/" and tiles["Mean time to repair"] == "/reports/mtbf/"
    assert tiles["Repair spend, month to date"] == "/reports/spend/" and tiles["Cost of service ratio, annualized"] == "/reports/cosr/"
    assert tiles["PM completion on time"] == "/pm/"  # the mock sends it to the PM schedule


def test_api_lists_the_catalog_and_serves_one_report(client, signed_in, monkeypatch):
    from apps.reports import services as rs

    signed_in("analyst")
    assert [r["key"] for r in client.get("/api/v1/reports/").json()] == REPORT_KEYS
    monkeypatch.setattr(rs, "run_report", lambda key, today=None: {"columns": ["A"], "rows": [[1, 3.14159, "x"]], "extra": object()})
    import apps.api.views as api_views

    monkeypatch.setattr(api_views, "run_report", rs.run_report)
    data = client.get("/api/v1/reports/tech/").json()
    assert data["key"] == "tech" and data["title"] == "Technician productivity" and data["columns"] == ["A"] and "extra" not in data
    assert data["rows"] == [[1, 3.14, "x"]]  # floats rounded like the CSV
    assert client.get("/api/v1/reports/nope/").status_code == 404
