"""
A user's own notification preferences over the API (slice 20, part C): what the account menu's Notifications page sets
(apps.notifications.services). Self-service only: it always reads and sets the requesting user's own, and the emails go to their
account's address. Work orders View, as the page; scoped users (apps.workorders.scoping) are refused, as the page refuses them.

GET  /api/v1/notification-preferences/   {"email": the account's address ("" when it has none: nothing is sent),
                                          "assignments", "daily_digest", "contract_reminders": what the user gets (true or false;
                                          contract reminders are false for someone without Contracts Edit, whatever was saved),
                                          "contract_reminders_offered": whether they have Contracts Edit}. Until the user first
                                          saves, the defaults: assignments on, the digest off, contract reminders on.
PUT  /api/v1/notification-preferences/   Any of {"assignments", "daily_digest", "contract_reminders"}, each true or false; the others
                                          keep what they were (set_preferences, which creates the row on the first save). Returns the
                                          preferences as GET shows them. "email" and "contract_reminders_offered" may be sent back as
                                          GET showed them; any other value for them, an unknown field, or a value that is not true or
                                          false is a 400, and so is turning contract reminders on without Contracts Edit (sending
                                          false for them then changes nothing).
"""
from django.http import QueryDict
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.models import Level, Module
from apps.notifications import services as ns

from .base import ApiViewSet, _via_service

SHOWN = ("email", "contract_reminders_offered")  # what GET shows and no write sets
TRUE, FALSE = {"true", "1", "on", "yes"}, {"false", "0", "off", "no"}


def preferences(user) -> dict:
    """The user's preferences as GET shows them."""
    return {"email": user.email or "", **ns.shown(user), "contract_reminders_offered": ns.offered(user, "contract_reminders")}


def _flag(value):
    """true or false, as JSON sends them or a form spells them; None for anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in TRUE | FALSE:
        return value.strip().lower() in TRUE
    return None


class NotificationPreferenceViewSet(ApiViewSet):
    """The requesting user's own preferences, on the list's URL: GET reads them, PUT sets them."""

    write_level = Level.VIEW  # choosing one's own emails changes nothing of the facility's: the module's View is enough

    @property
    def module(self):
        """The module whose View lets someone choose here: Work orders, or Contracts for someone who gets only contract reminders
        (ns.refusal then checks the exact rule: Work orders View or Contracts Edit)."""
        user = self.request.user
        return Module.CONTRACTS if not user.has_level(Module.WORKORDERS, Level.VIEW) and user.has_level(Module.CONTRACTS, Level.VIEW) else Module.WORKORDERS

    @classmethod
    def as_view(cls, actions=None, **initkwargs):
        if actions and actions.get("get") == "list":
            actions = {**actions, "put": "choose"}  # one resource per user, on the list's URL: there is no id to address
        return super().as_view(actions, **initkwargs)

    def initial(self, request, *args, **kwargs):
        """After the levels and the scope (ModulePermission) and the facility (TenantAPIMixin): someone who is not one of this
        facility's people (a superuser looking at it) has no preferences here."""
        super().initial(request, *args, **kwargs)
        why = ns.refusal(request.user)
        if why:
            raise PermissionDenied(why)

    def list(self, request):
        return Response(preferences(request.user))

    def choose(self, request):
        data = request.data
        if isinstance(data, QueryDict):
            data = {k: data.get(k) for k in data}
        if not isinstance(data, dict):
            raise DRFValidationError({"detail": "Send a JSON object."})
        unknown = sorted(set(data) - set(ns.KINDS) - set(SHOWN))
        if unknown:
            raise DRFValidationError({"detail": f"Unknown fields: {', '.join(unknown)}. Set any of {', '.join(ns.KINDS)}: true or false."})
        now = preferences(request.user)
        errors = {f: [f"{f} is shown, not set here: send it as GET shows it, or leave it out."] for f in SHOWN if f in data and data[f] != now[f]}
        choices = {}
        for kind in ns.KINDS:
            if kind in data:
                flag = _flag(data[kind])
                if flag is None:
                    errors[kind] = ["Send true or false."]
                choices[kind] = flag
        if errors:
            raise DRFValidationError(errors)
        if not choices:
            raise DRFValidationError({"detail": f"Send any of {', '.join(ns.KINDS)}: true or false."})
        _via_service(ns.set_preferences, request.user, **choices)
        return Response(preferences(request.user))


def register(router):
    router.register("notification-preferences", NotificationPreferenceViewSet, basename="notificationpreference")
