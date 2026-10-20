"""
The medical equipment program's annual evaluation (slice 32, beyond the mock): one facility's evaluation of its program for one
12-month period, drafted from Cadence's figures for the period (a snapshot of indicators, apps.evaluations.indicators), with the text
Clinical Engineering writes about the program, the year's objectives reviewed and the next year's set, and the committee approvals
recorded. The Joint Commission no longer requires it of hospitals (2026); DNV, ACHC, and the facility's own plan may, and hospitals
keep it for their EOC or safety committee and their QAPI program. Cadence never says which accreditor requires it.

Every write goes through apps.evaluations.services. Statuses: draft (figures refreshed, text and objectives written) -> ready (sent to
committee: frozen; Back to draft) -> approved (the first approval) -> superseded (when its amendment, the same number with the next
version, is approved). A draft may be discarded. Nothing approved is ever edited: an amendment is a new version.

Privacy (CLAUDE.md non-negotiable 6): the text is staff-written, about the program in aggregate, length-limited, and the form warns
against patient or requester details; it is never emailed, never a custom report source, never in All facilities, never shown to a
scoped user. Incident figures are counts only and shown only to Incidents View.
"""
from django.conf import settings
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import TenantModel
from apps.core.validators import reference_token

PLAN_VERSION_VALIDATOR = reference_token("Enter the plan's version as your policy system shows it, without spaces (v4, 2026-1).")
MINUTES_VALIDATOR = reference_token("Enter the minutes' reference as your committee files them, without spaces (EOC-2026-02).")

TEXT_MAX = 2000  # each narrative field
OBJECTIVE_MAX, TARGET_MAX, COMMENT_MAX = 300, 120, 300
OBJECTIVES_MAX = 20  # of each kind

# The text Clinical Engineering writes (field, label, help): one column each, saved one field at a time (services.update_text), and the
# form, the page, the print, and the history read this tuple. Each at most TEXT_MAX characters.
NARRATIVE = (
    ("summary", "Summary", "A paragraph for the committee: the year in short, and what you ask of it."),
    ("scope_changes", "Scope and changes", "What the program covers, and what changed in the year: services, sites, equipment, hazards."),
    ("emergency_equipment", "Equipment for emergencies", "The equipment needed for likely emergencies, and how it is kept ready."),
    ("aem_effectiveness", "AEM program safety and effectiveness",
     "How degraded performance was detected, malfunctions investigated, unsafe equipment removed, and the program changed. Refer to "
     "incidents by number only, never by unit, day, or outcome."),
    ("effectiveness", "Overall effectiveness", "Whether the program did what it set out to do, judged from the figures and the objectives."),
    ("went_well", "What went well", ""),
    ("improvements", "What needs improvement", ""),
    ("capital_staffing", "Capital, replacement, staffing, and training", "Replacement planning, capital requests, staffing, and CE staff training."),
    # Slice 37: the Device security screen keeps the register (help text only: no approved snapshot stores it)
    ("cybersecurity", "Cybersecurity", "The Device security screen keeps the register: how medical device security was managed this period."),
)
NARRATIVE_FIELDS = tuple(f for f, _label, _help in NARRATIVE)
PRIVACY_WARNING = "About the program as a whole: no patient, requester, or incident details (incidents by number only)."


class Status(models.TextChoices):
    DRAFT = "draft", "Draft"
    READY = "ready", "Sent to committee"
    APPROVED = "approved", "Approved"
    SUPERSEDED = "superseded", "Superseded by an amendment"
    DISCARDED = "discarded", "Discarded"


# The statuses whose period counts: no two of these overlap but for one evaluation's versions (services._overlapping).
LIVE_STATUSES = (Status.DRAFT, Status.READY, Status.APPROVED)


