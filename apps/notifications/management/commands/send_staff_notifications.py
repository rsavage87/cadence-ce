"""
Daily job (apps.jobs: staff_notifications): in every active facility, email each technician who chose it their daily digest (their
work orders due today or overdue, their PMs in the next seven days), and each person with Contracts Edit the contracts ending in 90,
30, or 7 days, or just ended (apps/notifications/daily.py). No email goes twice, so a rerun only retries what failed. Each facility
works on its own day (its time zone, Tenant.timezone); the daily job runs it for one facility and that facility's day.

    python manage.py send_staff_notifications                         # every facility, each as of its today
    python manage.py send_staff_notifications --tenant riverside      # one facility
    python manage.py send_staff_notifications --date 2026-10-05       # a missed day, as of that day

Prints one line per facility and the totals. Fails (exit status 1) only when an email could not be sent, after trying them all.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.jobs import cli
from apps.notifications import daily


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _line(counts: dict) -> str:
    if counts["error"]:
        return f"{counts['slug']}: failed: {counts['error']}"
    parts = []
    if counts["digests"] or counts["quiet"]:
        part = f"{_plural(counts['digests'], 'digest')} sent"
        if counts["quiet"]:
            part += f" ({counts['quiet']} with nothing due, not sent)"
        parts.append(part)
    if counts["reminders"]:
        parts.append(f"{_plural(counts['reminders'], 'contract reminder')} sent ({_plural(counts['reminded'], 'contract')})")
    skipped = counts["skipped"]
    if skipped:
        why = "; ".join(f"{n} {daily.KIND_WORDS[kind]}: {daily.SKIP_REASONS[reason]}" for (kind, reason), n in sorted(skipped.items()))
        parts.append(f"{sum(skipped.values())} skipped ({why})")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    return f"{counts['slug']}: " + (", ".join(parts) if parts else "nothing to send")


class Command(BaseCommand):
    help = "Email the daily digests and the contract reminders due today."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Facility slug; default every active facility")
        parser.add_argument("--date", help="YYYY-MM-DD: send as of that day, for a missed run; default each facility's today (its time zone)")

    def handle(self, *args, **opts):
        facilities = cli.facilities(opts["tenant"])
        day = cli.parse_day(opts["date"]) if opts["date"] else None
        if day is not None:
            cli.refuse_future(day, facilities, daily.local_today)
        summary = daily.send_due(day, facility=facilities[0] if opts["tenant"] else None)
        for counts in summary["tenants"]:
            self.stdout.write(_line(counts))
        self.stdout.write(f"Staff notifications for {cli.days_worded(summary, daily.local_today)}: {summary['sent']} sent, {summary['failed']} failed, "
                          f"{summary['skipped']} skipped")
        if summary["failed"]:
            failed = [c["slug"] for c in summary["tenants"] if c["failed"]]
            raise CommandError(f"{_plural(summary['failed'], 'staff notification')} could not be sent ({', '.join(failed)})")
