"""
The PM completion window on the Settings screen and the API (slice 27, part C): the panel per role (choices and their examples for
Settings Edit, the windows in words for View, nothing for anyone else); Save behind its confirm, the toast naming the effect, the
default PM policy lines following the window (the policy panel swapped out of band); refusals in words keyed by field, all or nothing,
what was chosen kept; the warning when a PM policy line contradicts its window (swapped out of band by the policy's Save and Reset);
the API's round trip and refusals; another facility's window never shows or changes.
"""
import json

import pytest

from apps.facility import services as fs
from apps.facility.models import POLICY_DEFAULTS, PmWindow
from apps.pm import windows as W
from apps.tenants.context import tenant_context

HX = {"HTTP_HX_REQUEST": "true"}
URL = "/settings/pm-window/"
API = "/api/v1/settings/"
CONFIRM = 'hx-confirm="Every PM on-time figure, past months included, will be counted by this window."'
K = PmWindow


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def _toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


def _post(**changes):
    return {"pm_window_high": K.DUE_DATE, "pm_window_high_days": "", "pm_window_other": K.DUE_DATE, "pm_window_other_days": "", **changes}


def _panel(body: str) -> str:
    start = body.index('<div class="panel mt" id="set-pm-window">')
    return body[start:body.index('id="set-risk"', start) if 'id="set-risk"' in body[start:] else len(body)]


def _radio(group: str, value: str, checked: bool) -> str:
    field = f"pm_window_{group}"
    return f'<input type="radio" id="pmw-{group}-{value}" name="{field}" value="{value}"{" checked" if checked else ""}>'


def _windows():
    s = fs.get_settings()
    return (s.pm_window_high, s.pm_window_high_days, s.pm_window_other, s.pm_window_other_days)


# --- the panel, per role ------------------------------------------------------------------------------------------------------------

def test_an_editor_sees_both_groups_choices_with_their_examples(client, signed_in, ctx):
    signed_in("director")
    body = client.get("/settings/").content.decode()
    # its own panel, under the KPI targets
    assert body.index('id="set-targets"') < body.index('id="set-pm-window"') < body.index('id="set-risk"')
    panel = _panel(body)
    assert "<h2>PM on-time window</h2>" in panel and "When a completed PM counts as on time" in panel
    assert f'<form class="pmw-form" hx-post="{URL}" hx-target="#set-pm-window" hx-swap="outerHTML" hx-disinherit="hx-swap hx-target hx-confirm"' in panel
    assert CONFIRM in panel and "Save PM windows" in panel
    assert "<legend>Life support and high risk</legend>" in panel and "<legend>Medium and low risk</legend>" in panel
    for group in ("high", "other"):
        assert _radio(group, K.DUE_DATE, True) in panel
        for value in (K.DAYS_AFTER, K.DUE_MONTH, K.NEXT_MONTH):
            assert _radio(group, value, False) in panel
        assert f'<input id="pmw-{group}-days" name="pm_window_{group}_days" value="" inputmode="numeric"' in panel
    for label, example in [("By the due date", "A PM due Mar 10 is on time if done by Mar 10."),
                           ("Within a number of days after the due date", "With 14 days, a PM due Mar 10 is on time if done by Mar 24."),
                           ("By the end of the due month", "A PM due Mar 10 is on time if done by Mar 31."),
                           ("By the end of the month after the due month", "A PM due Mar 10 is on time if done by Apr 30.")]:
        assert panel.count(f"<span>{label}<small>{example}</small></span>") == 2, label
    assert '<span class="hint" id="pmw-high-days-help">1 to 45</span>' in panel
    assert "The schedule keeps the due date" in panel and "past months included" in panel
    assert '<div class="pmw-warn" id="set-pm-window-warn" aria-live="polite"></div>' in panel  # nothing to warn about
    assert "Changing settings needs Settings Edit access." not in panel


def test_a_saved_days_window_shows_its_days_and_example(client, signed_in, ctx):
    fs.update_settings(pm_window_high=K.DAYS_AFTER, pm_window_high_days=10)
    signed_in("director")
    panel = _panel(client.get("/settings/").content.decode())
    assert _radio("high", K.DAYS_AFTER, True) in panel and _radio("high", K.DUE_DATE, False) in panel and _radio("other", K.DUE_DATE, True) in panel
    assert 'name="pm_window_high_days" value="10"' in panel and 'name="pm_window_other_days" value=""' in panel
    assert "<span>Within a number of days after the due date<small>A PM due Mar 10 is on time if done by Mar 20.</small></span>" in panel
    assert "<small>With 14 days, a PM due Mar 10 is on time if done by Mar 24.</small>" in panel  # the other group has none saved


