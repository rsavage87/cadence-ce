"""
Sending the account emails (invitations, password resets). One place decides how a failure is handled: it is logged and
reported to the caller as False, never raised, so a mail outage cannot undo the account change that asked for the email.
"""
import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string

log = logging.getLogger(__name__)


def send(to: str, template: str, context: dict) -> bool:
    """Render `<template>_subject.txt` and `<template>_body.txt` and send them to `to`. True once the backend accepted it."""
    subject = " ".join(render_to_string(f"{template}_subject.txt", context).split())  # one line, whatever the template's whitespace
    body = render_to_string(f"{template}_body.txt", context)
    try:
        EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [to]).send(fail_silently=False)
    except (smtplib.SMTPException, OSError):
        log.exception("Could not send %s to %s", template, to)
        return False
    return True
