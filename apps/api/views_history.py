"""
The change log over the API (slice 20, part B): the Users and access tab's Change log as JSON (apps.core.history.change_log), across
the areas the user's role can view: the records' own histories, and the access events (invitations, roles, deactivations, what a
role may do) under "access".

GET /api/v1/change-log/   Users View. Read only. Refused (403) to scoped users (apps.workorders.scoping: the log is the whole
                          facility's, so it names no scoped_actions) and to a user with no facility.
    ?area=<key>           One area: devices, device_models, work_orders, labor, parts, contracts, procedures, aem, credentials,
                          roles, settings, custom_reports, or access, among those the user's role can view (the others are a 400).
    ?since=, ?until=      YYYY-MM-DD, the facility's local days, inclusive.
    ?who=<user id>        The changes one person in the facility made.
    ?limit=, ?after=      Newest first, `limit` entries (50 unless given, 1 to 200), after the cursor `after` (none: the newest).
                          Follow "next" for older pages: its cursor is the last entry returned, so changes saved meanwhile never
                          repeat or skip entries.
    Returns {"results": [...], "more": whether older entries follow, "limit", "after": the cursor this page read after (or null),
    "next": the next page's URL or null}.
    Each entry: at (ISO 8601 in the facility's time zone), who ("Cadence" for a nightly job or an import), who_id (null then),
    area, area_label, record (the record in a few words), url (its page in the web app, or null), action (added, changed, or
    deleted; an access change's own: invited, invitation_resent, role_changed, scope_changed, deactivated, reactivated,
    role_created, role_level_changed, role_scope_changed), action_label, reason, and changes [{field, before, after}]: each changed
    field's label and values in words ("before" is "" for a record's added values; an access change has one, field "", with its
    words in "after").
A parameter that is not one of these shapes is a 400 naming it.
"""
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.accounts.models import AccessEvent, Module, User
from apps.core.history import ACCESS, ALL_AREAS, change_log, parse_cursor, readable_areas
from apps.tenants.context import get_current_tenant

from .base import ApiViewSet, _parse_day

DEFAULT_LIMIT, MAX_LIMIT = 50, 200
ACCESS_ACTIONS = {str(label): value for value, label in AccessEvent.Action.choices}  # an access entry's words back to its slug


def _number(params, name, default, low, high, errors):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not low <= value <= high:
        errors[name] = [f"A whole number from {low:,} to {high:,}."]
        return default
    return value


def _entry(e, request) -> dict:
    access = e.area == ACCESS.key
    return {"at": timezone.localtime(e.at).isoformat(timespec="seconds"),"who": e.who, "who_id": e.who_id, "area": e.area, "area_label": e.area_label,
            "record": e.record, "url": request.build_absolute_uri(e.url) if e.url else None,
            "action": ACCESS_ACTIONS.get(e.action, e.action) if access else e.action, "action_label": e.action_label, "reason": e.reason,
            "changes": [{"field": c.field, "before": c.before, "after": c.after} for c in e.changes]}


class ChangeLogViewSet(ApiViewSet):
    module = Module.USERS
    http_method_names = ["get", "head", "options"]

    def _filters(self, request) -> dict:
        params, errors = request.query_params, {}
        area = params.get("area", "")
        readable = [a.key for a in readable_areas(request.user)]
        if area and area not in readable:
            errors["area"] = [f"Your role cannot view {ALL_AREAS[area].label}." if area in ALL_AREAS else
                              f"One of {', '.join(readable)}."]
        days = {}
        for name in ("since", "until"):
            raw = params.get(name)
            days[name] = _parse_day(raw) if raw else None
            if raw and days[name] is None:
                errors[name] = ["A date as YYYY-MM-DD."]
        if days["since"] and days["until"] and days["since"] > days["until"]:
            errors["until"] = ["On or after since."]
        who = params.get("who")
        if who not in (None, ""):
            try:
                who = int(who)
            except (TypeError, ValueError):
                who = None
            if who is None or not User.objects.filter(tenant=get_current_tenant(), pk=who).exists():  # User is not tenant-scoped
                errors["who"] = ["The id of a user in this facility."]
        else:
            who = None
        after = params.get("after") or None
        if after is not None and parse_cursor(after) is None:
            errors["after"] = ["A cursor from a page's \"next\"."]
        limit = _number(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT, errors)
        if errors:
            raise DRFValidationError(errors)
        return {"areas": [area] if area else None, "since": days["since"], "until": days["until"], "who": who, "after": after, "limit": limit}

    def list(self, request):
        f = self._filters(request)
        entries, following = change_log(request.user, **f)
        next_url = None
        if following is not None:
            query = request.query_params.copy()
            query.pop("offset", None)
            query["after"] = following
            next_url = request.build_absolute_uri(f"{request.path}?{query.urlencode()}")
        return Response({"results": [_entry(e, request) for e in entries], "more": following is not None, "limit": f["limit"],
                         "after": f["after"], "next": next_url})


def register(router):
    router.register("change-log", ChangeLogViewSet, basename="change-log")
