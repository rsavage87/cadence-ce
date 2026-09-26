"""Chips and labels for the Technician credentials tab."""
from django import template
from django.utils.html import format_html

from apps.credentials.services import SCOPE_SHORT, credential_state

from .web import short_name

register = template.Library()


def _chip(css, label):
    return format_html('<span class="chip {}">{}</span>', css, label)


@register.filter
def scope_short(scope):
    return SCOPE_SHORT.get(scope, scope)


@register.simple_tag
def cred_state_chip(credential):
    st = credential_state(credential)
    return _chip(st["css"], st["label"])


@register.simple_tag
def coverage_chip(row):
    """Coverage status from a coverage_by_category row, worded as the mock's coverage table."""
    n = len(row["technicians"])
    if n == 0:
        return _chip("warn", "Vendor contract only") if row["vendor_contract"] else _chip("crit", "No in-house coverage")
    return _chip("warn", "Single technician") if n == 1 else _chip("ok", "Covered")


@register.simple_tag
def credentialed_names(row):
    """"Dana W., Tom O. (Hamilton-G5 only)": full coverage first, then partial coverage with what it is limited to."""
    names = [short_name(t.name) for t in row["technicians"]]
    names += [f"{short_name(t.name)} ({', '.join(values)} only)" for t, values in row["partial"]]
    return ", ".join(names)
