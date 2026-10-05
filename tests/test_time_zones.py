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