def test_a_viewer_sees_the_windows_in_words_and_no_form(client, signed_in, ctx):
    fs.update_settings(pm_window_high=K.DUE_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=21)
    signed_in("manager")  # Settings View
    body = client.get("/settings/").content.decode()
    panel = _panel(body)
    assert "<form" not in panel and "hx-post" not in panel and "hx-confirm" not in panel and 'type="radio"' not in panel
    assert ('<span class="lbl">Life support and high risk<span class="why">A PM due Mar 10 is on time if done by Mar 31.</span></span>'
            '<span class="pmw-now">By the end of the due month</span>') in panel
    assert ('<span class="lbl">Medium and low risk<span class="why">A PM due Mar 10 is on time if done by Mar 31.</span></span>'
            '<span class="pmw-now">Within 21 days after the due date</span>') in panel
    assert "Changing settings needs Settings Edit access." in panel and "hx-post" not in body


@pytest.mark.parametrize("role", ["manager", "technician", "analyst", "requester", "vendor"])
def test_only_settings_edit_saves_a_window(client, signed_in, ctx, role):
    signed_in(role)
    r = client.post(URL, _post(pm_window_high=K.DUE_MONTH), **HX)
    assert r.status_code == 403 and fs.get_settings()._state.adding  # nothing saved
    if role != "manager":
        assert client.get("/settings/").status_code == 403


def test_a_window_post_needs_post(client, signed_in, ctx):
    signed_in("director")
    assert client.get(URL).status_code == 405


# --- Save ---------------------------------------------------------------------------------------------------------------------------

def test_save_both_groups_and_the_default_policy_lines_follow(client, signed_in, ctx):
    kim = signed_in("director")
    r = client.post(URL, _post(pm_window_high=K.DUE_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=" 14 "), **HX)
    assert r.status_code == 200
    assert _toast(r) == ("PMs now count as on time by the end of the due month for life support and high risk and within 14 days after "
                         "the due date for medium and low risk, past months included. The default PM policy lines follow them")
    assert _windows() == (K.DUE_MONTH, None, K.DAYS_AFTER, 14)
    s = fs.get_settings()
    assert s.history.first().history_user == kim and s.history.count() == 1  # one audited save
    assert (s.policy_life_support, s.policy_medium_low) == ("OEM interval, complete by the end of the due month",
                                                            "AEM allowed, complete within 14 days after the due date")
    body = r.content.decode()
    assert body.lstrip().startswith('<div class="panel mt" id="set-pm-window">') and "<html" not in body
    panel = body[:body.index('id="set-policy"')]
    assert _radio("high", K.DUE_MONTH, True) in panel and _radio("other", K.DAYS_AFTER, True) in panel and 'name="pm_window_other_days" value="14"' in panel
    assert "<small>A PM due Mar 10 is on time if done by Mar 24.</small>" in panel  # the saved days' example
    # the policy panel comes along out of band, so it never shows the old lines to save back
    policy = body[body.index('<div class="panel" id="set-policy" hx-swap-oob="true">'):]
    assert 'name="policy_life_support" value="OEM interval, complete by the end of the due month"' in policy
    assert 'name="policy_medium_low" value="AEM allowed, complete within 14 days after the due date"' in policy


@pytest.mark.parametrize("post, message", [
    ({"pm_window_other": K.DUE_MONTH},
     "Medium and low risk PMs now count as on time by the end of the due month, past months included. The default PM policy line follows it"),
    ({"pm_window_high": K.NEXT_MONTH, "pm_window_other": K.NEXT_MONTH},
     "Every PM now counts as on time by the end of the month after the due month, past months included. The default PM policy lines follow them"),
    ({"pm_window_high": K.DAYS_AFTER, "pm_window_high_days": "1"},
     "Life support and high risk PMs now count as on time within 1 day after the due date, past months included. The default PM policy line follows it"),
])
def test_the_toast_names_the_effect(client, signed_in, ctx, post, message):
    signed_in("director")
    r = client.post(URL, _post(**post), **HX)
    assert _toast(r) == message


