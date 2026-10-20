"""
The Fleet's filters (slice 37): one parser for every door that lists devices on site by them, the Device security screen (the Fleet, a
model's panel, their CSVs, the head figures' links) and the API's device-security list, so a head figure's link and the API, asked the
same query, answer the same devices. apps.web and apps.api both read this module; it imports neither.

The filters (query parameters):
- whose: "ours" or "temporary" (rentals, loaners, demo units on site): the Equipment screen's slugs (slice 29).
- dept: a department's id (the API also takes it as ?department=, its own name).
- category: a device model's category, as written.
- connected: "either" (connected here or able to connect, as disclosed: what the head figures count, rules.ON_SITE_KEYS), or one of
  rules' connection keys: "connected" (recorded as connected here), "can_connect" (as disclosed, no fact of its own), "not_connected",
  "not_recorded".
- standing: "updates_ended" (past the manufacturer's security updates or the model's support), "updates_end_announced",
  "security_not_recorded", "os_ended", "os_end_announced".
- norelease: "1", the devices with no release recorded.

The last three read each device's facts (rules.standings): `FleetFilters.matches` on a device's standing; `narrow` applies the first
three to a queryset of devices. `parse` is the screen's (an unknown value dropped, never a 500; a department or a category only from those
the screen offers); `parse_strict` is the API's (an unknown value refused, in words keyed by the parameter: FilterError).

`FleetFilters.within(...)`: the filters on screen narrowed to what a head figure counts, or None when the two cannot be written as one
query (the figure's link is then left off). A figure is counted under the filters on screen, so its link keeps every one of them and adds
only what the figure needs: the linked list is the devices the figure counted.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, fields, replace

from apps.equipment.models import Ownership
from apps.equipment.services import OWNED

from . import rules

WHOSE_OURS, WHOSE_TEMPORARY = "ours", "temporary"  # apps.web.forms' WHOSE_OURS / WHOSE_TEMPORARY (a test pins them equal)
WHOSE = (WHOSE_OURS, WHOSE_TEMPORARY)
EITHER = "either"  # connected here or able to connect: what the head figures count (rules.ON_SITE_KEYS)
CONNECTED_KEYS = {EITHER: rules.ON_SITE_KEYS, "connected": (rules.CONNECTED,), "can_connect": (rules.CAN_CONNECT,),
                  "not_connected": (rules.NOT_CONNECTED,), "not_recorded": (rules.CONNECTION_NOT_RECORDED,)}
STANDINGS = {"updates_ended": lambda s: s.security.key in rules.ENDED_KEYS,
             "updates_end_announced": lambda s: s.security.key == rules.UPDATES_END_ANNOUNCED,
             "security_not_recorded": lambda s: s.security.key == rules.SECURITY_NOT_RECORDED,
             "os_ended": lambda s: s.os.key == rules.OS_ENDED,
             "os_end_announced": lambda s: s.os.key == rules.OS_END_ANNOUNCED}
UPDATES_ENDED = "updates_ended"
NORELEASE = "1"

# The API's refusals, keyed by the parameter
WHOSE_WORDS = f"One of {', '.join(WHOSE)}."
DEPARTMENT_WORDS = "A department's id."
DEPARTMENT_TWICE = "Give the department once: ?department= or ?dept=, not two different ones."
CATEGORY_WORDS = "A device model's category, as written."
CONNECTED_WORDS = f"One of {', '.join(CONNECTED_KEYS)}."
STANDING_WORDS = f"One of {', '.join(STANDINGS)}."
NORELEASE_WORDS = "1 for the devices with no release recorded."


class FilterError(ValueError):
    """Unknown filter values (parse_strict): {parameter: [words]}."""

    def __init__(self, errors: dict):
        super().__init__(errors)
        self.errors = errors


@dataclass(frozen=True)
class FleetFilters:
    """The Fleet's filters (and a model panel's, carried from the Fleet): whose, department (its id), category, the device's connection
    as the register reads it, its standing, and "release not recorded". The last three read each device's facts (rules.standings)."""

    whose: str = ""
    dept: str = ""
    category: str = ""
    connected: str = ""
    standing: str = ""
    norelease: bool = False

    @property
    def any(self) -> bool:
        return any(getattr(self, f.name) for f in fields(self))

    @property
    def by_device(self) -> bool:
        """A filter on a device's facts: read through rules.standings, then kept or left out one by one."""
        return bool(self.connected or self.standing or self.norelease)

    def matches(self, s) -> bool:
        """A device's standing (rules.DeviceStanding) passes the facts' filters."""
        if self.connected and s.connection.key not in CONNECTED_KEYS[self.connected]:
            return False
        if self.standing and not STANDINGS[self.standing](s):
            return False
        return not (self.norelease and s.release is not None)

    def keeps(self, s) -> bool:
        """Every filter on one device's standing (a model panel's devices, already read)."""
        a = s.asset
        ours = a.ownership == Ownership.OWNED
        if (self.whose == WHOSE_OURS and not ours) or (self.whose == WHOSE_TEMPORARY and ours):
            return False
        if (self.dept and str(a.department_id) != self.dept) or (self.category and a.device_model.category.casefold() != self.category.casefold()):
            return False
        return self.matches(s)

    def params(self) -> dict:
        """The filters as query parameters (the panel's links and the head figures' carry them)."""
        out = {"whose": self.whose, "dept": self.dept, "category": self.category, "connected": self.connected, "standing": self.standing,
               "norelease": NORELEASE if self.norelease else ""}
        return {k: v for k, v in out.items() if v}

    def within(self, *, connected: str = "", standing: str = "", norelease: bool = False) -> FleetFilters | None:
        """These filters narrowed to what a head figure counts (its connection, its standing, release not recorded), or None when the
        two cannot be one query. A connection narrows to the connection keys both allow (none in common: None); a standing is one per
        device, so another standing on screen cannot be narrowed to the figure's (None); release not recorded only narrows."""
        changes: dict = {}
        if connected:
            keys = set(CONNECTED_KEYS[connected])
            if self.connected:
                keys &= set(CONNECTED_KEYS[self.connected])
            value = next((v for v, allowed in CONNECTED_KEYS.items() if set(allowed) == keys), None) if keys else None
            if value is None:
                return None
            changes["connected"] = value
        if standing:
            if self.standing not in ("", standing):
                return None
            changes["standing"] = standing
        if norelease:
            changes["norelease"] = True
        return replace(self, **changes)


