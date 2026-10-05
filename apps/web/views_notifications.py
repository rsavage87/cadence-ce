"""
Notifications (slice 20, part C): the account menu's Notifications page, where people choose the emails Cadence sends them about
their own work (apps.notifications): work orders assigned to them, a daily digest, and contract reminders (offered only with Contracts
Edit). Self-service only: it always sets the signed-in user's own choices, and the emails go to their account's address, which the page
shows (or says there is none).

Scoped users (apps.workorders.scoping) are not offered it: the emails are about the facility's work (web_view refuses them, and the
service says so again). Each switch saves only itself as it is flipped (its own hx-post with a hidden 0 before it), so a tab opened
earlier never writes back a switch nobody touched in it; the POST answers with the switches re-rendered and a toast. A POST without
HTMX saves and comes back to the page.
"""
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.models import Level, Module
from apps.credentials.models import Technician
from apps.notifications import services as ns

from .decorators import web_view
from .htmx import toast

ROWS = (
    ("assignments", "When a work order is assigned to you: its number, device, department, priority, and due date, with a link."),
    ("daily_digest", "Each morning: your work orders due or overdue, and your PMs this week."),
    ("contract_reminders", "When a service contract is about to end."),
)


def _choices(post) -> dict:
    """The switches present in the post. A switch posts a hidden 0 with the checkbox's 1 when it is on, so on means a 1 is among
    its values, whatever their order; anything else goes to the service to refuse."""
    out = {}
    for kind in ns.KINDS:
        if kind in post:
            values = post.getlist(kind)
            out[kind] = True if "1" in values else False if set(values) == {"0"} else post.get(kind)
    return out


def _ctx(request) -> dict:
    user = request.user
    refused = ns.refusal(user)
    if refused:
        return {"refused": refused}
    now = ns.shown(user)
    rows = [{"kind": kind, "label": ns.LABELS[kind], "help": help_text, "on": now[kind], "offered": ns.offered(user, kind)}
            for kind, help_text in ROWS]
    return {"rows": rows, "email": user.email, "not_offered": ns.NOT_OFFERED,
            "is_technician": Technician.objects.filter(user=user, is_active=True).exists()}


def _message(choices: dict) -> str:
    if len(choices) == 1:
        kind, on = next(iter(choices.items()))
        return f"{ns.SHORT[kind]} {'on' if on else 'off'}"
    return "Notifications saved"


@web_view(Module.WORKORDERS, Level.VIEW)
def notifications(request):
    if request.method == "POST":
        choices = _choices(request.POST)
        try:
            ns.set_preferences(request.user, **choices)
            message = _message(choices) if choices else "Nothing to save"
        except ValidationError as e:
            message = e.messages[0]
        if not request.htmx:
            return redirect(reverse("web:notifications"))
        return toast(render(request, "web/_notifications_form.html", _ctx(request)), message)
    return render(request, "web/notifications.html", _ctx(request))
