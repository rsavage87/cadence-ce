"""
The program's annual evaluation over the API (slice 32; apps/api/views_evaluations.py). Showing only: the API is read-only this slice,
and every write goes through apps.evaluations.services from the screens.

The list (EvaluationListSerializer) carries no figures and no text: the number and version, the months, the status, the days it was
sent to committee and approved, and the committees that approved it. One evaluation (EvaluationSerializer) adds the text Clinical
Engineering wrote (models.NARRATIVE, the inventory review, the plan reviewed), the objectives, the approvals in full, and the figures
cut to the reader (apps.evaluations.indicators.cut: what their role may not see is removed and named in `left_out`; a reader without
Reports View gets no figures at all). The text is never anything else's: no CSV, email, digest, custom report, or All facilities.
"""
from django.urls import reverse
from rest_framework import serializers

from apps.accounts import people
from apps.evaluations import indicators
from apps.evaluations import services as ev_services
from apps.evaluations.models import NARRATIVE, Body, EvaluationApproval, ObjectiveKind, ProgramEvaluation, Status
from apps.tenants.context import get_current_tenant


def _name(user):
    """A user as Cadence names people (str(user): the full name, else the email); null once the account is gone."""
    return str(user) if user is not None else None


def _day(value):
    return value.isoformat() if value is not None else None


def committee(approval: EvaluationApproval) -> str:
    """The committee in words: another committee by the name recorded."""
    return approval.body_name if approval.body == Body.OTHER and approval.body_name else Body(approval.body).label


def page_url(evaluation: ProgramEvaluation, name: str = "web:evaluation_version") -> str:
    """This version's page on the web (or its print: web:evaluation_version_print), naming its facility: numbers repeat across
    facilities."""
    tenant = get_current_tenant()  # the request's facility (TenantAPIMixin): no query per evaluation
    if tenant is None or tenant.pk != evaluation.tenant_id:
        tenant = evaluation.tenant
    return people.with_facility(reverse(name, args=[evaluation.number, evaluation.version]), tenant)


class EvaluationListSerializer(serializers.ModelSerializer):
    """An evaluation as the list shows it: no figures, no text."""

    label = serializers.CharField(read_only=True)
    period_label = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    amends_label = serializers.CharField(source="amends.label", read_only=True, default=None)
    approvals = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = ProgramEvaluation
        fields = ["id", "number", "version", "label", "period_start", "period_end", "period_label", "status", "status_label", "sent_on",
                  "approved_on", "amends", "amends_label", "approvals", "url"]
        read_only_fields = fields

    def get_period_label(self, ev) -> str:
        return ev_services.period_label(ev.period_start, ev.period_end)

    def get_approvals(self, ev) -> list:
        """Each committee's approval in meeting order: which committee and the day it met."""
        return [{"body": a.body, "committee": committee(a), "meeting_on": _day(a.meeting_on)} for a in ev.approvals.all()]

    def get_url(self, ev) -> str:
        return page_url(ev)


class EvaluationSerializer(EvaluationListSerializer):
    """One evaluation: the list's fields, then its text, objectives, approvals in full, and the figures cut to the reader
    (context["request"].user)."""

    text = serializers.SerializerMethodField()
    plan = serializers.SerializerMethodField()
    objectives = serializers.SerializerMethodField()
    approvals = serializers.SerializerMethodField()
    figures = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    print_url = serializers.SerializerMethodField()

    class Meta(EvaluationListSerializer.Meta):
        fields = EvaluationListSerializer.Meta.fields + ["text", "inventory_reviewed_on", "plan", "objectives", "figures", "created_by_name",
                                                         "print_url", "created_at", "updated_at"]
        read_only_fields = fields

    def get_text(self, ev) -> list:
        """The narrative in NARRATIVE's order: [{field, label, text}]."""
        return [{"field": field, "label": label, "text": getattr(ev, field)} for field, label, _help in NARRATIVE]

    def get_plan(self, ev) -> dict:
        """The written plan reviewed: a reference to the facility's own plan, never the plan."""
        return {"title": ev.plan_title, "version": ev.plan_version, "effective_on": _day(ev.plan_effective_on)}

    def get_objectives(self, ev) -> dict:
        """This period's objectives with their results ("review"), and the next period's ("next"), each in order."""
        review, upcoming = [], []
        for obj in ev.objectives.all():
            if obj.kind == ObjectiveKind.REVIEW:
                review.append({"id": str(obj.id), "position": obj.position, "text": obj.text, "target": obj.target, "result": obj.result,
                               "result_label": obj.get_result_display() if obj.result else "", "comment": obj.comment,
                               "carried": obj.carried_from_id is not None})
            else:
                upcoming.append({"id": str(obj.id), "position": obj.position, "text": obj.text, "target": obj.target})
        return {"review": review, "next": upcoming}

    def get_approvals(self, ev) -> list:
        """Every approval in meeting order; `approves` marks the one that approved the evaluation (the first recorded)."""
        found = list(ev.approvals.all())
        first = min(found, key=lambda a: (a.created_at, str(a.id))) if found else None
        return [{"id": str(a.id), "body": a.body, "body_label": a.get_body_display(), "committee": committee(a), "meeting_on": _day(a.meeting_on),
                 "chair_name": a.chair_name, "chair_title": a.chair_title, "minutes_reference": a.minutes_reference,
                 "recorded_by_name": _name(a.recorded_by), "approves": a is first} for a in found]

    def get_figures(self, ev):
        """The figures as taken (null before any), cut to the reader: the snapshot's shape (indicators.snapshot) with what their role
        may not see removed, `left_out` naming it, `complete` (nothing left out), and `met` of `judged` counted over what is shown."""
        snap = ev.snapshot
        if snap is None:
            return None
        seen = indicators.cut(snap.data, self.context["request"].user)
        met, judged = indicators.tally(seen.get("indicators") or [])
        return {**seen, "taken_on": _day(snap.taken_on), "taken_by_name": _name(snap.taken_by), "complete": not seen["left_out"],
                "met": met, "judged": judged}

    def get_created_by_name(self, ev):
        return _name(ev.created_by)

    def get_print_url(self, ev) -> str:
        return page_url(ev, "web:evaluation_version_print")


STATUS_FILTERS = (*Status.values, "all")