def narrow(qs, f: FleetFilters):
    """Whose, department, and category on a queryset of devices (the facts' filters are FleetFilters.matches, on their standings)."""
    if f.whose == WHOSE_OURS:
        qs = qs.filter(OWNED)
    elif f.whose == WHOSE_TEMPORARY:
        qs = qs.exclude(OWNED)
    if f.dept:
        qs = qs.filter(department_id=f.dept)
    if f.category:
        qs = qs.filter(device_model__category__iexact=f.category)
    return qs


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def parse(params, *, departments=(), categories=()) -> FleetFilters:
    """The screen's filters from the address: an unknown choice is dropped, never a 500; a department id or a category is kept as asked
    (in any letter case; one matching nothing answers none, as the API's parse_strict). `categories`: the facility's, for their case."""
    def one_of(name, allowed):
        value = params.get(name, "")
        return value if value in allowed else ""

    dept = params.get("dept", "")
    dept = str(uuid.UUID(dept)) if _is_uuid(dept) else ""  # any department id: one with no device here answers none, as the API does
    typed = str(params.get("category", "")).strip()
    known = {c.casefold(): c for c in categories}
    category = known.get(typed.casefold(), typed) if typed and "\x00" not in typed and len(typed) <= 120 else ""
    return FleetFilters(whose=one_of("whose", WHOSE), dept=dept, category=category,
                        connected=one_of("connected", CONNECTED_KEYS), standing=one_of("standing", STANDINGS),
                        norelease=params.get("norelease") == NORELEASE)


def parse_strict(params) -> FleetFilters:
    """The API's filters: the screen's names and values (a head figure's link asks the same), the department also as ?department=. An
    unknown value is refused (FilterError, every problem at once, keyed by the parameter); a department or category matching nothing
    answers no devices."""
    errors: dict = {}

    def one_of(name, allowed, words):
        value = params.get(name) or ""
        if value and value not in allowed:
            errors[name] = [words]
            return ""
        return value

    whose = one_of("whose", WHOSE, WHOSE_WORDS)
    dept = ""
    given = {name: params.get(name) or "" for name in ("department", "dept")}
    named = {name: value for name, value in given.items() if value}
    if len(set(named.values())) > 1:
        errors["department"] = [DEPARTMENT_TWICE]
    elif named:
        name, value = next(iter(named.items()))
        if _is_uuid(value):
            dept = str(uuid.UUID(value))
        else:
            errors[name] = [DEPARTMENT_WORDS]
    category = params.get("category") or ""
    if "\x00" in category:
        errors["category"] = [CATEGORY_WORDS]
        category = ""
    connected = one_of("connected", CONNECTED_KEYS, CONNECTED_WORDS)
    standing = one_of("standing", STANDINGS, STANDING_WORDS)
    norelease = params.get("norelease") or ""
    if norelease not in ("", "0", NORELEASE):
        errors["norelease"] = [NORELEASE_WORDS]
    if errors:
        raise FilterError(errors)
    return FleetFilters(whose=whose, dept=dept, category=category, connected=connected, standing=standing, norelease=norelease == NORELEASE)
