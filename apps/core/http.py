"""Request helpers shared across apps."""


def client_ip(request):
    """The caller's address for rate limits: the first X-Forwarded-For hop (the app runs behind a proxy when hosted), else
    REMOTE_ADDR. The header can be forged by a direct caller, so limits keyed on it must never be the only protection."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None
