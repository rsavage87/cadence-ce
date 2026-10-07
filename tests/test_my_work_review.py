"""
Slice 24 review fixes on My work: how long work has waited on parts, and its note, come from its move into waiting (never from a
reassignment or a due-date move), longest first; signing in from the app's address (/login/?next=/) and accepting an invitation start
a technician on My work; a card's refused move re-fetches the list; a card's Start, Resume, and Take answer on <body>; cards and their
moves are named for a screen reader and findable after the list re-fetches.
"""
import json
from datetime import datetime, time, timedelta
from urllib.parse import urlsplit

from django.utils import timezone
from test_my_work_page import BODY, HX, TODAY, card, dana, floor, frozen, page, wo  # noqa: F401  (fixtures)

from apps.accounts import invitations, services
from apps.accounts.models import Role
from apps.credentials.models import Technician
from apps.workorders import my_work
from apps.workorders.models import WorkOrder, WorkOrderStatusHistory, WoStatus
from apps.workorders.services import assign, change_status


def _waited(w, days, note):
    """`w` moved into waiting `days` before the frozen TODAY with `note` (noon that day: the page counts days from TODAY, never the
    real clock)."""
    change_status(w, WoStatus.IN_PROGRESS)
    change_status(w, WoStatus.AWAITING_PARTS, note=note)
    noon = timezone.make_aware(datetime.combine(TODAY - timedelta(days=days), time(12)))
    WorkOrderStatusHistory.objects.filter(work_order=w, to_status=WoStatus.AWAITING_PARTS).update(created_at=noon)


def test_waiting_is_measured_from_the_move_into_waiting(client, dana, floor, make_user):  # noqa: F811
    other = Technician.objects.create(name="Sam Other")
    long_wait = wo(floor["pump"], tech=other)
    _waited(long_wait, 9, "Door latch kit, PO 4471")
    short_wait = wo(floor["pump2"], tech=dana["tech"])
    _waited(short_wait, 2, "Battery, PO 9")
    assign(WorkOrder.objects.get(pk=long_wait.pk), technician=dana["tech"])  # a reassignment: a same-status row with its own note
    body = page(client)
    assert "Waiting 9 d" in card(body, long_wait.number) and "Door latch kit, PO 4471" in card(body, long_wait.number)
    assert "Assigned to" not in card(body, long_wait.number)
    assert [w.number for w in my_work.groups(dana["user"], TODAY).waiting] == [long_wait.number, short_wait.number]  # longest first


def test_signing_in_from_the_apps_address_starts_a_technician_on_my_work(client, dana, make_user):  # noqa: F811
    client.logout()
    r = client.get("/")
    assert r.status_code == 302 and r["Location"] == "/login/?next=/"
    r = client.post("/login/?next=/", {"username": dana["user"].username, "password": "Test-Pass-2026-x"})
    assert r["Location"] == "/my-work/"
    client.logout()
    r = client.post("/login/?next=/?y=2026", {"username": dana["user"].username, "password": "Test-Pass-2026-x"})
    assert r["Location"] == "/?y=2026"  # a home page asked for something in particular is still followed
    client.logout()
    director = make_user("director")
    assert client.post("/login/?next=/", {"username": director.username, "password": "Test-Pass-2026-x"})["Location"] == "/"


def test_accepting_an_invitation_with_a_technician_profile_starts_on_my_work(client, ctx, mailoutbox):
    user = services.invite_user(ctx, email="ana@riverside.example", first_name="Ana", last_name="Diaz", role=Role.objects.get(slug="technician"),
                                create_technician=True)
    assert invitations.send_invitation(user)
    r = client.get(urlsplit(invitations.invitation_url(user)).path)
    done = client.post(r["Location"], {"new_password1": "Kestrel-Harbor-2031", "new_password2": "Kestrel-Harbor-2031"})
    assert done.status_code == 302 and done["Location"] == "/my-work/"


def test_a_refused_move_from_a_card_re_fetches_the_list(client, dana, floor):  # noqa: F811
    done = wo(floor["pump"], tech=dana["tech"], status=WoStatus.IN_PROGRESS)
    WorkOrder.objects.filter(pk=done.pk).update(status=WoStatus.COMPLETED)  # completed elsewhere meanwhile
    for url in (f"/work-orders/{done.number}/complete/?from=my_work", f"/work-orders/{done.number}/waiting/"):
        r = client.get(url, **HX)
        assert r.status_code == 200 and "wo-changed" in r.get("HX-Trigger", ""), url
    WorkOrder.objects.filter(pk=done.pk).update(status=WoStatus.CLOSED)
    r = client.get(f"/work-orders/{done.number}/labor/?from=my_work", **HX)
    assert "wo-changed" in r.get("HX-Trigger", "")
    r = client.get(f"/work-orders/{done.number}/complete/", **HX)  # from the drawer: nothing to re-fetch
    assert "wo-changed" not in r.get("HX-Trigger", "")


def test_a_cards_start_answers_on_body_and_the_card_is_named(client, dana, floor):  # noqa: F811
    w = wo(floor["pump"], tech=dana["tech"])
    body = page(client)
    html = card(body, w.number)
    assert f'id="mw-{w.number}"' in html and f'aria-label="Start {w.number}"' in html and f'aria-label="Log time {w.number}"' in html
    r = client.post(f"/work-orders/{w.number}/status/", {"to": "in_progress"}, **HX)
    events = json.loads(r["HX-Trigger"])
    assert events["wo-changed"]["target"] == "body" and events["toast"]["target"] == "body"
