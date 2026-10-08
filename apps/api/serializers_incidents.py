"""
Device incidents over the API (slice 28; apps/api/views_incidents.py). Showing only: every write goes through apps.incidents.services,
which validate, keep the clock, and record the history; IncidentViewSet parses each body itself and hands it over.

What an incident shows is for Incidents View alone (the endpoint's door; scoped users never reach it): the outcome and who was
affected, as categories, never who. No field is free text: the narrative and the deliberations are in the facility's event report,
which event_reference points to.
"""
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers

from apps.accounts import people
from apps.incidents import services as inc
from apps.incidents.models import Incident, IncidentHold
from apps.tenants.context import get_current_tenant


def _name(user):
    """A user as Cadence names people (str(user): the full name, else the email); null once the account is gone."""
    return str(user) if user is not None else None


def _day(value):
    return value.isoformat() if value is not None else None


class IncidentHoldSerializer(serializers.ModelSerializer):
    """One device held by the incident: when, the status it had then, its trips to the manufacturer, and how the hold ended.
    release_note (an incident read alone, and every write's answer: context["notes"]) says what keeps a released device out of
    service now (another open incident still holding it, its incoming inspection, a repair), "" when nothing does or the hold is
    active. A list leaves it out (it reads the device again for every released hold)."""

    asset_tag = serializers.CharField(source="asset.tag", read_only=True)
    status_before_label = serializers.CharField(source="get_status_before_display", read_only=True)
    active = serializers.BooleanField(read_only=True)
    with_manufacturer = serializers.BooleanField(read_only=True)
    release_label = serializers.CharField(source="get_release_display", read_only=True)
    released_by_name = serializers.SerializerMethodField()

    class Meta:
        model = IncidentHold
        fields = ["id", "asset", "asset_tag", "held_on", "status_before", "status_before_label", "active", "sent_on", "back_on",
                  "with_manufacturer", "released_on", "release", "release_label", "released_by_name"]
        read_only_fields = fields

    def get_released_by_name(self, hold):
        return _name(hold.released_by)

    def to_representation(self, hold):
        data = super().to_representation(hold)
        if self.context.get("notes"):
            data["release_note"] = inc.release_note(hold)
        return data


class IncidentSerializer(serializers.ModelSerializer):
    """An incident as its drawer shows it. Each choice comes with its words (`*_label`). The 10-work-day clock (`clock`, null while
    the incident has no due date): due, the work days left after today (0 when due today or past), overdue and soon (within
    inc.SOON_WORK_DAYS) while the decision or a required report is missing, pending (that), and recipients: who must get the report
    ("fda", "manufacturer"; an undecided clock: who would if it is reportable). required_recipients and reports_missing once it is
    decided reportable (a serious injury's report to the FDA counts for the manufacturer's); needs_decision while someone was harmed,
    or may have been, and nobody has decided. The investigation (work_order, its number and status; opened_work_order: the incident
    opened it rather than adopting the request it was reported as), and the holds (IncidentHoldSerializer). `url` is the incident's
    page on the web, naming its facility (?facility=<slug>, people.with_facility): numbers repeat across facilities, and a browser
    signed in to another one is offered the switch rather than that facility's incident of the same number."""

    asset_tag = serializers.CharField(source="asset.tag", read_only=True)
    device_model_name = serializers.SerializerMethodField()
    work_order_number = serializers.CharField(source="work_order.number", read_only=True, default=None)
    work_order_status = serializers.CharField(source="work_order.status", read_only=True, default=None)
    outcome_label = serializers.CharField(source="get_outcome_display", read_only=True)
    affected_label = serializers.CharField(source="get_affected_display", read_only=True)
    accessories_label = serializers.CharField(source="get_accessories_display", read_only=True)
    event_log_label = serializers.CharField(source="get_event_log_display", read_only=True)
    clock = serializers.SerializerMethodField()
    needs_decision = serializers.SerializerMethodField()
    required_recipients = serializers.SerializerMethodField()
    reports_missing = serializers.SerializerMethodField()
    basis_label = serializers.CharField(source="get_basis_display", read_only=True)
    decided_by_label = serializers.CharField(source="get_decided_by_display", read_only=True)
    recorded_by_name = serializers.SerializerMethodField()
    finding_label = serializers.CharField(source="get_finding_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    closed_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    holds = IncidentHoldSerializer(many=True, read_only=True)
    url = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = ["id", "number", "status", "status_label", "asset", "asset_tag", "device_model_name", "work_order", "work_order_number",
                  "work_order_status", "opened_work_order", "occurred_on", "aware_on", "outcome", "outcome_label", "affected", "affected_label",
                  "event_reference", "accessories", "accessories_label", "event_log", "event_log_label", "report_due_on", "clock",
                  "needs_decision", "reportable", "basis", "basis_label", "decided_on", "decided_by", "decided_by_label", "recorded_by_name",
                  "required_recipients", "fda_reported_on", "manufacturer_reported_on", "report_number", "reports_missing", "finding",
                  "finding_label", "closed_on", "closed_by_name", "created_by_name", "holds", "url", "created_at", "updated_at"]
        read_only_fields = fields

    def _today(self):
        today = self.context.get("today")
        if today is None:
            today = self.context["today"] = timezone.localdate()
        return today

    def get_device_model_name(self, incident):
        return str(incident.asset.device_model)

    def get_clock(self, incident):
        if incident.report_due_on is None:
            return None
        c = inc.clock(incident, self._today())
        return {"due": _day(c.due), "left": c.left, "overdue": c.overdue, "soon": c.soon, "pending": c.pending, "recipients": list(c.recipients)}

    def get_needs_decision(self, incident) -> bool:
        return inc.needs_decision(incident)

    def get_required_recipients(self, incident) -> list:
        return list(inc.required_recipients(incident))

    def get_reports_missing(self, incident) -> list:
        return list(inc.reports_missing(incident))

    def get_recorded_by_name(self, incident):
        return _name(incident.recorded_by)

    def get_closed_by_name(self, incident):
        return _name(incident.closed_by)

    def get_created_by_name(self, incident):
        return _name(incident.created_by)

    def get_url(self, incident) -> str:
        tenant = get_current_tenant()  # the request's facility (TenantAPIMixin): no query per incident
        if tenant is None or tenant.pk != incident.tenant_id:
            tenant = incident.tenant
        return people.with_facility(reverse("web:incident", args=[incident.number]), tenant)
