"""
Checks after a repair (slice 30): before a repaired device goes back into use, CE records its safety, operational, and functional checks
(CMS A-0724: every device, after major repairs or upgrades; the Joint Commission's checks after a major repair; NFPA 99's electrical
safety testing after a repair that might have compromised it; IEC 62353's test after repair). The facility's own policy line says it
already (FacilitySettings.policy_post_repair: "Safety test required before return to service").

Every repair records how it ended (WorkOrder.repair_check, RepairCheck), through completion.complete_work_order only:
- passed: the POST_REPAIR checklist below, every step recorded (pass, fail, or N/A; never N/A on NEVER_NA), none failed;
- vendor_report: on a vendor-service repair, the vendor did them; their service report's number (WorkOrder.checks_reference, a token);
- not_returned: the device does not go back into use (beyond repair, to be retired, back to its owner, kept by the manufacturer).

Cadence records readings and the technician's pass or fail, never limits: the safety analyzer and the facility's plan apply them (they
depend on the standard, the protection class, and the applied parts, which Cadence does not know). The standard checklist only: a model's
PM procedure is a PM's, and doing it on a repair never credits the PM.
"""
from __future__ import annotations

from dataclasses import dataclass

# The checklist (completion.checklist_of's shape: text, and what to record or None). The N/A rule is in the words: electrical tests
# follow what the repair touched (NFPA 99, IEC 62353 4.2), so a door latch fixed at the bedside records N/A on them with a reason.
POST_REPAIR_CHECKLIST = [
    ("Visual: housing, cords, plugs, mounts, and accessories intact", None),
    ("Protective earth resistance (N/A when the repair did not open the case or touch the power cord or supply, or for a "
     "double-insulated or battery-only device)", "Record Ω"),
    ("Equipment (touch) leakage (N/A as for protective earth)", "Record leakage µA"),
    ("Applied-part (patient) leakage (N/A with no patient applied parts, or when the repair did not touch them)", "Record leakage µA"),
    ("Function verified against the manufacturer's specifications", None),
    ("Alarms and indicators tested (N/A when the device has none)", None),
    ("Settings and configuration restored, cleaned, and the tag and labels in place", None),
]
# The steps that are never N/A, by position: whatever the repair, the device is looked at and its function verified.
NEVER_NA = (0, 4)

# The error keys complete_work_order uses for a repair (the modal and the API key their messages by them).
ERROR_KEYS = ("repair_check", "checks_reference", "checklist")  # and step_<n>, reading_<n> as for a PM


@dataclass(frozen=True, eq=False)
class RepairChecklist:
    """POST_REPAIR in a PM procedure's shape (code, name, revision, checklist), so completion.checklist_of, the signature, and the modal
    read it as they read a procedure. Never an incoming checklist (inspections.is_incoming_checklist stays False) and never saved: a
    completed repair keeps its steps as recorded (WorkOrder.checklist_results)."""
    code: str = ""
    name: str = "Checks after repair"
    revision: str = ""
    checklist: tuple = tuple({"text": text, "measure": measure} for text, measure in POST_REPAIR_CHECKLIST)
    is_incoming: bool = False
    is_post_repair: bool = True
    temporary: bool = False


POST_REPAIR = RepairChecklist()


def is_post_repair_checklist(procedure) -> bool:
    return isinstance(procedure, RepairChecklist)


def last_completed_first(qs):
    """Work orders `qs`, the one completed last first: by the day completed, then by the moment its completion was recorded (the status
    history's completed row), the number last (review fix: two repairs completed the same day are told apart by when, never by
    number). For "a device's last repair" (the binder's repairs section, equipment.services.loaner_device_back)."""
    from django.db.models import F, OuterRef, Subquery

    from .models import WorkOrderStatusHistory, WoStatus

    done_at = (WorkOrderStatusHistory.objects.filter(work_order=OuterRef("pk"), to_status=WoStatus.COMPLETED)
               .order_by("-created_at").values("created_at")[:1])
    return qs.annotate(done_at=Subquery(done_at)).order_by("-completed_on", F("done_at").desc(nulls_last=True), "-number")


def checked_by(wo, user=None) -> str:
    """Who did a repair's checks, or an incoming inspection (the print, the drawer's results, the binder): on vendor service the vendor's
    engineer the work order names (slice 31: "Dana Kim (Philips)"; for a scoped `user`, another company's engineer reads as
    vendors.services.engineer_shown words it), else the vendor; else the technician assigned; never whoever completed it from the desk
    (IEC 62353 6.1 names the tester). Select vendor_engineer with the work order when reading many."""
    if wo.vendor_service:
        if wo.vendor_engineer_id:
            from apps.vendors.services import engineer_shown

            return engineer_shown(wo, user)
        return wo.vendor_name or "Vendor"
    return wo.assigned_to.name if wo.assigned_to_id else ""
