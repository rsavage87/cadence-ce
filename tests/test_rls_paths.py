"""
Row-level security on the paths that start with no tenant (slice 10 review).

On PostgreSQL a tenant-scoped table only shows a tenant's rows while `app.tenant_id` is set (enable_rls); the tests run on
SQLite, where set_db_tenant does nothing, so a query made too early passes here and fails in production. The `rls` fixture
stands in for the policy: it records what set_db_tenant was last told and notes every query that touches a tenant-scoped
table while no tenant is set. Covered: loading the signed-in user on every request, signing in, the password-reset request,
accepting an invitation, the public portal, and the bootstrap and seed commands.
"""
import re
from io import StringIO
from urllib.parse import urlsplit

import pytest
from django.core.management import call_command
from django.db import connection

from apps.accounts import invitations, services
from apps.accounts.models import Role
from apps.tenants.management.commands.enable_rls import tenant_scoped_tables


class _Rls:
    def __init__(self):
        self.tenant = None
        self.violations = []
        tables = tenant_scoped_tables()
        self._pattern = re.compile(r'"(' + "|".join(map(re.escape, tables)) + r')"')

    def set_db_tenant(self, tenant):
        self.tenant = tenant

    def _guard(self, execute, sql, params, many, context):
        if self.tenant is None:
            m = self._pattern.search(sql)
            if m:
                self.violations.append(f"{m.group(1)}: {sql[:200]}")
        return execute(sql, params, many, context)

    def __enter__(self):
        self._wrapper = connection.execute_wrapper(self._guard)
        self._wrapper.__enter__()
        return self

    def __exit__(self, *exc):
        self._wrapper.__exit__(*exc)


@pytest.fixture
def rls(monkeypatch):
    """`with rls:` fails (via rls.violations) any tenant-table query made while no tenant is set, as the policy would."""
    guard = _Rls()
    monkeypatch.setattr("apps.tenants.context.set_db_tenant", guard.set_db_tenant)
    monkeypatch.setattr("apps.tenants.middleware.set_db_tenant", guard.set_db_tenant)
    return guard


def _role(tenant, slug):
    return Role.unscoped.get(tenant=tenant, slug=slug)  # unscoped: test setup, outside any tenant


def test_the_guard_catches_a_query_made_before_the_tenant_is_set(rls, tenant):
    with rls:
        list(Role.unscoped.filter(tenant=tenant))  # unscoped, no tenant set: what the policy would refuse
    assert rls.violations and "accounts_role" in rls.violations[0]


def test_signed_in_requests_load_the_user_without_touching_tenant_tables(client, rls, make_user):
    """The middleware loads the user before it sets the tenant, so that load must not join Role (or anything scoped)."""
    client.force_login(make_user("director"))
    with rls:
        for url in ["/", "/equipment/", "/work-orders/", "/users/", "/pm/", "/settings/", "/account/password/"]:
            assert client.get(url).status_code == 200, url
    assert rls.violations == []


def test_signing_in_and_out(client, rls, make_user):
    make_user("technician", username="tech@riverside.example")
    with rls:
        assert client.get("/login/").status_code == 200
        r = client.post("/login/", {"username": "TECH@riverside.example", "password": "Test-Pass-2026-x"})
        assert r.status_code == 302 and client.get(r["Location"]).status_code == 200
        assert client.post("/logout/").status_code == 302
    assert rls.violations == []


def test_password_reset_request_for_a_pending_invitation_and_a_password_account(client, rls, tenant, make_user, mailoutbox):
    """Signed out, no tenant: the reset request finds the accounts and, for a pending invitation, sends a fresh one whose
    email still names the role (read inside the invitee's facility, never from a role loaded before it)."""
    tech = make_user("technician", username="tech@riverside.example")
    tech.email = "tech@riverside.example"
    tech.save(update_fields=["email"])
    services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=_role(tenant, "technician"))
    with rls:
        for email in ["ana@riverside.example", "tech@riverside.example", "nobody@riverside.example"]:
            assert client.post("/password-reset/", {"email": email}).status_code == 302
    assert rls.violations == []
    assert sorted(m.to[0] for m in mailoutbox) == ["ana@riverside.example", "tech@riverside.example"]
    invite = next(m for m in mailoutbox if m.to == ["ana@riverside.example"])
    assert "as Technician" in invite.body


def test_accepting_an_invitation(client, rls, tenant, mailoutbox):
    user = services.invite_user(tenant, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=_role(tenant, "technician"))
    assert invitations.send_invitation(user)
    path = urlsplit(invitations.invitation_url(user)).path
    with rls:
        r = client.get(path)
        assert r.status_code == 302
        page = client.get(r["Location"])
        assert page.status_code == 200 and b"Set your password" in page.content
        done = client.post(r["Location"], {"new_password1": "Kestrel-Harbor-2031", "new_password2": "Kestrel-Harbor-2031"})
        assert done.status_code == 302 and client.get(done["Location"]).status_code == 200
    assert rls.violations == []


def test_public_portal(client, rls, tenant):
    with rls:
        assert client.get("/r/riverside/").status_code == 200
    assert rls.violations == []


def test_bootstrap_tenant_with_invite(rls, db, mailoutbox):
    with rls:
        call_command("bootstrap_tenant", "--name", "Lakeside General", "--slug", "lakeside", "--admin-email", "dir@lakeside.example", "--invite",
                     stdout=StringIO())
    assert rls.violations == []
    assert len(mailoutbox) == 1 and "as Director" in mailoutbox[0].body


def test_seed_demo(rls, db):
    with rls:
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_demo", stdout=(again := StringIO()))  # the second run sees the devices and stops
    assert rls.violations == []
    assert "already seeded" in again.getvalue()
