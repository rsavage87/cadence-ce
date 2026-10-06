"""
Who may import what (slice 23). The Import data page needs Settings View (closed to scoped users, like every Settings page); each
kind needs its own module's level, the level of the screen that does the same thing one record at a time (Kind.module, Kind.level).
Checked at every step (upload, columns, check, import, discard) on the kind stored with the run, never on the page alone. Within a
kind, a row that needs more (retiring a device, the CMS OEM-schedule mark) is checked by the importer and skipped with a reason.
"""
from apps.accounts.models import Level, Module
from apps.workorders import scoping

from . import kinds

PAGE_MODULE, PAGE_LEVEL = Module.SETTINGS, Level.VIEW


def can_import(user, kind: str) -> bool:
    """Whether `user` may import files of `kind` (None: the command line, which may)."""
    if user is None:
        return True
    importer = kinds.get(kind)
    return bool(importer and not scoping.is_scoped(user) and user.has_level(importer.module, importer.level))


def importable(user) -> list:
    """The kinds `user` may import, in onboarding order."""
    return [imp for imp in kinds.KINDS.values() if can_import(user, imp.kind)]
