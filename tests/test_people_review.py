"""
Slice 22 review fixes: an inviting facility's change log reads the same for an address used elsewhere and a new one even after a
failed first email and a reset request; a nameless linked account is never named by its username; a reset frees every facility's
username; choosing the current facility in the menu goes back to it; a tab left from before a switch goes to its screen (never a
record with the same number) whether by HTMX or a form post with its old CSRF token; a full save never writes a stale link.
"""
import smtplib
import uuid
from types import SimpleNamespace

from django.test import Client
from test_people import EMAIL, PASSWORD, account, invited, kim, lakeside  # noqa: F401  (fixtures)

from apps.accounts import invitations, services, signin
from apps.accounts.models import AccessEvent, Role, User
from apps.tenants.context import tenant_context
from apps.workorders import completion


def _mail_down(monkeypatch):
    def boom(self, fail_silently=False):
        raise smtplib.SMTPException("mail server unreachable")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)


def test_the_change_log_reads_the_same_after_a_failed_first_email_and_a_reset(kim, lakeside, client, monkeypatch, mailoutbox):  # noqa: F811
    a, _b = kim
    User.objects.filter(pk=_b.pk).delete()  # Kim works at Riverside only; Lakeside now invites her and a new address
    lee = account(lakeside, "director", username="lee@lakeside.example", email="lee@lakeside.example")
    technician = Role.unscoped.get(tenant=lakeside, slug="technician")  # unscoped: test setup
    with monkeypatch.context() as m:
        _mail_down(m)
        linked = services.invite_user(lakeside, email=EMAIL, first_name="Kim", last_name="Alvarez", role=technician, by=lee)
        fresh = services.invite_user(lakeside, email="ana@health.example", first_name="Ana", last_name="Diaz", role=technician, by=lee)
        assert not invitations.send_invitation(linked, by=lee) and not invitations.send_invitation(fresh, by=lee)
    assert linked.person == a.person and fresh.person is None
    for email in (EMAIL, "ana@health.example"):
        client.post("/password-reset/", {"email": email})
    for user in (linked, fresh):
        user.refresh_from_db()
        assert invitations.send_invitation(user, by=lee, resend=True)
    with tenant_context(lakeside):
        seen = {u.pk: [e.action for e in AccessEvent.objects.filter(user=u).order_by("at", "pk")] for u in (linked, fresh)}
    assert seen[linked.pk] == seen[fresh.pk] == ["invited", "invitation_resent"]


def test_a_nameless_linked_account_completing_a_failed_pm_is_named_by_its_email(tenant, lakeside):  # noqa: F811
    director = account(lakeside, "director", username=f"{EMAIL}@lakeside", person=uuid.uuid4())
    User.objects.filter(pk=director.pk).update(first_name="", last_name="")
    director.refresh_from_db()
    assert completion._requester(director, SimpleNamespace(assigned_to_id=None)) == EMAIL


def test_a_reset_frees_the_username_of_every_facility(kim, settings):  # noqa: F811
    a, b = kim
    settings.SIGNIN_MAX_FAILURES = 2
    for _ in range(2):
        signin.record_failure(b.username, None, b)  # wrong current passwords on Lakeside's Change password
    assert signin.is_locked(b.username, None)
    signin.clear_failures(a.username, a)  # the reset went to the landing account, Riverside
    assert not signin.is_locked(b.username, None) and not signin.person_locked(b)


def test_choosing_the_current_facility_or_all_facilities_in_the_menu_goes_there(kim, client):  # noqa: F811
    a, _b = kim
    client.force_login(a)
    assert client.post("/account/facility/", {"account": a.pk, "screen": "overview"})["Location"] == "/"
    assert client.post("/account/facility/", {"account": a.pk, "screen": "workorders"})["Location"] == "/work-orders/"
    assert client.post("/account/facility/", {"account": "all"})["Location"] == "/overview/all/"
    assert int(client.session["_auth_user_id"]) == a.pk


def test_a_stale_tab_goes_to_its_screen_never_a_record(kim, tenant):  # noqa: F811
    a, b = kim
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(a)
    page = browser.get("/work-orders/")
    old_token = page.cookies["csrftoken"].value if "csrftoken" in page.cookies else browser.cookies["csrftoken"].value
    assert 'data-facility="riverside"' in page.content.decode() and '"refreshOnHistoryMiss": true' in page.content.decode()
    # another tab switches to Lakeside (the switch renews the CSRF token)
    r = browser.post("/account/facility/", {"account": b.pk, "csrfmiddlewaretoken": old_token})
    assert r.status_code == 302 and int(browser.session["_auth_user_id"]) == b.pk
    stale = {"HTTP_HX_REQUEST": "true", "HTTP_X_CADENCE_FACILITY": str(tenant.pk), "HTTP_X_CSRFTOKEN": old_token,
             "HTTP_HX_CURRENT_URL": "http://testserver/work-orders/WO-26-0003/?status=open"}
    r = browser.get("/work-orders/WO-26-0003/", **stale)
    assert r.status_code == 204 and r["HX-Redirect"] == "/work-orders/"
    # the facility menu or Sign out on that old page: a form post with the old token
    r = browser.post("/account/facility/", {"account": a.pk, "csrfmiddlewaretoken": old_token},
                     HTTP_REFERER="http://testserver/equipment/CE-10001/")
    assert r.status_code == 302 and r["Location"] == "/equipment/" and int(browser.session["_auth_user_id"]) == b.pk
    # back in the old facility (A -> B -> A elsewhere): the header matches again, the token does not
    current = browser.get("/").cookies
    token = current["csrftoken"].value if "csrftoken" in current else browser.cookies["csrftoken"].value
    browser.post("/account/facility/", {"account": a.pk, "csrfmiddlewaretoken": token})
    r = browser.post("/work-orders/new/", {}, **stale)
    assert r.status_code == 204 and r["HX-Redirect"] == "/work-orders/"
    # a referer from another site, or none, goes to the Overview; someone signed out gets Django's refusal
    assert browser.post("/account/facility/", {"account": b.pk}, HTTP_REFERER="https://evil.example/work-orders/")["Location"] == "/"
    browser.logout()
    assert Client(enforce_csrf_checks=True).post("/login/", {"username": EMAIL, "password": PASSWORD}).status_code == 403


def test_a_print_link_from_a_stale_tab_offers_the_switch(kim, client, tenant):  # noqa: F811
    a, b = kim
    client.force_login(b)  # the browser is in Lakeside; the link came from a Riverside page (cadence.js tags it)
    body = client.get("/print/work-orders/WO-26-0001/?facility=riverside").content.decode()
    assert "This link is for Riverside Regional" in body


def test_a_full_save_never_writes_back_a_link_made_since_the_row_was_loaded(tenant, lakeside):  # noqa: F811
    a = account(tenant, "director", username=EMAIL)
    loaded = User.objects.get(pk=a.pk)  # a Change password request, about to hash
    technician = Role.unscoped.get(tenant=lakeside, slug="technician")  # unscoped: test setup
    linked = services.invite_user(lakeside, email=EMAIL, first_name="Kim", last_name="Alvarez", role=technician)
    pending_password = User.objects.get(pk=linked.pk).password
    loaded.set_password("Another-Pass-2026-y")
    loaded.save()
    a.refresh_from_db()
    assert a.person is not None and a.person == User.objects.get(pk=linked.pk).person
    assert User.objects.get(pk=linked.pk).password != pending_password  # shared with the person it has now: the invitation's link died
