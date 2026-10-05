"""
Daily job (apps.jobs: report_emails): email the reports due today to the users who scheduled them, in every active facility, each
on its own day (its time zone, Tenant.timezone: a facility's Monday is its own). The daily job runs it for one facility and that
facility's day. Weekly ones go on Mondays, monthly ones on the first Monday; a subscription is never sent twice on one day, so a
rerun only retries what failed.

    python manage.py send_report_emails                                     # every facility, each as of its today
    python manage.py send_report_emails --tenant riverside                  # one facility
    python manage.py send_report_emails --date 2026-10-05                   # a missed day, sent as of that day

Prints one line per facility and the totals. Fails (exit status 1) only when an email could not be sent, after trying them all.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.jobs import cli
from apps.reports import subscriptions


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
        parser.add_argument("--tenant", help="Facility slug; default every active facility")
        parser.add_argument("--date", help="YYYY-MM-DD: send what was due that day, for a missed run; default each facility's today (its time zone)")

    def handle(self, *args, **opts):
        facilities = cli.facilities(opts["tenant"])
        day = cli.parse_day(opts["date"]) if opts["date"] else None
        if day is not None:
            cli.refuse_future(day, facilities, subscriptions.local_today)
        summary = subscriptions.send_due(day, facility=facilities[0] if opts["tenant"] else None)
        for counts in summary["tenants"]:
            self.stdout.write(_line(counts))
        self.stdout.write(f"Report emails for {cli.days_worded(summary, subscriptions.local_today)}: {summary['sent']} sent, {summary['failed']} failed, "
                          f"{summary['skipped']} skipped")
        if summary["failed"]:
            failed = [c["slug"] for c in summary["tenants"] if c["failed"]]
            raise CommandError(f"{summary['failed']} report email{'s' if summary['failed'] != 1 else ''} could not be sent ({', '.join(failed)})")
