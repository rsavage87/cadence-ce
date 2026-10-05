"""
What every API view refuses before it does anything (apps/api/tenancy.py, slice 19 review): a user with no facility (a superuser who
has not picked one, which a token never can) gets the plain 403 on every endpoint but the root, and a body holding text PostgreSQL
cannot store (a NUL, which the request's own check leaves to multipart fields, or a lone surrogate, which JSON allows) is a 400,
never a 500. Also the shapes of input that reached a service unchecked: a risk-score part too large to be a number, a custom report's
date period that is not text.
"""
import json

import pytest
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import User
from apps.api.tenancy import BAD_TEXT, NO_TENANT
from apps.equipment.models import Department, DeviceModel, RiskClass
from apps.reports.models import CustomReport

API = "/api/v1/"


@pytest.fixture
def root(db):
    user = User.objects.create_superuser(username="root", password="Test-Pass-2026-x", email="root@example.com")
    return {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}


@pytest.fixture
def monitor(ctx):
    return DeviceModel.objects.create(manufacturer="Philips", model="MX750", description="Patient monitor", category="Patient monitoring",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12)


def raw(client, method, url, text, **extra):
    """A JSON body sent byte for byte (json.dumps would escape what the test means to send)."""
    return getattr(client, method)(url, data=text, content_type="application/json", **extra)


@pytest.mark.parametrize("method, url, body", [
    ("post", "departments/", {"name": "ICU"}),
    ("post", "device-models/", {"manufacturer": "A", "model": "B", "description": "C", "category": "D", "risk_class": "low"}),
    ("get", "departments/", None), ("get", "work-orders/", None), ("get", "alert-matches/", None), ("get", "overview/", None),
    ("get", "reports/", None), ("get", "users/", None), ("get", "custom-reports/", None),
])
def test_a_user_with_no_facility_is_told_to_pick_one_everywhere(client, root, method, url, body):
    r = client.get(API + url, **root) if method == "get" else client.post(API + url, body, content_type="application/json", **root)
    assert r.status_code == 403 and r.json() == {"detail": NO_TENANT}, (url, r.status_code)
    assert not Department.unscoped.exists() and not DeviceModel.unscoped.exists()  # unscoped: nothing written anywhere


def test_the_api_root_still_answers_with_no_facility(client, root):
    assert client.get(API, **root).status_code == 200


def test_a_nul_in_a_multipart_body_is_a_400(client, make_user, monitor):
    """RejectNulMiddleware leaves multipart bodies to the fields that read them; the API reads them itself, so it checks."""
    client.force_login(make_user("technician"))
    r = client.post(f"{API}device-models/{monitor.pk}/aem/", {"interval_months": "24", "rationale": "Few\x00failures"})
    assert r.status_code == 400 and r.json() == {"detail": BAD_TEXT}


def test_a_lone_surrogate_in_a_json_body_is_a_400(client, make_user, ctx):
    client.force_login(make_user("analyst"))
    r = raw(client, "post", f"{API}custom-reports/", '{"name": "Pumps \\ud800", "source": "devices", "columns": ["tag"]}')
    assert r.status_code == 400 and r.json() == {"detail": BAD_TEXT}
    r = raw(client, "post", f"{API}custom-reports/", '{"name": "Pumps", "source": "devices", "columns": ["tag"], "filters": {"category": ["\\udfff"]}}')
    assert r.status_code == 400 and not CustomReport.objects.exists()


def test_a_risk_score_part_too_large_for_a_number_is_a_400(client, make_user, monitor):
    client.force_login(make_user("director"))
    r = raw(client, "put", f"{API}device-models/{monitor.pk}/risk-score/", '{"function": 1e400, "physical": 5, "maintenance": 3, "incidents": 0}')
    assert r.status_code == 400 and "function" in json.dumps(r.json())
    monitor.refresh_from_db()
    assert monitor.risk_score is None


@pytest.mark.parametrize("period", [["last_30"], {"a": 1}, 30])
def test_a_custom_reports_date_period_that_is_not_text_is_a_400(client, make_user, ctx, period):
    client.force_login(make_user("analyst"))
    body = {"name": "Repair cost", "source": "work_orders", "columns": ["number"], "filters": {"date": {"field": "completed", "period": period}}}
    r = client.post(f"{API}custom-reports/", body, content_type="application/json")
    assert r.status_code == 400 and r.json()["date"] and not CustomReport.objects.exists()


@needs_postgres
def test_the_guards_under_the_policies(client, root, make_user, monitor):
    """On PostgreSQL these were 500s: a save with no tenant, and text the database refuses."""
    client.force_login(make_user("technician"))
    as_app_role()
    r = client.post(f"{API}device-models/{monitor.pk}/aem/", {"interval_months": "24", "rationale": "Few\x00failures"})
    assert r.status_code == 400 and r.json() == {"detail": BAD_TEXT}
    client.logout()
    assert client.post(f"{API}departments/", {"name": "ICU"}, content_type="application/json", **root).status_code == 403
