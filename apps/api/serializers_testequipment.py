"""
The CE department's test equipment over the API (slice 35; apps/api/views_testequipment.py). Showing only: the API is read only, and
every write goes through apps.testequipment.services from the Equipment screen's Test equipment tab (and, for which test equipment a work
order recorded, the work order's transition to completed and POST work-orders/{id}/test-equipment/).

Every standing is apps.testequipment.rules' words, judged on the facility's today (read once by the view: context["today"]): "Calibration
due Mar 31, 2027", "Calibration was due Aug 31, 2026", "No calibration on file", "Failed calibration Oct 3, 2026" ("Verification ..."
for an item whose certificates verify it). Never "calibrated", "in date", or "uncalibrated" as a status, and no slug for one either: the
list's ?standing= filter takes rules.FILTERS' keys.

- TestEquipmentListSerializer: an item as the list shows it (its standing from list_with_standing: item.standing).
- TestEquipmentSerializer: one item, adding its calibrations (every one, those entered in error marked) and the tolerance reviews (one per
  live calibration that needs one: its look-back window, the work in it now, the decision and its frozen count, work added since, and
  recheck progress). Which work orders are in a window only for Work orders View too (as the item's History shows the uses).
- test_equipment_of(...) and the work order's field (serializers.WorkOrderSerializer.test_equipment): each item a work order recorded with
  its words on its own day, the day of the work it was recorded on (services.judged_by_work_order: a page at once).
"""
from django.urls import reverse
from rest_framework import serializers

from apps.accounts import people
from apps.accounts.models import Level, Module
from apps.tenants.context import get_current_tenant
from apps.testequipment import permissions as perms
from apps.testequipment import rules
from apps.testequipment import services as te_services
from apps.testequipment.models import AsFound, Basis, Calibration, Mark, Reason, Result, TestEquipment


def _name(user):
    """A person as Cadence names people (str(user): the full name, else the email); null when none (or the account is gone)."""
    return str(user) if user is not None else None


def _day(value):
    return value.isoformat() if value is not None else None


def label(choices, value: str) -> str:
    """A stored choice's words; "" when none is recorded."""
    return choices(value).label if value in choices.values else ""


def page_url(item: TestEquipment) -> str:
    """The item's page on the web, naming its facility (tags are the facility's own: another may use the same one)."""
    tenant = get_current_tenant()  # the request's facility (TenantAPIMixin): no query per item
    if tenant is None or tenant.pk != item.tenant_id:
        tenant = item.tenant
    return people.with_facility(reverse("web:test_equipment_item", args=[item.tag]), tenant)


def shows_test_equipment(user) -> bool:
    """Whether `user` reads test equipment: Equipment View, never a scoped user (permissions.can_view, the screen's door)."""
    return user is not None and getattr(user, "is_authenticated", False) and perms.can_view(user)


def test_equipment_of(work_orders, user) -> dict | None:
    """{work order id: [{"id", "tag", "used_on", "words"}, ...] by tag} for `work_orders` (a page at once: services.judged_by_work_order, at most
    four queries), each item's words on its own day (rules.use_words); None when `user` does not read test equipment (no query).
    A cancelled work order records none, as its drawer and print say (review fix: uses left from before a reopen and a cancel count
    nowhere)."""
    from apps.workorders.models import WoStatus

    if not shows_test_equipment(user):
        return None
    rows = list(work_orders)
    found = te_services.judged_by_work_order([w for w in rows if w.status != WoStatus.CANCELLED])
    return {w.pk: [{"id": str(j.item.pk), "tag": j.item.tag, "used_on": _day(j.day), "words": j.words} for j in found.get(w.pk, ())]
            for w in rows}


test_equipment_of.__test__ = False  # pytest: not a test