class ProgramEvaluation(TenantModel):
    number = models.CharField(max_length=20, editable=False, help_text="EV-26-0001 (Sequence 'evaluation-<yy>', yy from the period's end)")
    version = models.PositiveSmallIntegerField(default=1, editable=False)
    amends = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="amendments", editable=False,
                               help_text="The approved version this amendment replaces once it is approved")
    period_start = models.DateField(help_text="The first of a month")
    period_end = models.DateField(help_text="12 months after the start, less a day; ended before the evaluation is drafted")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False)
    snapshot = models.ForeignKey("EvaluationSnapshot", on_delete=models.PROTECT, null=True, blank=True, related_name="+", editable=False,
                                 help_text="The figures as last taken (services.refresh); kept as they were when it was sent to committee")
    sent_on = models.DateField(null=True, blank=True, editable=False, help_text="The day it was last sent to committee (ready)")
    approved_on = models.DateField(null=True, blank=True, editable=False, help_text="The first approval's meeting date")
    # The text (NARRATIVE), each at most TEXT_MAX characters.
    summary = models.TextField(blank=True)
    scope_changes = models.TextField(blank=True)
    emergency_equipment = models.TextField(blank=True)
    aem_effectiveness = models.TextField(blank=True)
    effectiveness = models.TextField(blank=True)
    went_well = models.TextField(blank=True)
    improvements = models.TextField(blank=True)
    capital_staffing = models.TextField(blank=True)
    cybersecurity = models.TextField(blank=True)
    inventory_reviewed_on = models.DateField(null=True, blank=True, help_text="When the inventory was last reviewed for completeness")
    # The written plan reviewed: a reference (the plan lives in the facility's policy system), never the plan itself.
    plan_title = models.CharField(max_length=120, blank=True)
    plan_version = models.CharField(max_length=40, blank=True, validators=[PLAN_VERSION_VALIDATOR])
    plan_effective_on = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-period_end", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number", "version"], name="uniq_evaluation_version_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "period_start"], condition=models.Q(status="approved"),
                                    name="uniq_approved_evaluation_per_period"),
        ]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "period_end"])]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return self.number if self.version == 1 else f"{self.number} v{self.version}"


class EvaluationSnapshot(TenantModel):
    """The figures for an evaluation's period as taken on one day (apps.evaluations.indicators.snapshot): every indicator, whoever
    reads it later (each reader sees the ones their access allows: indicators.cut). Immutable and without history: a refresh adds a new
    row and the evaluation points at it."""
    evaluation = models.ForeignKey(ProgramEvaluation, on_delete=models.CASCADE, related_name="snapshots")
    taken_at = models.DateTimeField()
    taken_on = models.DateField(help_text="The facility's day the figures were taken")
    taken_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    data = models.JSONField(default=dict, help_text='{"schema": 1, ...}: indicators.snapshot\'s shape')

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"{self.evaluation.label} figures {self.taken_on}"


class ObjectiveKind(models.TextChoices):
    REVIEW = "review", "This period's objective"
    NEXT = "next", "Next period's objective"


class ObjectiveResult(models.TextChoices):
    MET = "met", "Met"
    PARTLY = "partly", "Partly met"
    NOT_MET = "not_met", "Not met"
    CARRIED = "carried", "Carried forward"


class EvaluationObjective(TenantModel):
    """One objective: this period's (copied from the previous approved evaluation's next-period objectives when the draft is made, or
    added; its result and comment written here), or the next period's (its text and target)."""
    evaluation = models.ForeignKey(ProgramEvaluation, on_delete=models.CASCADE, related_name="objectives")
    kind = models.CharField(max_length=10, choices=ObjectiveKind.choices)
    position = models.PositiveSmallIntegerField(default=0)
    text = models.CharField(max_length=OBJECTIVE_MAX)
    target = models.CharField(max_length=TARGET_MAX, blank=True)
    result = models.CharField(max_length=10, choices=ObjectiveResult.choices, blank=True)
    comment = models.CharField(max_length=COMMENT_MAX, blank=True)
    carried_from = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False,
                                     help_text="The previous evaluation's next-period objective this one was copied from")
    history = HistoricalRecords()

    class Meta:
        ordering = ["kind", "position", "created_at"]

    def __str__(self):
        return f"{self.evaluation.label} · objective"


class Body(models.TextChoices):
    EOC = "eoc", "Environment of care or safety committee"
    QUALITY = "quality", "Quality oversight (QAPI)"
    MEC = "mec", "Medical executive committee"
    GOVERNING = "governing", "Governing body"
    OTHER = "other", "Another committee"


