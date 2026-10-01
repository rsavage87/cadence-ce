"""
Daily job (apps.jobs: report_emails): email the reports due today to the users who scheduled them, in every active facility.
Weekly ones go on Mondays, monthly ones on the first Monday; a subscription is never sent twice on one day, so a rerun only
retries what failed.

    python manage.py send_report_emails                     # today (local time)
    python manage.py send_report_emails --date 2026-10-05   # a missed day, sent as of that day

Prints one line per facility and the totals. Fails (exit status 1) only when an email could not be sent, after trying them all.
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.reports import subscriptions


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise CommandError(f"--date must be YYYY-MM-DD; got {value!r}") from e


def _line(counts: dict) -> str:
    if counts["error"]:
        return f"{counts['slug']}: failed: {counts['error']}"
    if not counts["due"]:
        return f"{counts['slug']}: nothing due"
    parts = [f"{counts['due']} due", f"{counts['sent']} sent"]
    skipped = counts["skipped"]
    if skipped:
        why = ", ".join(f"{n} {subscriptions.SKIP_REASONS[key]}" for key, n in sorted(skipped.items()))
        parts.append(f"{sum(skipped.values())} skipped ({why})")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    return f"{counts['slug']}: " + ", ".join(parts)


class Command(BaseCommand):
    help = "Email the reports due today to the users who scheduled them."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="YYYY-MM-DD: send what was due that day, for a missed run; default today (local time)")

    def handle(self, *args, **opts):
        today = subscriptions.local_today()
        day = _day(opts["date"]) if opts["date"] else today
        if day > today:
            raise CommandError("--date cannot be in the future.")
        summary = subscriptions.send_due(day)
        for counts in summary["tenants"]:
            self.stdout.write(_line(counts))
        self.stdout.write(f"Report emails for {day:%Y-%m-%d}: {summary['sent']} sent, {summary['failed']} failed, {summary['skipped']} skipped")
        if summary["failed"]:
            failed = [c["slug"] for c in summary["tenants"] if c["failed"]]
            raise CommandError(f"{summary['failed']} report email{'s' if summary['failed'] != 1 else ''} could not be sent ({', '.join(failed)})")
