"""
Facility time zones (slice 21 scaffold): working inside a facility works in its time zone (Tenant.timezone), so
timezone.localdate() is the facility's today: tenant_context(), the tenant middleware (web requests), and the API (token or session).
"""
from datetime import datetime
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.jobs.models import JobRun
from apps.tenants.context import tenant_context, zone_of

# 08:30 UTC on Oct 5 is Oct 5 in New York (04:30) and still Oct 4 in Honolulu (22:30).
NOW = datetime(2026, 10, 5, 8, 30, tzinfo=dt_timezone.utc)


@pytest.fixture
def honolulu(tenant):
    tenant.timezone = "Pacific/Honolulu"
    tenant.save(update_fields=["timezone"])
    return tenant


def test_a_facility_works_in_its_own_time_zone(honolulu, monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    assert timezone.localdate().isoformat() == "2026-10-05"  # the server's (TIME_ZONE, New York)
    with tenant_context(honolulu):
        assert timezone.get_current_timezone() == ZoneInfo("Pacific/Honolulu")
        assert timezone.localdate().isoformat() == "2026-10-04"
    assert timezone.localdate().isoformat() == "2026-10-05"  # restored on the way out


def test_a_name_that_is_not_a_zone_falls_back_to_the_servers(tenant, settings):
    tenant.timezone = "Mars/Olympus_Mons"
    assert zone_of(tenant) == ZoneInfo(settings.TIME_ZONE)
    assert zone_of(None) == ZoneInfo(settings.TIME_ZONE)


def test_web_requests_and_the_api_work_in_the_facilitys_zone(client, honolulu, make_user, monkeypatch):
    from rest_framework.authtoken.models import Token

    from apps.web import views

    seen = []
    real = views.overview_page

    def spy(*args, **kwargs):
        seen.append(timezone.get_current_timezone_name())
        return real(*args, **kwargs)

    monkeypatch.setattr(views, "overview_page", spy)
    user = make_user("director")
    client.force_login(user)
    client.get("/")
    token = Token.objects.create(user=user)
    client.logout()
    from apps.api import views_reports

    real_kpis = views_reports.overview_kpis

    def kpis(*args, **kwargs):
        seen.append(timezone.get_current_timezone_name())
        return real_kpis(*args, **kwargs)

    monkeypatch.setattr(views_reports, "overview_kpis", kpis)
    client.get("/api/v1/overview/", HTTP_AUTHORIZATION=f"Token {token.key}")
    assert len(seen) == 2 and set(seen) == {"Pacific/Honolulu"}
    assert timezone.get_current_timezone_name() != "Pacific/Honolulu"  # nothing leaks past the request


def test_a_job_runs_once_per_day_for_everyone_or_once_per_facility_and_day(db, tenant, other_tenant):
    day = datetime(2026, 10, 5).date()
    JobRun.objects.create(job="import_openfda", run_on=day)
    with pytest.raises(IntegrityError), transaction.atomic():
        JobRun.objects.create(job="import_openfda", run_on=day)
    JobRun.objects.create(job="generate_pm", facility=tenant, run_on=day)
    JobRun.objects.create(job="generate_pm", facility=other_tenant, run_on=day)
    with pytest.raises(IntegrityError), transaction.atomic():
        JobRun.objects.create(job="generate_pm", facility=tenant, run_on=day)


def test_no_code_reads_the_servers_clock():
    """Inside a facility today is timezone.localdate() (its zone is active); date.today(), datetime.now(), and datetime.today() are
    the server's day and clock. Comments and docstrings may name them; code may not."""
    import ast
    from pathlib import Path

    offenders = []
    for path in Path("apps").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if (node.func.value.id, node.func.attr) in {("date", "today"), ("datetime", "now"), ("datetime", "today")}:
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_a_facility_that_has_chosen_no_zone_works_in_the_servers(db, settings, monkeypatch, mailoutbox):
    """Review fix: a facility starts with no zone of its own (blank) and works in the server's (DJANGO_TIME_ZONE), as every facility
    did before it could choose; a server on the West Coast keeps its facilities on the West Coast."""
    from io import StringIO

    from django.core.management import call_command

    from apps.tenants.models import Tenant

    settings.TIME_ZONE = "America/Los_Angeles"
    call_command("bootstrap_tenant", "--name", "West", "--slug", "west", "--admin-email", "dir@west.example", stdout=StringIO())
    west = Tenant.objects.get(slug="west")
    assert west.timezone == "" and west.zone_name == "America/Los_Angeles"
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 10, 5, 6, 30, tzinfo=dt_timezone.utc))  # 23:30 Oct 4 in Los Angeles
    with tenant_context(west):
        assert timezone.localdate().isoformat() == "2026-10-04"


def test_settings_and_the_api_show_the_servers_zone_until_one_is_chosen(client, tenant, make_user):
    assert tenant.timezone == ""
    client.force_login(make_user("director"))
    body = client.get("/settings/").content.decode()
    assert "the facility has not chosen its own" in body
    assert client.get("/api/v1/settings/").json()["time_zone"] == "America/New_York"
    client.post("/settings/time-zone/", {"time_zone": "Pacific/Honolulu"}, HTTP_HX_REQUEST="true")
    tenant.refresh_from_db()
    assert tenant.timezone == "Pacific/Honolulu" and "the facility has not chosen its own" not in client.get("/settings/").content.decode()
    from apps.core import history

    with tenant_context(tenant):
        [entry] = [e for e in history.change_log(make_user("director", username="kim2@riverside.example"), areas=["facility"])[0]
                   if e.action == "changed"]
    assert [(c.field, c.before, c.after) for c in entry.changes][0][:2] == ("Time zone", "The server's")