class EvaluationApproval(TenantModel):
    """One committee's approval of an evaluation, as its minutes record it: the first approves the evaluation (it was sent to that
    committee); later ones record where it went next (quality oversight, the governing body)."""
    evaluation = models.ForeignKey(ProgramEvaluation, on_delete=models.PROTECT, related_name="approvals")
    body = models.CharField(max_length=12, choices=Body.choices)
    body_name = models.CharField(max_length=80, blank=True, help_text="The committee's name, for another committee")
    meeting_on = models.DateField()
    chair_name = models.CharField(max_length=120, blank=True)
    chair_title = models.CharField(max_length=120, blank=True)
    minutes_reference = models.CharField(max_length=40, blank=True, validators=[MINUTES_VALIDATOR])
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["meeting_on", "created_at"]

    def __str__(self):
        return f"{self.evaluation.label} · approval"


# --- slice 34: the review of each contracted service vendor ------------------------------------------------------------------------
#
# The facility's yearly review of a service vendor (CMS A-0084: every contracted service evaluated through QAPI; The Joint Commission's
# governing body assesses contracted services and acts on problems; DNV each contractor at defined intervals): figures from Cadence's own
# records of the vendor's work for a 12-month period (apps.evaluations.vendor_figures; never uptime or response computed from its dates),
# the reviewer's ratings and the vendor's own reported figures, a decision, a corrective plan followed to its result, the sign-off, and
# the committees it was reported to. Standalone: never slice 32's ProgramEvaluation models or services. Every write goes through
# apps.evaluations.vendor_reviews. Never shown to a scoped user (a vendor's account never sees its review); the summary is staff-written,
# length-limited, never emailed.

REPORT_REFERENCE_VALIDATOR = reference_token("Enter the vendor's report reference as it is printed, without spaces (UPT-2026-Q4).")
REVIEW_SUMMARY_MAX = 2000
REVIEW_PRIVACY_WARNING = "About the vendor's service as a whole: no patient, requester, engineer, or incident details."


class ReviewStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    APPROVED = "approved", "Signed off"
    WITHDRAWN = "withdrawn", "Withdrawn"
    DISCARDED = "discarded", "Discarded"


# The statuses whose period counts: no vendor key in two of these with overlapping periods (vendor_reviews checks it under its lock).
LIVE_REVIEW_STATUSES = (ReviewStatus.DRAFT, ReviewStatus.APPROVED)


class Rating(models.TextChoices):
    MET = "met", "Met"
    PARTLY = "partly", "Partly met"
    NOT_MET = "not_met", "Not met"
    NA = "na", "Not applicable"


# The criteria the reviewer rates (field, label, what it covers): the review's fixed rating fields, the form, the page, the print, and
# the history read this tuple.
CRITERIA = (
    ("rating_timeliness", "Timeliness", "Time with the vendor, PMs assigned to it done on time, the vendor's reported response."),
    ("rating_quality", "Quality of work", "Repeat repairs, checks after repair, the vendor's reported uptime."),
    ("rating_documentation", "Documentation", "Service report numbers, engineers named, checks and findings recorded."),
    ("rating_staff_evidence", "Staff evidence", "Training or the vendor's attestation on file for the work it did."),
    ("rating_user_satisfaction", "User satisfaction", "What the units and CE staff who work with the vendor report."),
    ("rating_invoices", "Invoices", "Invoices accurate and on time."),
)
CRITERIA_FIELDS = tuple(f for f, _label, _help in CRITERIA)


class StatedInContract(models.TextChoices):
    YES = "yes", "Yes"
    PARTLY = "partly", "Partly"
    NO = "no", "No"


class Overall(models.TextChoices):
    MEETS = "meets", "Meets expectations"
    PARTIALLY = "partially", "Partially meets expectations"
    DOES_NOT_MEET = "does_not_meet", "Does not meet expectations"


class Decision(models.TextChoices):
    RENEW = "renew", "Renew"
    RENEW_CHANGES = "renew_changes", "Renew with changes"
    CORRECTIVE_PLAN = "corrective_plan", "Corrective action plan"
    REPLACE = "replace", "Replace or end the contract"
    IN_HOUSE = "in_house", "Bring the service in-house"


class PlanResult(models.TextChoices):
    MET = "met", "Plan met"
    NOT_MET = "not_met", "Plan not met"
    CONTRACT_ENDED = "contract_ended", "Contract ended"