def test_a_line_the_facility_wrote_stays_and_the_policy_panel_is_left_alone(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(policy_life_support="Our words: within the scheduled month", policy_medium_low="AEM allowed, by the end of the due month")
    r = client.post(URL, _post(pm_window_high=K.DUE_MONTH, pm_window_other=K.DUE_MONTH), **HX)
    assert _toast(r) == "Every PM now counts as on time by the end of the due month, past months included"
    s = fs.get_settings()
    assert (s.policy_life_support, s.policy_medium_low) == ("Our words: within the scheduled month", "AEM allowed, by the end of the due month")
    assert 'id="set-policy"' not in r.content.decode()  # nothing on the policy panel changed


def test_an_unchanged_save_says_so(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(pm_window_other=K.DAYS_AFTER, pm_window_other_days=30)
    r = client.post(URL, _post(pm_window_other=K.DAYS_AFTER, pm_window_other_days="30"), **HX)
    assert _toast(r) == "The PM windows are unchanged" and 'id="set-policy"' not in r.content.decode()
    assert _windows() == (K.DUE_DATE, None, K.DAYS_AFTER, 30)


def test_days_go_only_with_the_days_choice(client, signed_in, ctx):
    """A number left in the hidden days box never rides along with another choice: the form sends no days, and the saved days go."""
    signed_in("director")
    fs.update_settings(pm_window_other=K.DAYS_AFTER, pm_window_other_days=30)
    r = client.post(URL, _post(pm_window_high_days="12", pm_window_other=K.NEXT_MONTH, pm_window_other_days="30"), **HX)
    assert r.status_code == 200 and _windows() == (K.DUE_DATE, None, K.NEXT_MONTH, None)
    assert 'name="pm_window_other_days" value=""' in r.content.decode()


def test_the_form_helper():
    from apps.web.forms_settings import pm_window_fields

    assert pm_window_fields({}) == {"pm_window_high": "", "pm_window_high_days": None, "pm_window_other": "", "pm_window_other_days": None}
    assert pm_window_fields({"pm_window_high": "days_after", "pm_window_high_days": " 7 ", "pm_window_other": "due_month",
                             "pm_window_other_days": "7"}) == {"pm_window_high": "days_after", "pm_window_high_days": "7",
                                                              "pm_window_other": "due_month", "pm_window_other_days": None}
    assert pm_window_fields({"pm_window_high": "days_after"})["pm_window_high_days"] == ""  # blank: the service asks for the days


# --- refusals ------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("days, message", [
    ("", "Say how many days after the due date (1 to 45)."),
    ("46", "Enter a whole number of days, 1 to 45."),
    ("0", "Enter a whole number of days, 1 to 45."),
    ("14.5", "Enter a whole number of days, 1 to 45."),
    ("two", "Enter a whole number of days, 1 to 45."),
    ("14 days", "Enter a whole number of days, 1 to 45."),
])
def test_bad_days_are_refused_under_their_field_and_nothing_is_saved(client, signed_in, ctx, days, message):
    signed_in("director")
    r = client.post(URL, _post(pm_window_high=K.DUE_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=days), **HX)
    assert r.status_code == 200 and _toast(r) == message
    assert fs.get_settings()._state.adding  # all or nothing: the high group's valid change did not save either
    body = r.content.decode()
    # what was chosen stays chosen, the days as typed, marked
    assert _radio("high", K.DUE_MONTH, True) in body and _radio("other", K.DAYS_AFTER, True) in body
    assert (f'name="pm_window_other_days" value="{days}" inputmode="numeric" autocomplete="off" '
            f'aria-describedby="pmw-other-days-help pmw-other-days-err" aria-invalid="true">') in body
    assert f'<span class="hint err" id="pmw-other-days-err">{message}</span>' in body and body.count('aria-invalid="true"') == 1
    assert 'id="set-policy"' not in body


@pytest.mark.parametrize("post", [_post(pm_window_high="soon"), {k: v for k, v in _post().items() if k != "pm_window_high"}])
def test_an_unknown_or_missing_choice_is_refused(client, signed_in, ctx, post):
    signed_in("director")
    r = client.post(URL, {**post, "pm_window_other": K.DUE_MONTH}, **HX)
    assert _toast(r) == "Choose when a PM counts as on time." and fs.get_settings()._state.adding
    body = r.content.decode()
    assert '<fieldset class="pmw-group" id="pmw-high" aria-describedby="pmw-high-err">' in body
    assert '<span class="hint err" id="pmw-high-err">Choose when a PM counts as on time.</span>' in body
    assert 'name="pm_window_high" value="due_date">' in body and _radio("other", K.DUE_MONTH, True) in body  # nothing chosen there; the other kept


