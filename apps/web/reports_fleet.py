"""Presenters for the fleet reports: chart geometry and display values built from apps.reports.fleet's data."""
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from apps.capital import priority
from apps.facility.fiscal import describe, fy_label, month_name
from apps.pm.services import pm_on_time_series
from apps.workorders import findings

from .overview import pct_label, pm_trend_chart


def present_compliance(r: dict) -> dict:
    """The Overview's 12-month PM trend, ending at the report's month (the mock draws the same chart under this table). The series
    follow the report's clock and its PM window (slice 27: read once, with the report), so the chart and the table describe the same day
    by the same rule; the target line is the policy target the table uses."""
    today, w = r["today"], r.get("w")
    return {"chart": pm_trend_chart(pm_on_time_series(today.year, today.month, today=today, w=w),
                                    pm_on_time_series(today.year, today.month, life_support_only=True, today=today, w=w),
                                    target=r["policy_target_pct"]),
            "policy_target": pct_label(r["policy_target_pct"])}


def present_mtbf(r: dict) -> dict:
    shown = len(r["models"])
    return {"shown": shown, "truncated": r["total_models"] > shown}


FLEET_LIMIT = 12  # the screen's fleets by model (the report's data carries them all)


def flag_chips(flags) -> list[dict]:
    """A device's flags as chips: must act (parts no longer available, kept out of use after a repair) crit, review (support ended, a
    confirmed recall) warn, in FLAGS order."""
    return [{"key": f, "words": priority.FLAG_WORDS[f], "kind": "crit" if f in priority.MUST_ACT else "warn"} for f in flags]


def cost_pct(p) -> str:
    """Service cost a year against the replacement estimate as a whole percentage ("18%"), "" when unknown."""
    ratio = p.extras.get("cost_ratio")
    if ratio is None or p.component(priority.COST).value is None:
        return ""
    pct = f"{(ratio * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}%"
    return f"at least {pct}" if p.extras.get("cost_at_least") else pct  # its contract share unknown, the lines alone at the top


def suggested_words(p) -> str:
    """ "Suggested for FY2027" or "Not placed" (never "due" or "scheduled")."""
    return f"Suggested for {fy_label(p.suggested_fy)}" if p.suggested_fy is not None else "Not placed"


def unknown_words(p) -> str:
    """ "+25 unknown": the points the rule could not score beside the score ("" when every input is known). Never scored as 0."""
    return f"+{p.points_unknown} unknown" if p.points_unknown else ""


def estimate_source(p) -> str:
    """The estimate's source in a few words for a table cell ("list price", "acquisition cost, 2017"); the full words go in its title."""
    e = p.estimate
    if e.source == priority.ESTIMATE_LIST:
        return "list price"
    if e.source == priority.ESTIMATE_ACQUISITION:
        return f"acquisition cost, {p.asset.installed_on.year}" if p.asset.installed_on else "acquisition cost"
    return "no estimate"


def _top_row(p) -> dict:
    age = p.component(priority.AGE)
    return {"p": p, "asset": p.asset, "chips": flag_chips(p.flags), "age": age.input, "over_life": p.age_years is not None and p.age_years > p.life_years,
            "support": p.support.words, "failures": "" if p.failures is None else str(p.failures),
            "failures_title": p.component(priority.RELIABILITY).input, "cost_pct": cost_pct(p), "cost_title": p.component(priority.COST).input,
            "score": p.score, "points_unknown": p.points_unknown, "unknown": unknown_words(p), "estimate": p.estimate.amount,
            "estimate_words": p.estimate.words, "estimate_source": estimate_source(p), "suggested": suggested_words(p)}


