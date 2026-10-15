"""
The review of each contracted service vendor over the API (slice 34; apps/api/views_vendor_reviews.py). Showing only: the API is
read-only, and every write goes through apps.evaluations.vendor_reviews from the Contracts screen's Reviews tab.

The list (VendorReviewListSerializer) carries no figures and no summary: the number, the vendor's names, the 12 months, the status, the
overall rating and the decision, the corrective action plan's due day and result, and the day it was signed off. One review
(VendorReviewSerializer) adds the reviewer's ratings, the vendor's own reported figures, the summary, the sign-off, the previous
signed-off review of the vendor, the committees it was reported to, and the figures through their gate, all or nothing
(apps.evaluations.vendor_figures.cut: Evaluations View, Contracts View, Reports View, and Work orders View; `left_out` says what a
reader without them needs). The summary is never anything else's: no CSV, email, digest, custom report, or All facilities.
"""
from django.urls import reverse
from rest_framework import serializers

from apps.accounts import people
from apps.evaluations import vendor_figures
from apps.evaluations import vendor_reviews as vr
from apps.evaluations.models import CRITERIA, Decision, Overall, PlanResult, Rating, StatedInContract, VendorReview
from apps.tenants.context import get_current_tenant


def _name(user):
    """A user as Cadence names people (str(user): the full name, else the email); null once the account is gone."""
    return str(user) if user is not None else None


def _day(value):
    return value.isoformat() if value is not None else None


def label(choices, value: str) -> str:
    """A stored choice's words; "" when none is recorded."""
    return choices(value).label if value in choices.values else ""


def page_url(review: VendorReview, name: str = "web:vendor_review") -> str:
    """The review's page on the web (or its print: web:vendor_review_print), naming its facility: numbers repeat across facilities."""
    tenant = get_current_tenant()  # the request's facility (TenantAPIMixin): no query per review
    if tenant is None or tenant.pk != review.tenant_id:
        tenant = review.tenant
    return people.with_facility(reverse(name, args=[review.number]), tenant)


class VendorReviewListSerializer(serializers.ModelSerializer):
    """A review as the list shows it: no figures, no summary."""

    vendor = serializers.CharField(read_only=True)
    period_label = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    overall_label = serializers.SerializerMethodField()
    decision_label = serializers.SerializerMethodField()
    plan_result_label = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = VendorReview
        fields = ["id", "number", "vendor", "vendor_names", "period_start", "period_end", "period_label", "status", "status_label", "overall",
                  "overall_label", "decision", "decision_label", "plan_due_on", "plan_result", "plan_result_label", "plan_closed_on",
                  "signed_on", "url"]
        read_only_fields = fields

    def get_period_label(self, r) -> str:
        return vr.period_label(r.period_start, r.period_end)

    def get_overall_label(self, r) -> str:
        return label(Overall, r.overall)

    def get_decision_label(self, r) -> str:
        return label(Decision, r.decision)

    def get_plan_result_label(self, r) -> str:
        return label(PlanResult, r.plan_result)

    def get_url(self, r) -> str:
        return page_url(r)


class VendorReviewSerializer(VendorReviewListSerializer):
    """One review: the list's fields, then the ratings, the vendor's reported figures, the summary, the sign-off, the previous signed-off
    review of the vendor, the committee reports, and the figures through their gate (context["request"].user)."""

    ratings = serializers.SerializerMethodField()
    stated_in_contract_label = serializers.SerializerMethodField()
    previous = serializers.SerializerMethodField()
    reports = serializers.SerializerMethodField()
    figures = serializers.SerializerMethodField()
    signed_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    print_url = serializers.SerializerMethodField()

    class Meta(VendorReviewListSerializer.Meta):
        fields = VendorReviewListSerializer.Meta.fields + [
            "ratings", "stated_in_contract", "stated_in_contract_label", "uptime_target_pct", "uptime_reported_pct", "uptime_lowest_pct",
            "systems_below", "systems_total", "response_target_pct", "response_reported_pct", "vendor_report_reference", "summary",
            "approver_name", "approver_title", "signed_by_name", "previous", "reports", "figures", "created_by_name", "print_url",
            "created_at", "updated_at"]
        read_only_fields = fields

    def get_ratings(self, r) -> list:
        """The reviewer's rating of each criterion, in models.CRITERIA's order: [{field, label, covers, rating, rating_label}]."""
        return [{"field": field, "label": words, "covers": covers, "rating": getattr(r, field), "rating_label": label(Rating, getattr(r, field))}
                for field, words, covers in CRITERIA]

    def get_stated_in_contract_label(self, r) -> str:
        return label(StatedInContract, r.stated_in_contract)

    def get_previous(self, r):
        """The previous signed-off review of the vendor (the one a draft is prefilled from), or null: its number, overall rating,
        decision, and plan result, and its page."""
        before = vr.previous_approved(r)
        if before is None:
            return None
        return {"id": str(before.id), "number": before.number, "period_label": vr.period_label(before.period_start, before.period_end),
                "overall": before.overall, "overall_label": label(Overall, before.overall), "decision": before.decision,
                "decision_label": label(Decision, before.decision), "plan_result": before.plan_result,
                "plan_result_label": label(PlanResult, before.plan_result), "url": page_url(before)}

    def get_reports(self, r) -> list:
        """The committees it was reported to, in meeting order."""
        return [{"id": str(rep.id), "body": rep.body, "body_label": rep.get_body_display(), "meeting_on": _day(rep.meeting_on),
                 "minutes_reference": rep.minutes_reference, "recorded_by_name": _name(rep.recorded_by)} for rep in r.reports.all()]

    def get_figures(self, r):
        """The figures as taken (null before any), all or nothing (vendor_figures.cut): the snapshot's shape, `left_out` naming what a
        reader without the gate needs, and who took them."""
        snap = r.snapshot
        if snap is None:
            return None
        return {**vendor_figures.cut(snap.data, self.context["request"].user), "taken_by_name": _name(snap.taken_by)}

    def get_signed_by_name(self, r):
        """Who recorded the sign-off in Cadence (the approver is approver_name, as they signed)."""
        return _name(r.signed_by)

    def get_created_by_name(self, r):
        return _name(r.created_by)

    def get_print_url(self, r) -> str:
        return page_url(r, "web:vendor_review_print")
