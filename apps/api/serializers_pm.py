"""
What the PM program's API shows and parses (slice 19, part B; apps/api/views_pm.py): PM procedures, a device model's risk score and
AEM program, AEM decisions, and Auto-assign week. Showing and parsing only: every write goes through apps.pm.procedures,
apps.equipment.services, apps.pm.aem, and apps.pm.services, which validate and record the history. The input serializers here check
types (text is text, a checklist is lines or a list) and leave every rule, and its words, to the services.
"""
from rest_framework import serializers

from apps.equipment import services as eq_services
from apps.equipment.models import RiskClass
from apps.facility.services import RISK_BANDS
from apps.pm import aem, procedures
from apps.pm.models import AemDecision, AemStatus, PmProcedure
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoType

from .base import _hours

# --- PM procedures ------------------------------------------------------------------------------------------------------------


def _stored_steps(checklist) -> list:
    """A stored checklist as its steps, whatever its shape (as the print reads it): a list, one string with a step per line
    (older data), or a single step."""
    if checklist in (None, "", [], {}):
        return []
    if isinstance(checklist, str):
        return [line.strip() for line in checklist.splitlines() if line.strip()]
    return list(checklist) if isinstance(checklist, (list, tuple)) else [checklist]


def checklist_lines(checklist) -> list[dict]:
    """The checklist as structured lines, in order: {"text", "measure"}, where measure is null (nothing recorded), true (a reading,
    without saying what), or what to record ("µA, limit 100"). Sending these lines back saves the same checklist."""
    return [{"text": text, "measure": measure} for text, measure in (procedures.step_parts(s) for s in _stored_steps(checklist))]


class ProcedureSerializer(serializers.ModelSerializer):
    """A procedure as the model drawer's Procedure tab and Edit procedure show it, with the models that use it. open_pm_work_orders:
    the open PM work orders on devices of those models. They keep the hours estimated when they were created, and print the
    checklist as it is when they are printed, so a revision reaches them on paper but not in the hours planned."""

    checklist = serializers.SerializerMethodField()
    checklist_text = serializers.SerializerMethodField()
    models = serializers.SerializerMethodField()
    open_pm_work_orders = serializers.SerializerMethodField()

    class Meta:
        model = PmProcedure
        fields = ["id", "code", "name", "source_kind", "source_reference", "source_url", "revision", "estimated_hours", "checklist",
                  "checklist_text", "models", "open_pm_work_orders", "updated_at"]
        read_only_fields = fields

    def get_checklist(self, p):
        return checklist_lines(p.checklist)

    def get_checklist_text(self, p):
        return procedures.checklist_text(p.checklist)

    def get_models(self, p):
        # Prefetched by the viewset (ordered as procedures.models_using); read here otherwise.
        models = p.device_models.all() if "device_models" in getattr(p, "_prefetched_objects_cache", {}) else procedures.models_using(p)
        return [{"id": str(dm.pk), "manufacturer": dm.manufacturer, "model": dm.model, "description": dm.description} for dm in models]

    def get_open_pm_work_orders(self, p):
        if hasattr(p, "open_pm"):  # annotated by the viewset (one query for the list)
            return p.open_pm or 0
        return WorkOrder.objects.filter(asset__device_model__pm_procedure=p, type=WoType.PM, status__in=OPEN_STATUSES).count()


# What a procedure's GET shows but a write never takes; a client may send them back unchanged (views_pm._check_fields).
PROCEDURE_READ_ONLY = ("id", "checklist_text", "models", "open_pm_work_orders", "updated_at")


class RawHours(serializers.Field):
    """The estimated hours as sent (a number or its text): apps.pm.procedures parses them and says what is wrong."""

    default_error_messages = {"invalid": "Enter the hours as a number, e.g. 1.5."}

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, (str, int, float)):
            self.fail("invalid")
        return data

    def to_representation(self, value):
        return value


class RawChecklist(serializers.Field):
    """The checklist as structured lines (a list of steps: text, or {"text", "measure"}) or as the editor's text, one step per line
    in procedures.LINE_FORMAT. apps.pm.procedures checks either."""

    default_error_messages = {"invalid": "Send the checklist as a list of steps, or as text with one step per line."}

    def to_internal_value(self, data):
        if not isinstance(data, (str, list)):
            self.fail("invalid")
        return data

    def to_representation(self, value):
        return value


