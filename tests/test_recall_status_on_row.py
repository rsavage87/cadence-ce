"""
A recall match's status moves on its row as it is now (recalls.services.set_status): locked and re-read, the transition checked from the
stored status, then the person's Recalls level (the screen and the API pass it), and only the status fields written. A technician
(Recalls Edit) whose copy predates a manager's closure used to write over it: under review or in progress over "Reviewed, not affected",
closed_on cleared, a reopen that needs Approve. Now the move is refused in the transition's words, or as needing Approve, and the
closure stays. The recall work order batch still moves its match to in progress.
"""
import json

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from pg_helpers import needs_postgres
from test_incidents_review import pause_at, race

from apps.accounts.models import Level
from apps.recalls import permissions as rc_perms
from apps.recalls import services as rc
from apps.recalls.models import AlertMatch

REOPEN_WORDS = "FDA Z-TEST-1 is reviewed, not affected: reopening it needs Approve access."

S = AlertMatch.Status
HX = {"HTTP_HX_REQUEST": "true"}


def _closed_meanwhile(match, to=S.NOT_AFFECTED, note="Reviewed with the manufacturer: different lot"):
    """A manager's closure, committed after the copy `match` was read."""
    AlertMatch.objects.filter(pk=match.pk).update(status=to, closed_on=timezone.localdate(), disposition_note=note)


def _row(match):
    return AlertMatch.objects.get(pk=match.pk)


@pytest.mark.django_db
def test_a_move_the_stored_status_does_not_allow_is_refused_in_its_words(ctx, pump_recall):
    AlertMatch.objects.filter(pk=pump_recall.pk).update(status=S.UNDER_REVIEW)
    seen = _row(pump_recall)  # the technician's copy: under review
    _closed_meanwhile(seen)
    with pytest.raises(ValidationError) as e:
        rc.set_status(seen, S.IN_PROGRESS, level=Level.EDIT)
    assert e.value.messages == ["Cannot move FDA Z-TEST-1 from reviewed, not affected to action in progress."]
    row = _row(pump_recall)
    assert (row.status, row.closed_on, row.disposition_note) == (S.NOT_AFFECTED, timezone.localdate(), "Reviewed with the manufacturer: different lot")


@pytest.mark.django_db
def test_a_reopen_the_copy_never_showed_needs_approve_on_the_row(ctx, pump_recall):
    """needs action -> under review is Edit; on the row (reviewed, not affected) it is a reopen: Approve."""
    seen = _row(pump_recall)
    _closed_meanwhile(seen)
    with pytest.raises(PermissionDenied) as e:
        rc.set_status(seen, S.UNDER_REVIEW, level=Level.EDIT)
    assert str(e.value) == REOPEN_WORDS and _row(pump_recall).status == S.NOT_AFFECTED
    rc.set_status(seen, S.UNDER_REVIEW, level=Level.APPROVE)
    row = _row(pump_recall)
    assert (row.status, row.closed_on) == (S.UNDER_REVIEW, None) and seen.status == S.UNDER_REVIEW  # the caller's copy follows


@pytest.mark.django_db
def test_with_no_level_the_move_is_never_checked_for_one(ctx, pump_recall):
    """The recall work order batch and the seed pass no level."""
    seen = _row(pump_recall)
    _closed_meanwhile(seen)
    rc.set_status(seen, S.UNDER_REVIEW)
    assert _row(pump_recall).status == S.UNDER_REVIEW


@pytest.mark.django_db
def test_only_the_status_fields_are_written_onto_the_row_as_it_is(ctx, pump_recall):
    """A copy whose note predates the row's keeps nothing of its own but the move: the stored note stays when none is given."""
    AlertMatch.objects.filter(pk=pump_recall.pk).update(status=S.UNDER_REVIEW, disposition_note="Asked BD for the lot list")
    seen = _row(pump_recall)
    seen.disposition_note = "an older note on the caller's copy"
    rc.set_status(seen, S.IN_PROGRESS, level=Level.EDIT)
    assert (_row(pump_recall).status, _row(pump_recall).disposition_note) == (S.IN_PROGRESS, "Asked BD for the lot list")


def _check_then_close(monkeypatch, match):
    """can_transition as the door calls it (on the match it read), then the manager's closure before the move."""
    real = rc_perms.can_transition

    def check(user, from_status, to_status):
        allowed = real(user, from_status, to_status)
        _closed_meanwhile(match)
        return allowed

    monkeypatch.setattr(rc_perms, "can_transition", check)


