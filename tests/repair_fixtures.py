"""
Slice 30, checks after a repair: results for the standard post-repair checklist (workorders.repair_checks.POST_REPAIR_CHECKLIST) and
the keyword arguments complete_work_order takes for a repair checked and ready for use. Import from any test file:
`from repair_fixtures import CHECKED, checked_repair, post_repair_results`.

- post_repair_results(*values): "pass" / "fail" / "na" per step (all pass when none given), a reading on each measured step that is
  not N/A (the protective earth in ohms, the leakages in microamps).
- CHECKED: complete_work_order(wo, resolution=..., **CHECKED) completes a repair as passed, every step passing, and found worn out (slice
  33: FINDING, a reliability failure every door and reader accepts, so no figure a test pins moves).
- checked_repair(**extra): CHECKED with more keyword arguments.
- checked_post(**extra): the completion modal's fields (a web POST to /work-orders/<n>/complete/) for a repair checked and ready for
  use, every step passing, with the checklist's signature; `extra` adds or replaces fields (resolution, hours, from, ...).
"""
from apps.workorders.completion import checklist_of, checklist_signature
from apps.workorders.models import RepairCheck, RepairFinding
from apps.workorders.repair_checks import POST_REPAIR, POST_REPAIR_CHECKLIST

READINGS = {"Record Ω": "0.12 Ω", "Record leakage µA": "45 µA"}


def post_repair_results(*values) -> list[dict]:
    values = values or ("pass",) * len(POST_REPAIR_CHECKLIST)
    return [{"result": v, "reading": READINGS.get(measure, "1") if measure and v != "na" else ""}
            for v, (_text, measure) in zip(values, POST_REPAIR_CHECKLIST, strict=True)]


POST_REPAIR_ALL_PASS = post_repair_results()
FINDING = RepairFinding.WEAR  # slice 33: what the fixtures' repairs found (allowed for a vendor's account and on an investigation)
CHECKED = {"repair_check": RepairCheck.PASSED, "results": POST_REPAIR_ALL_PASS, "repair_finding": FINDING}


def checked_repair(**extra) -> dict:
    return {**CHECKED, **extra}


def checked_post(*values, **extra) -> dict:
    data = {"repair_check": RepairCheck.PASSED, "signature": checklist_signature(checklist_of(POST_REPAIR)), "repair_finding": FINDING}
    for n, step in enumerate(post_repair_results(*values), 1):
        data[f"step_{n}"], data[f"reading_{n}"] = step["result"], step["reading"]
    return {**data, **extra}