class TestEquipmentListSerializer(serializers.ModelSerializer):
    """An item as the list shows it: its fields, who holds it, and its standing judged today (`item.standing`, set by
    services.list_with_standing; computed here for an item read without it)."""
    __test__ = False  # pytest: not a test class

    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    program_label = serializers.CharField(source="get_program_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    status_reason_label = serializers.SerializerMethodField()
    held_by = serializers.SerializerMethodField()
    held_by_name = serializers.SerializerMethodField()
    standing = serializers.SerializerMethodField()
    due_on = serializers.SerializerMethodField()
    last_calibrated_on = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = TestEquipment
        fields = ["id", "tag", "kind", "kind_label", "manufacturer", "model", "serial", "program", "program_label", "interval_months", "status",
                  "status_label", "status_reason", "status_reason_label", "status_since", "held_since", "held_by", "held_by_name", "standing",
                  "due_on", "last_calibrated_on", "url"]
        read_only_fields = fields

    def _standing(self, item) -> rules.Standing:
        found = getattr(item, "standing", None)
        if found is None:
            today = self.context["today"]
            found = item.standing = rules.standings([item], today)[item.pk]
        return found

    def get_status_reason_label(self, item) -> str:
        return label(Reason, item.status_reason)

    def get_held_by(self, item):
        return str(item.held_by_id) if item.held_by_id else None

    def get_held_by_name(self, item):
        return item.held_by.name if item.held_by_id else None

    def get_standing(self, item) -> str:
        """rules' words on today ("Calibration due Mar 31, 2027"), never a status slug."""
        return self._standing(item).words

    def get_due_on(self, item):
        """The governing certificate's due day (a passed one); null with none on file or after a failed one."""
        return _day(self._standing(item).due_on)

    def get_last_calibrated_on(self, item):
        """The governing certificate's day (the latest live one on or before today, passed or failed); null with none on file."""
        cal = self._standing(item).calibration
        return _day(cal.calibrated_on) if cal is not None else None

    def get_url(self, item) -> str:
        return page_url(item)


def calibration_data(cal: Calibration, entered: dict) -> dict:
    """One certificate as recorded: its days, provider and number, the accreditation mark as the certificate shows it, as found, adjusted,
    result, who recorded it and the facility day it was entered (entered_on: rules.entered_days, the "on file since" day), and whether it
    was marked entered in error (by whom, on which day: never deleted)."""
    return {"id": str(cal.pk), "calibrated_on": _day(cal.calibrated_on), "due_on": _day(cal.due_on), "provider": cal.provider,
            "certificate": cal.certificate, "accreditation": cal.accreditation, "accreditation_label": label(Mark, cal.accreditation),
            "as_found": cal.as_found, "as_found_label": label(AsFound, cal.as_found), "adjusted": cal.adjusted, "result": cal.result,
            "result_label": label(Result, cal.result), "needs_review": cal.needs_review, "recorded_by": _name(cal.recorded_by),
            "entered_on": _day(entered.get(cal.pk)), "entered_in_error": cal.entered_in_error,
            "entered_in_error_by": _name(cal.entered_in_error_by) if cal.entered_in_error else None,
            "entered_in_error_on": _day(cal.entered_in_error_on)}


def review_data(cal: Calibration, *, work_orders: bool) -> dict:
    """The tolerance review of one live calibration that needs one (services.review_summary, services.recheck_progress): the look-back
    window as it is now and the work in it, the state in words ("Not reviewed", "Reviewed Oct 10, 2026: ..."), the decision (its basis,
    quality record, day, who, and the window and count it was drawn with, frozen), the work added since, and recheck progress ("1 of 2
    rechecked"). `work_orders`: list the work orders in the window (Work orders View; each with used_on, the day of its use of the item,
    the one the window reads, beside its completed_on), else null."""
    summary = te_services.review_summary(cal)
    review = summary.review
    start, end = summary.window
    decision = None
    if review is not None:
        decision = {"basis": review.basis, "basis_label": label(Basis, review.basis), "quality_reference": review.quality_reference,
                    "decided_on": _day(review.decided_on), "decided_by": _name(review.decided_by), "window_start": _day(review.window_start),
                    "window_end": _day(review.window_end), "work_count": review.work_count}
    listed = None
    if work_orders:
        reviewed = rules.reviewed_ids(review) if review is not None else set()
        listed = [{"id": str(w.pk), "number": w.number, "completed_on": _day(w.completed_on), "used_on": _day(w.used_on),
                   "asset_tag": w.asset.tag, "in_review": str(w.pk) in reviewed} for w in summary.work]
    return {"calibration": str(cal.pk), "certificate": cal.certificate, "calibrated_on": _day(cal.calibrated_on),
            "state": rules.REVIEWED if review is not None else rules.NOT_REVIEWED, "words": summary.words, "window_start": _day(start),
            "window_end": _day(end), "work_count": len(summary.work), "decision": decision, "added_since_review": len(summary.added),
            "added_words": summary.added_words, "recheck": te_services.recheck_progress(cal).words, "work_orders": listed}


class TestEquipmentSerializer(TestEquipmentListSerializer):
    """One item: the list's fields, then its calibrations (newest first; context["calibrations"], read once by the view) and the tolerance
    reviews (one per live calibration that needs one, newest first)."""
    __test__ = False  # pytest: not a test class

    calibrations = serializers.SerializerMethodField()
    reviews = serializers.SerializerMethodField()

    class Meta(TestEquipmentListSerializer.Meta):
        fields = [*TestEquipmentListSerializer.Meta.fields, "calibrations", "reviews"]
        read_only_fields = fields

    def _calibrations(self, item) -> list:
        found = self.context.get("calibrations")
        if found is None:
            found = self.context["calibrations"] = list(Calibration.objects.filter(test_equipment=item)
                                                        .select_related("recorded_by", "entered_in_error_by"))
        return sorted(found, key=lambda c: (c.calibrated_on, c.created_at), reverse=True)

    def get_calibrations(self, item) -> list:
        cals = self._calibrations(item)
        entered = rules.entered_days(cals)
        return [calibration_data(c, entered) for c in cals]

    def get_reviews(self, item) -> list:
        user = getattr(self.context.get("request"), "user", None)
        lists = user is not None and user.has_level(Module.WORKORDERS, Level.VIEW)
        return [review_data(c, work_orders=lists) for c in self._calibrations(item) if c.needs_review and not c.entered_in_error]

