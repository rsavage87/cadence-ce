"""
The device security register over the API (slice 37; apps/api/views_cybersecurity.py). Showing only: the API is read only, and every
write goes through apps.cybersecurity.services from the Device security screen and the device drawer's Security tab.

Every fact is read through apps.cybersecurity.rules, as the screen and the drawer read it: a release over its model's profile by group
(support, MDS2, disclosure; the facility's arrangement always the profile's), each group with its source in words ("this release", "the
model's disclosure, version not stated", "not recorded"); a device's connection ("Connected (wireless)", "Can connect (wireless), as
disclosed", ...); and the two standing lines, never merged: the manufacturer's security updates (or the model's support, slice 36) and the
OS vendor's published support, each {key, words, day}, judged on the facility's today (read once by the view: context["today"]). Choices go
out as their stored slugs with a "_label" beside each; days as YYYY-MM-DD.

Never nested into serializers.AssetSerializer or DeviceModelSerializer (tests/test_cyber_never.py pins both field sets): a device's facts
are their own endpoint, behind Device security View and Equipment View, never a scoped user's.

- release_data(release, ...): one row of GET software-releases/.
- device_data(standing, ...): one row of GET device-security/ (a rules.DeviceStanding).
"""
from django.urls import reverse
from rest_framework import serializers

from apps.accounts import people
from apps.cybersecurity import rules
from apps.cybersecurity.models import FACILITY_GROUP, GROUPS, Network
from apps.cybersecurity.services import CHOICES
from apps.equipment.models import Ownership
from apps.tenants.context import get_current_tenant


def _day(value):
    return value.isoformat() if value is not None else None


def label(choices, value: str) -> str:
    """A stored choice's words; "" when none is recorded."""
    return choices(value).label if value in choices.values else ""


def _facility_url(path: str, tenant_id) -> str:
    """A page on the web naming its facility (tags and model panels are the facility's own)."""
    tenant = get_current_tenant()
    return people.with_facility(path, tenant) if tenant is not None and tenant.pk == tenant_id else path


def standing_data(st: rules.Standing | None):
    return {"key": st.key, "words": st.words, "day": _day(st.day)} if st is not None else None


def _group(source: str, names, values) -> dict:
    out = {"source": source}
    for name in names:
        value = values(name)
        out[name] = _day(value) if name in rules.DATE_FIELDS else value
        if name in CHOICES:
            out[f"{name}_label"] = label(CHOICES[name], value)
    return out


def groups_data(facts: rules.Facts) -> dict:
    """The facts a release or device reads, by group, each with its source: {"support", "mds2", "disclosure", "facility"}."""
    return {group: _group(facts.sources[group], names, facts.get) for group, names in (*GROUPS.items(), ("facility", FACILITY_GROUP))}


def own_groups(release) -> dict:
    """A release entered in error as it was recorded (rules read it as nothing): its own fields, by group."""
    def source(names):
        return rules.THIS_RELEASE if any(getattr(release, n) not in (None, "") for n in names) else rules.NOT_RECORDED

    return {group: _group(source(names), names, lambda n: getattr(release, n))
            for group, names in (*GROUPS.items(), ("facility", FACILITY_GROUP))}


def model_data(dm) -> dict:
    return {"id": str(dm.pk), "manufacturer": dm.manufacturer, "model": dm.model, "description": dm.description, "category": dm.category,
            "url": _facility_url(reverse("web:cybersecurity_model", args=[dm.pk]), dm.tenant_id)}


def release_data(release, *, profile, devices: int, today) -> dict:
    """One release (or the model's profile) with its groups as a device on it reads them (the profile merged in by group), its two
    standing lines, and `devices`: the devices on site recorded on it (for a profile, its model's devices on site with no release
    recorded, which read only the profile). One entered in error reads its own fields and no standing (?removed=1 lists it)."""
    removed = release.removed_on is not None
    if removed:
        groups, security, os_line = own_groups(release), None, None
    else:
        facts = rules.facts(None, release) if release.is_profile else rules.facts(release, profile)
        groups = groups_data(facts)
        security, os_line = rules.release_standing(facts, release.device_model, today), rules.os_standing(facts, today)
    return {"id": str(release.pk), "device_model": model_data(release.device_model), "version": release.version,
            "profile": release.is_profile, "groups": groups, "security": standing_data(security), "os": standing_data(os_line),
            "devices": devices, "removed_on": _day(release.removed_on),
            "removed_by": str(release.removed_by) if removed and release.removed_by_id else None}


def device_data(s: rules.DeviceStanding) -> dict:
    """One device on site with what this facility knows about it and the words every door reads."""
    a, dm = s.asset, s.asset.device_model
    return {"id": str(a.pk), "tag": a.tag, "device_model": model_data(dm),
            "department": {"id": str(a.department_id), "name": a.department.name} if a.department_id else None, "room": a.room,
            "ownership": a.ownership, "ownership_label": label(Ownership, a.ownership),
            "release": {"id": str(s.release.pk), "version": s.release.version} if s.release is not None else None,
            "release_since": _day(s.release_since), "release_words": s.release_words,
            "network": s.network, "network_label": label(Network, s.network),
            "connection": {"key": s.connection.key, "words": s.connection.words, "here": s.connection.here},
            "security": standing_data(s.security), "os": standing_data(s.os), "groups": groups_data(s.facts),
            "checked_on": _day(s.checked_on), "checked_by": str(s.checked_by) if s.checked_by is not None else None,
            "url": _facility_url(reverse("web:asset", args=[a.tag]) + "?tab=security", a.tenant_id)}


class SoftwareReleaseSerializer(serializers.BaseSerializer):
    """GET software-releases/: context holds "today", "profiles" ({model id: profile row}) and "devices" ({release id: count})."""

    def to_representation(self, release):
        ctx = self.context
        return release_data(release, profile=ctx["profiles"].get(release.device_model_id), devices=ctx["devices"].get(release.pk, 0),
                            today=ctx["today"])


class DeviceSecuritySerializer(serializers.BaseSerializer):
    """GET device-security/: each a rules.DeviceStanding (rules.standings, a fixed number of queries for a page)."""

    def to_representation(self, standing):
        return device_data(standing)
