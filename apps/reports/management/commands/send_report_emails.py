"""Email the reports due today to the users who asked for them (slice 13). Scaffold: agent A builds it."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Email the reports due today to the users who asked for them."

    def handle(self, *args, **opts):
        self.stdout.write("Report emails are not built yet.")
