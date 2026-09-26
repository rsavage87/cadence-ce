"""Contract chips and access checks. The device drawer's context is built in views.py, so it derives contract access from the user here."""
from django import template
from django.utils.html import format_html

from apps.accounts.models import Level, Module
from apps.contracts import services as ct
from apps.contracts.models import ContractType

register = template.Library()


@register.simple_tag
def contract_status_chip(contract):
    st = ct.contract_status(contract)
    return format_html('<span class="chip {}">{}</span>', st["css"], st["label"])


@register.simple_tag
def contract_type_chip(contract):
    if contract.type == ContractType.OEM:
        return format_html('<span class="chip acc">{}</span>', "OEM")
    return format_html('<span class="chip neutral">{}</span>', "Third-party")


@register.filter
def covers(models):
    """The table's Covers column: the two largest model groups, then "+N more"."""
    text = ", ".join(label for label, _ in models[:2])
    return f"{text} +{len(models) - 2} more" if len(models) > 2 else text


@register.simple_tag
def contracts_access(user):
    return {"view": user.has_level(Module.CONTRACTS, Level.VIEW), "edit": user.has_level(Module.CONTRACTS, Level.EDIT)}
