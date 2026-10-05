"""
What the reports API returns (slice 19, part C): custom reports, a report's table, report emails (apps/api/views_reports.py), and
a Check FDA feed (apps/api/views_recalls.py). Only shapes what the services return: every change goes through apps.reports.custom,
apps.reports.subscriptions, and apps.recalls.feeds, never a serializer's save.
"""
from rest_framework import serializers

from apps.reports import custom
from apps.reports import subscriptions as subs
from apps.reports.services import find_report


def two_decimals(value):
    """A float to two decimals, as the CSV writes it; anything else as it is."""
    return round(value, 2) if isinstance(value, float) else value


def table(data: dict) -> dict:
    """A custom report's run (apps.reports.custom.run, through run_any) as the API returns it: the table the screen and the CSV
    show, floats to two decimals."""
    totals = data.get("totals")
    return {
        "columns": data["columns"],
        "kinds": data["kinds"],
        "rows": [[two_decimals(v) for v in row] for row in data["rows"]],
        "total": data["total"],
        "records": data["records"],
        "truncated": data["truncated"],
        "grouped": data["grouped"],
        "totals": None if totals is None else [two_decimals(v) for v in totals],
        "left_out": data["left_out"],
        "description": data["description"],
        "problem": data.get("problem", ""),
    }


class CustomReportSerializer(serializers.Serializer):
    """A custom report in the list: what the Reports screen's list and the panel's head say about it."""

    def to_representation(self, report):
        meta = custom.meta_of(report)  # the meta every place that shows a report by key reads
        return {"id": str(report.pk), "key": report.key, "name": report.name, "source": report.source, "source_label": meta["source_label"],
                "description": meta["subtitle"]}


class CustomReportDetailSerializer(CustomReportSerializer):
    """One custom report with its definition as it runs (apps.reports.custom.runnable): what the builder's Edit starts from, so a
    client can send back what it read. A report whose source is no longer offered shows its saved parts."""

    def to_representation(self, report):
        data = super().to_representation(report)
        d = custom.runnable(report) or custom.definition_of(report)
        return {**data, "columns": d["columns"], "filters": d["filters"], "group_by": d["group_by"], "sort": d["sort"]}


def report_email(report_key: str, sub, tenant) -> dict:
    """A report email of the signed-in user's: the report, how often, and when the next one goes out (the Schedule modal's "Next
    email"). `sub` None is a report that is not emailed (frequency ""). One the daily job would skip (the user lost access to what it
    lists, or their email address) says why in `not_sent`, in the job's words, and has no next day."""
    meta = find_report(report_key)
    title = meta["title"] if meta else None
    if sub is None:
        return {"report": meta["key"] if meta else report_key, "title": title, "frequency": "", "frequency_label": "Off", "next_on": None,
                "not_sent": None}
    reason = subs.skip_reason(sub, tenant)
    next_on = None if reason else subs.first_send_on(sub.frequency, subs.local_today(), sub.last_sent_on)
    return {"report": sub.report, "title": title, "frequency": sub.frequency, "frequency_label": sub.get_frequency_display(), "next_on": next_on,
            "not_sent": subs.SKIP_REASONS[reason] if reason else None}


def feed_check(check, message: str) -> dict:
    """What a Check FDA feed found (apps.recalls.feeds.Check), with the words the Recalls screen's toast says."""
    r = check.result
    return {
        "fetched": r is not None,  # False when the shared cooldown held the fetch back: only this facility's matching ran
        "new_notices": r.new if r else None,
        "returned": r.returned if r else None,
        "total": r.total if r else None,
        "truncated": r.truncated if r else False,
        "new_matches": check.matches,
        "checked_at": check.checked_at,
        "again_at": check.again_at,
        "last_failed": check.last_failed,
        "message": message,
    }
