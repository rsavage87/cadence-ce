"""
Access levels for Reports (slice 18), shared by the screen, its CSV and print pages, the builder, and the report emails, so none
of them is the weaker door.

Reports View runs every report, the eight standard ones and the facility's custom reports. Building, changing, and deleting a
custom report need Reports Edit: the director and "Finance and quality" by default.

A custom report lists records, which the standard reports only add up: a work order source (work orders, labor lines, part lines)
lists work orders, and the devices source lists devices. So running one also needs View on that module (SOURCE_NEEDS): the screen,
the CSV, the print page, and the builder's preview refuse someone without it, with refusal()'s plain words, and the report emails
skip them. A builder can only build on what they can see. Scoped users (apps.workorders.scoping) are refused everywhere in Reports
already (web_view, and the emails' own check): every report covers the whole facility.
"""
from apps.accounts.models import Level, Module

from .models import CustomReport

MODULE = Module.REPORTS
VIEW_LEVEL = Level.VIEW  # run any report, standard or custom
BUILD_LEVEL = Level.EDIT  # build, change, or delete a custom report

Source = CustomReport.Source
# What a source lists, and the access it needs beyond Reports View: (module, level, what it lists in words).
SOURCE_NEEDS = {
    Source.WORK_ORDERS: (Module.WORKORDERS, Level.VIEW, "work orders"),
    Source.LABOR: (Module.WORKORDERS, Level.VIEW, "work orders' labor"),
    Source.PARTS: (Module.WORKORDERS, Level.VIEW, "work orders' parts"),
    Source.DEVICES: (Module.EQUIPMENT, Level.VIEW, "devices"),
}


def can_build(user) -> bool:
    """Build a custom report, change one, or delete one."""
    return user.has_level(MODULE, BUILD_LEVEL)


def needs(source: str) -> tuple:
    """((module, level),) the source needs beyond Reports View; () for a source no longer offered (it lists nothing)."""
    need = SOURCE_NEEDS.get(source)
    return ((need[0], need[1]),) if need else ()


def _need_words(module, level) -> str:
    return f"{Module(module).label} {Level(level).label}"


def refusal(user, source: str) -> str:
    """Why `user` may not see a report on `source`, in plain words; "" when they may."""
    need = SOURCE_NEEDS.get(source)
    if need is None or user.has_level(need[0], need[1]):
        return ""
    return f"This report lists {need[2]}, so it needs {_need_words(need[0], need[1])}, which your role does not have."


def meta_refusal(user, meta: dict) -> str:
    """refusal() for a report found by key (apps.reports.services.find_report): "" for the standard reports."""
    return refusal(user, meta["source"]) if meta.get("custom") else ""


def build_refusal(user, source: str) -> str:
    """Why `user` may not build a report on `source`; "" when they may."""
    need = SOURCE_NEEDS.get(source)
    if need is None or user.has_level(need[0], need[1]):
        return ""
    return f"You need {_need_words(need[0], need[1])} to build a report that lists {need[2]}."


def email_refusal(user, source: str) -> str:
    """Why `user` may not have a report on `source` emailed; "" when they may."""
    need = SOURCE_NEEDS.get(source)
    if need is None or user.has_level(need[0], need[1]):
        return ""
    return f"You need {_need_words(need[0], need[1])} to have this report emailed: it lists {need[2]}."