def _fleet_row(f: dict) -> dict:
    dm = f["device_model"]
    rate = f["failures_per_device_year"]
    if f["median_score"] is None:
        median = ""
    elif f["min_score"] == f["max_score"]:
        median = f"{f['median_score']:g}"
    else:
        median = f"{f['median_score']:g} ({f['min_score']} to {f['max_score']})"
    return {"model": dm, "devices": f["devices"], "past_life": f["past_life"], "life_years": f["life_years"], "support": f["support"].words,
            "rate": "" if rate is None else f"{rate:.2f}", "median": median, "total": f["total"],
            "unestimated": f["unestimated"]}


GAP_LIMIT = 12  # devices or models listed under each gap; the count is every one, and the CSV carries every device
UNRANKED_LIMIT = 25  # devices listed under Not ranked; the CSV carries them all (its Ranked column says why)
# Where each gap is filled in. Life source and support are the model's (its PM program tab, Record support: slice 36 wave 2B).
GAP_FIX = {
    priority.NO_INSTALL_DATE: "Add it in the device's Edit details, or fill the CSV's Installed on column and import the file "
                              "(Settings, Import data, Devices).",
    priority.NO_ESTIMATE: "Set the model's list price in its Edit details, or the device's acquisition cost.",
    priority.LIFE_SOURCE: "Record where the model's expected life comes from: its PM program tab, Record support.",
    priority.SUPPORT_UNCHECKED: "Record what the manufacturer says about support, or that nothing is announced: the model's PM program tab, "
                                "Record support.",
}


def _gap(g: dict) -> dict:
    """One gap for the screen: its count, how it is filled in, and the first GAP_LIMIT of its models (a model's gap, each with how many
    of its devices the gap touches) or devices (by tag)."""
    of_models = g["device_models"] is not None
    if of_models:
        per_model = Counter(a.device_model_id for a in g["assets"])
        items = [{"model": dm, "devices": per_model[dm.pk]} for dm in g["device_models"]]
    else:
        items = g["assets"]
    return {"key": g["key"], "words": g["words"], "devices": g["devices"], "models": g["models"], "of_models": of_models,
            "fix": GAP_FIX[g["key"]], "items": items[:GAP_LIMIT], "more": max(len(items) - GAP_LIMIT, 0)}


def _year(y: dict, start: int, now: int) -> dict:
    title = describe(y["fy"], start)  # "FY2027: Jul 1, 2026 to Jun 30, 2027"
    return {**y, "title": title, "dates": title.partition(": ")[2], "current": y["fy"] == now}


def present_replace(r: dict) -> dict:
    """The Replacement planning panel's display values: the top rows with their chips and words, the fleets (the first FLEET_LIMIT), the
    outlook's fiscal years with their dates, the gaps with how each is filled in and the first of what they touch (the reader's access
    decides the links, in the template), the devices not ranked with why (the first UNRANKED_LIMIT), and the rule's version words."""
    start = r["start"]
    outlook = r["outlook"]
    now = outlook["years"][0]["fy"] if outlook["years"] else None
    unranked = r["unranked"]
    return {
        "shown": len(r["top"]), "rows": [_top_row(p) for p in r["top"]], "version": r["version"], "failure_rule": findings.FAILURE_RULE,
        "ranked": r["ranked_count"], "not_ranked": len(unranked),
        "fleets": [_fleet_row(f) for f in r["fleets"][:FLEET_LIMIT]], "fleets_total": len(r["fleets"]),
        "fleets_truncated": len(r["fleets"]) > FLEET_LIMIT,
        "years": [_year(y, start, now) for y in outlook["years"]], "later": outlook["later"],
        "last_label": outlook["years"][-1]["label"] if outlook["years"] else "",
        "not_placed": outlook["not_placed"], "fiscal_start": month_name(start),
        "gaps": [_gap(g) for g in r["gaps"] if g["devices"]],
        "unranked": [{"p": p, "asset": p.asset, "why": ", ".join(priority.UNRANKED_WORDS[k] for k in p.unranked_reasons), "unknown": unknown_words(p)}
                     for p in unranked[:UNRANKED_LIMIT]],
        "unranked_more": max(len(unranked) - UNRANKED_LIMIT, 0),
    }
