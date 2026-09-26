"""Server-side permission checks. Hidden buttons are not security; these are."""
from functools import wraps

from django.core.exceptions import PermissionDenied

from .models import Level


def require_level(module: str, level: int):
    """Decorator for Django views: user must hold at least `level` on `module`."""

    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated or not user.has_level(module, level):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return deco


READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def level_required_for_method(method: str, write_level: int = Level.EDIT, delete_level: int = Level.FULL) -> int:
    if method in READ_METHODS:
        return Level.VIEW
    if method == "DELETE":
        return delete_level
    return write_level