class ReportBody(models.TextChoices):
    QUALITY = "quality", "Quality oversight (QAPI)"
    GOVERNING = "governing", "Governing body"


class VendorReview(TenantModel):
    number = models.CharField(max_length=20, editable=False, help_text="VR-26-0001 (Sequence 'vendor-review-<yy>', yy from the period's end)")
    vendor_names = models.JSONField(default=list, help_text="The vendor's names as work orders and contracts spell it, the first shown")
    vendor_keys = models.JSONField(default=list, editable=False, help_text="apps.vendors.rules.vendor_key of each name: services only")
    vendor_key = models.CharField(max_length=120, editable=False, help_text="The first name's key (the approved review's constraint)")
    period_start = models.DateField()
    period_end = models.DateField(help_text="12 months after the start, less a day; ended before the review is drafted")
    status = models.CharField(max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.DRAFT, editable=False)
    snapshot = models.ForeignKey("VendorReviewSnapshot", on_delete=models.PROTECT, null=True, blank=True, related_name="+", editable=False)
    # The reviewer's ratings (CRITERIA) and the rest of the review
    rating_timeliness = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    rating_quality = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    rating_documentation = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    rating_staff_evidence = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    rating_user_satisfaction = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    rating_invoices = models.CharField(max_length=10, choices=Rating.choices, blank=True)
    stated_in_contract = models.CharField("expectations stated in the contract", max_length=10, choices=StatedInContract.choices, blank=True)
    # The vendor's own reported figures (Cadence never computes uptime or response from its dates)
    uptime_target_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    uptime_reported_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    uptime_lowest_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, help_text="The lowest system's")
    systems_below = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Systems below the guarantee")
    systems_total = models.PositiveSmallIntegerField(null=True, blank=True)
    response_target_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    response_reported_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    vendor_report_reference = models.CharField(max_length=40, blank=True, validators=[REPORT_REFERENCE_VALIDATOR])
    overall = models.CharField(max_length=15, choices=Overall.choices, blank=True)
    decision = models.CharField(max_length=20, choices=Decision.choices, blank=True)
    plan_due_on = models.DateField(null=True, blank=True)
    plan_result = models.CharField(max_length=15, choices=PlanResult.choices, blank=True, editable=False)
    plan_closed_on = models.DateField(null=True, blank=True, editable=False)
    summary = models.TextField(blank=True)
    # The sign-off
    signed_on = models.DateField(null=True, blank=True, editable=False)
    approver_name = models.CharField(max_length=120, blank=True)
    approver_title = models.CharField(max_length=120, blank=True)
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", editable=False,
                                  help_text="Who recorded the sign-off in Cadence")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["-period_end", "number"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "number"], name="uniq_vendor_review_number_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "vendor_key", "period_start"], condition=models.Q(status="approved"),
                                    name="uniq_approved_vendor_review_per_period"),
        ]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["tenant", "vendor_key"])]

    def __str__(self):
        return self.number

    @property
    def vendor(self) -> str:
        return (self.vendor_names or [""])[0]


class VendorReviewSnapshot(TenantModel):
    """The figures for a review's vendor and period as taken on one day (apps.evaluations.vendor_figures.snapshot). Immutable and without
    history: a refresh adds a new row and the review points at it."""
    review = models.ForeignKey(VendorReview, on_delete=models.CASCADE, related_name="snapshots")
    taken_at = models.DateTimeField()
    taken_on = models.DateField()
    taken_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    data = models.JSONField(default=dict, help_text='{"schema": 1, ...}: vendor_figures.snapshot\'s shape')

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"{self.review.number} figures {self.taken_on}"


class VendorReviewReport(TenantModel):
    """One committee a signed-off review was reported to, as its minutes record it (the quality program, the governing body)."""
    review = models.ForeignKey(VendorReview, on_delete=models.PROTECT, related_name="reports")
    body = models.CharField(max_length=12, choices=ReportBody.choices)
    meeting_on = models.DateField()
    minutes_reference = models.CharField(max_length=40, blank=True, validators=[MINUTES_VALIDATOR])
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    history = HistoricalRecords()

    class Meta:
        ordering = ["meeting_on", "created_at"]

    def __str__(self):
        return f"{self.review.number} · report"
