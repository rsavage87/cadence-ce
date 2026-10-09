"""
Slice 30, checks after a repair: results for the standard post-repair checklist (workorders.repair_checks.POST_REPAIR_CHECKLIST) and
the keyword arguments complete_work_order takes for a repair checked and ready for use. Import from any test file:
`from repair_fixtures import CHECKED, checked_repair, post_repair_results`.

- post_repair_results(*values): "pass" / "fail" / "na" per step (all pass when none given), a reading on each measured step that is
  not N/A (the protective earth in ohms, the leakages in microamps).
- CHECKED: complete_work_order(wo, resolution=..., **CHECKED) completes a repair as passed, every step passing.
- checked_repair(**extra): CHECKED with more keyword arguments.
"""
from apps.workorders.models import RepairCheck
from apps.workorders.repair_checks import POST_REPAIR_CHECKLIST

READINGS = {"Record Ω": "0.12 Ω", "Record leakage µA": "45 µA"}


def post_repair_results(*values) -> list[dict]:
    values = values or ("pass",) * len(POST_REPAIR_CHECKLIST)
    return [{"result": v, "reading": READINGS.get(measure, "1") if measure and v != "na" else ""}
            for v, (_text, measure) in zip(values, POST_REPAIR_CHECKLIST, strict=True)]


POST_REPAIR_ALL_PASS = post_repair_results()
CHECKED = {"repair_check": RepairCheck.PASSED, "results": POST_REPAIR_ALL_PASS}


def checked_repair(**extra) -> dict:
    return {**CHECKED, **extra}