def _text_field():
    # Text as sent (the service trims, collapses, and checks it); null is blank.
    return serializers.CharField(required=False, allow_blank=True, allow_null=True, trim_whitespace=False)


class ProcedureInputSerializer(serializers.Serializer):
    """A procedure's fields as a write sends them (procedures.EDITABLE), types only."""

    code = _text_field()
    name = _text_field()
    source_kind = _text_field()
    source_reference = _text_field()
    source_url = _text_field()
    revision = _text_field()
    estimated_hours = RawHours(required=False, allow_null=True)
    checklist = RawChecklist(required=False, allow_null=True)


# --- the risk score -----------------------------------------------------------------------------------------------------------

RISK_KEYS = tuple(key for key, *_rest in eq_services.RISK_PARTS)  # set_risk_score's keywords: function, physical, maintenance, incidents
_BAND_OF_CLASS = {rc.value: band for band, rc in RISK_BANDS}  # "life_support" -> "16 and above", as Settings shows the bands


def risk_score_data(dm, today) -> dict:
    """The model's risk score as the PM program tab and the risk modal show it: the four parts (null while unscored), the total,
    the band it falls in and the class that band gives, the class on file (set by hand while unscored), the review dates, and
    the rubric's ranges."""
    score = dm.risk_score
    band_class = eq_services.risk_band(score) if score is not None else None
    due_on = eq_services.risk_review_due_on(dm)
    return {
        "device_model": str(dm.pk),
        **{key: getattr(dm, field) for key, field, *_rest in eq_services.RISK_PARTS},
        "score": score,
        "band": _BAND_OF_CLASS.get(band_class) if band_class else None,
        "band_class": band_class,
        "risk_class": dm.risk_class,
        "risk_class_label": dm.get_risk_class_display(),
        "reviewed_on": dm.risk_reviewed_on.isoformat() if dm.risk_reviewed_on else None,
        "review_due_on": due_on.isoformat() if due_on else None,
        "review_due": eq_services.risk_review_due(dm, today),
        "rubric": [{"part": key, "label": label, "low": low, "high": high} for key, _field, label, low, high in eq_services.RISK_PARTS],
    }


def aem_effect_data(effect: dict | None) -> dict:
    """What the AEM program did about a model change (equipment.services sets dm.aem_effect; apps.pm.aem.model_changed): a model
    scored into life support leaves AEM, its interval in force ending and its devices' next PMs moving in (devices_moved)."""
    effect = effect or {}
    return {"ended": bool(effect.get("ended")), "withdrawn": bool(effect.get("withdrawn")), "cleared": bool(effect.get("cleared")),
            "devices_moved": effect.get("moved") or 0, "rule": effect.get("rule") or ""}


# --- AEM ----------------------------------------------------------------------------------------------------------------------


def _name(user):
    return str(user) if user is not None else None


class AemDecisionSerializer(serializers.ModelSerializer):
    """One AEM case: proposed with the model's failure history as it stood (evidence, apps.pm.aem.evidence), then approved (in
    force), rejected, withdrawn, or ended. A user who has left reads as null."""

    device_model_name = serializers.SerializerMethodField()
    proposed_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    ended_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AemDecision
        fields = ["id", "device_model", "device_model_name", "status", "interval_months", "oem_interval_months", "rationale", "evidence",
                  "proposed_by", "proposed_by_name", "proposed_on", "decided_by", "decided_by_name", "decided_on", "decision_note",
                  "ended_by", "ended_by_name", "ended_on", "end_reason", "updated_at"]
        read_only_fields = fields

    def get_device_model_name(self, d):
        return str(d.device_model)

    def get_proposed_by_name(self, d):
        return _name(d.proposed_by)

    def get_decided_by_name(self, d):
        return _name(d.decided_by)

    def get_ended_by_name(self, d):
        return _name(d.ended_by)


def _exclusion_rule(dm) -> str:
    """Which rule excludes the model from AEM, in apps.pm.aem.exclusion's order: life support first, then the CMS mark."""
    if not dm.aem_excluded:
        return ""
    return "life_support" if dm.risk_class == RiskClass.LIFE_SUPPORT else "oem_schedule"