# --- the policy warning ---------------------------------------------------------------------------------------------------------------

def test_a_policy_line_that_contradicts_its_window_is_warned_about(client, signed_in, ctx):
    fs.update_settings(policy_life_support="Complete within the scheduled month")  # the window is still the due date
    signed_in("director")
    panel = _panel(client.get("/settings/").content.decode())
    assert ('<div class="note warn">The life support and high risk PM policy line reads “Complete within the scheduled month”, but those '
            'PMs count as on time by the due date. Update the line under <a class="link" href="#set-policy_life_support">Maintenance policy</a>, '
            'or choose the window it describes. The survey binder lists it as a check.</div>') in panel
    assert panel.count('class="note warn"') == 1  # the medium and low line is the default: it follows its window
    # choosing the window the line describes clears the warning
    r = client.post(URL, _post(pm_window_high=K.DUE_MONTH), **HX)
    assert '<div class="pmw-warn" id="set-pm-window-warn" aria-live="polite"></div>' in r.content.decode()


def test_a_window_save_that_leaves_a_written_line_behind_warns(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(policy_medium_low="Complete by the due date")  # the facility's words, not the default
    r = client.post(URL, _post(pm_window_other=K.DAYS_AFTER, pm_window_other_days="10"), **HX)
    body = r.content.decode()
    assert "The medium and low risk PM policy line reads “Complete by the due date”, but those PMs count as on time within 10 days after the due date." in body
    assert 'href="#set-policy_medium_low"' in body


def test_the_policy_save_and_reset_update_the_warning_out_of_band(client, signed_in, ctx):
    signed_in("director")
    fs.update_settings(pm_window_high=K.DUE_MONTH)
    r = client.post("/settings/policy/", {**POLICY_DEFAULTS, "policy_life_support": "Complete by the due date, no grace"}, **HX)
    body = r.content.decode()
    assert _toast(r) == "Maintenance policy saved" and body.lstrip().startswith('<div class="panel" id="set-policy">')
    warn = body[body.index('<div class="pmw-warn" id="set-pm-window-warn" aria-live="polite" hx-swap-oob="true">'):]
    assert "reads “Complete by the due date, no grace”, but those PMs count as on time by the end of the due month." in warn
    r = client.post("/settings/policy/reset/", **HX)
    assert fs.get_settings().policy_life_support == "OEM interval, complete by the end of the due month"  # the default for its window
    assert '<div class="pmw-warn" id="set-pm-window-warn" aria-live="polite" hx-swap-oob="true"></div>' in r.content.decode()


def test_a_viewer_sees_the_warning_without_the_call_to_change_it(client, signed_in, ctx):
    fs.update_settings(pm_window_other=K.DUE_MONTH, policy_medium_low="AEM allowed, no grace")
    signed_in("manager")
    panel = _panel(client.get("/settings/").content.decode())
    assert "The medium and low risk PM policy line reads “AEM allowed, no grace”, but those PMs count as on time by the end of the due month." in panel
    assert 'See <a class="link" href="#set-policy_medium_low">Maintenance policy</a>.' in panel and "Update the line" not in panel


# --- the change log -----------------------------------------------------------------------------------------------------------------

def test_a_window_save_reads_in_the_change_log(client, signed_in, ctx, make_user):
    from apps.core import history

    signed_in("director")
    client.post(URL, _post(pm_window_other=K.DAYS_AFTER, pm_window_other_days="14"), **HX)
    client.post(URL, _post(pm_window_other=K.DAYS_AFTER, pm_window_other_days="21"), **HX)
    entries, _ = history.change_log(make_user("manager"), areas=["settings"])
    assert entries[0].who == "Director User"
    assert sorted((c.field, c.before, c.after) for c in entries[0].changes) == [
        ("PM on time, medium and low risk: days after the due date", "14 days", "21 days"),
        ("Policy: Medium and low risk", "AEM allowed, complete within 14 days after the due date", "AEM allowed, complete within 21 days after the due date")]


# --- the API ------------------------------------------------------------------------------------------------------------------------

def _api(client, data):
    return client.patch(API, data, content_type="application/json")


def test_the_api_round_trip(client, ctx, make_user):
    client.force_login(make_user("director"))
    got = client.get(API).json()
    assert (got["pm_window_high"], got["pm_window_high_days"], got["pm_window_other"], got["pm_window_other_days"]) == ("due_date", None, "due_date", None)
    r = _api(client, {"pm_window_high": "next_month", "pm_window_other": "days_after", "pm_window_other_days": 21})
    assert r.status_code == 200, r.content
    data = r.json()
    window = (data["pm_window_high"], data["pm_window_high_days"], data["pm_window_other"], data["pm_window_other_days"])
    assert window == ("next_month", None, "days_after", 21)
    assert data["policy_life_support"] == "OEM interval, complete by the end of the month after the due month"  # the default follows
    assert _api(client, client.get(API).json()).status_code == 200  # what GET returned goes back unchanged
    assert _windows() == (K.NEXT_MONTH, None, K.DAYS_AFTER, 21)
    assert _api(client, {"pm_window_other_days": 30}).json()["pm_window_other_days"] == 30  # days alone: the saved kind takes them
    assert _api(client, {"pm_window_other": "due_month"}).json()["pm_window_other_days"] is None  # a kind that takes none clears them


@pytest.mark.parametrize("data, field", [
    ({"pm_window_high": "days_after"}, "pm_window_high_days"),
    ({"pm_window_high": "soon"}, "pm_window_high"),
    ({"pm_window_high": ""}, "pm_window_high"),
    ({"pm_window_high": None}, "pm_window_high"),
    ({"pm_window_high": "days_after", "pm_window_high_days": 46}, "pm_window_high_days"),
    ({"pm_window_high": "days_after", "pm_window_high_days": 0}, "pm_window_high_days"),
    ({"pm_window_high": "days_after", "pm_window_high_days": "14.5"}, "pm_window_high_days"),
    ({"pm_window_high": "days_after", "pm_window_high_days": True}, "pm_window_high_days"),
    ({"pm_window_high": "days_after", "pm_window_high_days": "14 days"}, "pm_window_high_days"),
    ({"pm_window_high_days": 5}, "pm_window_high_days"),  # the saved kind takes no days
    ({"pm_window_other": "due_month", "pm_window_other_days": 30}, "pm_window_other_days"),
    ({"pm_window_high": "due_month", "pm_window_other": "days_after"}, "pm_window_other_days"),  # all or nothing: the high change waits too
])
def test_the_api_refuses_in_words_keyed_by_field(client, ctx, make_user, data, field):
    client.force_login(make_user("director"))
    r = _api(client, data)
    assert r.status_code == 400 and list(r.json()) == [field], r.content
    assert all(isinstance(m, str) and m for m in r.json()[field])
    assert fs.get_settings()._state.adding  # nothing saved


def test_the_api_needs_settings_edit_to_change_a_window(client, ctx, make_user):
    client.force_login(make_user("manager"))  # Settings View
    assert client.get(API).status_code == 200
    assert _api(client, {"pm_window_high": "due_month"}).status_code == 403
    client.force_login(make_user("technician"))
    assert client.get(API).status_code == 403 and _api(client, {"pm_window_high": "due_month"}).status_code == 403
    assert fs.get_settings()._state.adding


# --- tenant isolation -----------------------------------------------------------------------------------------------------------------

def test_another_facilitys_window_never_shows_or_changes(client, signed_in, ctx, other_tenant):
    with tenant_context(other_tenant):
        fs.update_settings(pm_window_high=K.NEXT_MONTH, pm_window_other=K.DAYS_AFTER, pm_window_other_days=33)
    signed_in("director")
    panel = _panel(client.get("/settings/").content.decode())
    assert _radio("high", K.DUE_DATE, True) in panel and 'value="33"' not in panel
    assert client.get(API).json()["pm_window_other"] == "due_date"
    client.post(URL, _post(pm_window_high=K.DUE_MONTH), **HX)
    assert W.windows().high == W.Window(K.DUE_MONTH)
    with tenant_context(other_tenant):
        assert W.windows() == W.Windows(W.Window(K.NEXT_MONTH), W.Window(K.DAYS_AFTER, 33))