@pytest.mark.django_db
def test_the_screens_move_on_a_match_closed_meanwhile_is_refused_for_edit(client, make_user, ctx, pump_recall, monkeypatch):
    client.force_login(make_user("technician"))  # Recalls View only in the default role; Edit through a role change below
    from apps.accounts.models import Module, Role

    Role.objects.get(slug="technician").set_levels({Module.RECALLS: Level.EDIT})
    _check_then_close(monkeypatch, pump_recall)
    r = client.post(f"/recalls/{pump_recall.pk}/status/", {"to": S.UNDER_REVIEW}, **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"] == {"value": REOPEN_WORDS}
    assert _row(pump_recall).status == S.NOT_AFFECTED


@pytest.mark.django_db
def test_the_apis_transition_on_a_match_closed_meanwhile_is_refused_for_edit(client, make_user, ctx, pump_recall, monkeypatch):
    from apps.accounts.models import Module, Role

    Role.objects.get(slug="technician").set_levels({Module.RECALLS: Level.EDIT})
    client.force_login(make_user("technician"))
    _check_then_close(monkeypatch, pump_recall)
    r = client.post(f"/api/v1/alert-matches/{pump_recall.pk}/transition/", {"status": S.UNDER_REVIEW}, content_type="application/json")
    assert r.status_code == 403 and r.json()["detail"] == "FDA Z-TEST-1 is reviewed, not affected: reopening it needs Approve access."
    assert _row(pump_recall).status == S.NOT_AFFECTED


@pytest.mark.django_db
def test_the_recall_work_order_batch_still_moves_its_match_to_in_progress(ctx, pump_recall, pump):
    batch = rc.create_recall_work_orders(_row(pump_recall))
    assert batch.created == 1 and _row(pump_recall).status == S.IN_PROGRESS


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_technicians_move_waits_for_a_managers_closure_and_is_refused(tenant, ctx, pump_recall, monkeypatch):
    """PostgreSQL: the manager's closure holds the match's row (stopped once it has written the note); the technician's move, its copy
    reading needs action, waits for it, then reads the row as closed: a reopen, refused as needing Approve. The closure stays."""
    seen = _row(pump_recall)
    pause = pause_at(monkeypatch, rc, "fmt_date", after=True)
    errors = race(tenant, pause, lambda: rc.set_status(_row(pump_recall), S.NOT_AFFECTED, level=Level.APPROVE),
                  lambda: rc.set_status(seen, S.UNDER_REVIEW, level=Level.EDIT))
    assert set(errors) == {"second"} and isinstance(errors["second"], PermissionDenied), errors
    assert _row(pump_recall).status == S.NOT_AFFECTED



# --- review fixes: the note on the row as it is, and the screen's answer to a page drawn before a closure ------------------------------

@pytest.mark.django_db
def test_the_note_is_written_on_the_row_as_it_is(ctx, pump_recall):
    """A copy read before a closure: the API's note edit writes the note only, never the copy's status or closed day over the closure."""
    seen = _row(pump_recall)
    _closed_meanwhile(seen)
    rc.set_note(seen, "Asked BD for the lot list")
    row = _row(pump_recall)
    assert (row.status, row.closed_on, row.disposition_note) == (S.NOT_AFFECTED, timezone.localdate(), "Asked BD for the lot list")
    assert seen.status == S.NOT_AFFECTED  # the caller's copy follows
    with pytest.raises(ValidationError) as e:
        rc.set_note(seen, "x", status=S.NEEDS_ACTION)
    assert e.value.message_dict == {"status": [rc.STATUS_BY_TRANSITION]}


@pytest.mark.django_db
def test_the_apis_note_edit_keeps_a_closure_made_after_it_read_the_match(client, make_user, ctx, pump_recall, monkeypatch):
    client.force_login(make_user("manager"))
    real = rc.set_note

    def closed_first(match, *args, **kwargs):
        _closed_meanwhile(match)
        return real(match, *args, **kwargs)

    monkeypatch.setattr(rc, "set_note", closed_first)
    r = client.patch(f"/api/v1/alert-matches/{pump_recall.pk}/", {"disposition_note": "Asked BD"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["status"] == S.NOT_AFFECTED and r.json()["disposition_note"] == "Asked BD"
    assert (_row(pump_recall).status, _row(pump_recall).closed_on) == (S.NOT_AFFECTED, timezone.localdate())


@pytest.mark.django_db
def test_the_screen_answers_a_page_drawn_before_a_closure_with_the_list_and_why(client, make_user, ctx, pump_recall):
    """The technician's page still offers Mark under review; the match was closed since: the list as it is now, and why."""
    from apps.accounts.models import Module, Role

    Role.objects.get(slug="technician").set_levels({Module.RECALLS: Level.EDIT})
    client.force_login(make_user("technician"))
    _closed_meanwhile(_row(pump_recall))
    r = client.post(f"/recalls/{pump_recall.pk}/status/", {"to": S.UNDER_REVIEW}, **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"] == {"value": REOPEN_WORDS}
    assert _row(pump_recall).status == S.NOT_AFFECTED


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_note_edit_waits_for_a_closure_and_keeps_it(tenant, ctx, pump_recall, monkeypatch):
    """PostgreSQL: the closure holds the match's row; the note edit, its copy reading needs action, waits for it, then writes the note
    only. The closure stays."""
    seen = _row(pump_recall)
    pause = pause_at(monkeypatch, rc, "fmt_date", after=True)
    errors = race(tenant, pause, lambda: rc.set_status(_row(pump_recall), S.NOT_AFFECTED, level=Level.APPROVE),
                  lambda: rc.set_note(seen, "Asked BD"))
    assert not errors, errors
    row = _row(pump_recall)
    assert (row.status, row.disposition_note) == (S.NOT_AFFECTED, "Asked BD")