def aem_data(dm, today) -> dict:
    """The model's AEM program as its AEM tab shows it: the interval in force and where it comes from, why the model is excluded
    if it is (apps.pm.aem.exclusion), the decision in force, an interval on file without a recorded approval, the open proposal,
    why a proposal would be refused now (propose_blocker, "" when one can be made), the failure history as of today, and every
    decision, newest first."""
    decisions = list(AemDecision.objects.filter(device_model=dm).select_related("device_model", "proposed_by", "decided_by", "ended_by"))
    approved = next((d for d in decisions if d.status == AemStatus.APPROVED), None)
    proposal = next((d for d in decisions if d.status == AemStatus.PROPOSED), None)
    ev = aem.evidence(dm, today)
    legacy = aem.is_legacy(dm, approved)
    rows = AemDecisionSerializer(decisions, many=True).data
    by_id = {row["id"]: row for row in rows}
    return {
        "device_model": str(dm.pk),
        "oem_interval_months": dm.oem_pm_interval_months,
        "interval_months": dm.pm_interval_months,  # in force: the OEM interval, or the AEM interval when one applies
        "on_aem": dm.pm_interval_months != dm.oem_pm_interval_months,
        "excluded": aem.exclusion(dm),
        "exclusion_rule": _exclusion_rule(dm),
        "in_force": by_id[str(approved.pk)] if approved is not None else None,
        # An AEM interval set before approvals were recorded (apps.pm.aem.is_legacy): in use unless the model is excluded.
        "unapproved_interval_months": dm.aem_interval_months if legacy else None,
        "open_proposal": by_id[str(proposal.pk)] if proposal is not None else None,
        "propose_blocker": aem.propose_blocker(dm, today, ev=ev, open_=proposal),
        "history_years": aem.AEM_HISTORY_YEARS,
        "evidence": ev,
        "decisions": rows,
    }


# --- Auto-assign week ---------------------------------------------------------------------------------------------------------


def _week_device(row) -> dict:
    """A device the week leaves on nobody's plate ({"asset", "has_open_pm"}): its tag, model, PM due date, and whether its PM work
    order is open already."""
    asset = row["asset"]
    return {"asset_id": str(asset.pk), "tag": asset.tag, "description": asset.device_model.description,
            "due_on": asset.next_pm_on.isoformat() if asset.next_pm_on else None, "has_open_pm": row["has_open_pm"]}


def week_data(w) -> dict:
    """Auto-assign week as its modal shows it (apps.pm.services.WeekAssignment): the preview, or what was done. Per technician, the
    new work orders and the open ones on nobody's plate they get, and the hours; the devices nobody is credentialed for (their
    PMs stay unassigned; a work order is still created for those without one); what is already held; and the overdue devices
    the week leaves alone. Slice 28: `on_hold`, the devices due this week held as evidence for an incident investigation (nobody
    may start their PM until the incident releases them, so the week neither creates nor assigns one; an open one stays open and
    unassigned), shaped as `uncovered`. They count in no other figure: a week with only held devices due reads nothing_to_do, with
    them listed here. No incident number: Auto-assign week is PM Approve's, not Incidents View's."""
    return {
        "start": w.start.isoformat(),
        "end": w.end.isoformat(),
        "nothing_to_do": w.nothing_to_do,
        "assigned": w.assigned,
        "assigned_new": w.assigned_new,
        "assigned_existing": w.assigned_existing,
        "created": w.created,
        "hours": _hours(w.hours),
        "technicians": w.technicians,
        "shares": [{"technician": {"id": str(s.technician.pk), "name": s.technician.name}, "new": s.new, "existing": s.existing,
                    "count": s.count, "hours": _hours(s.hours)} for s in w.shares],
        "unassigned": w.unassigned,
        "unassigned_new": w.unassigned_new,
        "uncovered": [_week_device(u) for u in w.uncovered],
        "on_hold": [_week_device(h) for h in w.on_hold],
        "held": w.held,
        "overdue": w.overdue,
        "overdue_to_create": w.overdue_to_create,
        "overdue_waiting": w.overdue_waiting,
    }
