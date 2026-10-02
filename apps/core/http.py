"""Request helpers shared across apps."""


def client_ip(request):
    """The caller's address for rate limits: the first X-Forwarded-For hop (the app runs behind a proxy when hosted), else
    REMOTE_ADDR. The header can be forged by a direct caller, so limits keyed on it must never be the only protection."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


class RejectNulMiddleware:
    """Answer 400 to a request that carries a NUL character in its path, query string, or form or JSON body. PostgreSQL text
    cannot hold NUL, so such a value would reach the ORM and fail as a 500 (SQLite takes it); the public portal is the easy target.
    Multipart uploads are left to the form fields, which refuse NUL themselves."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import HttpResponseBadRequest

        if "\x00" in request.path or "%00" in request.META.get("QUERY_STRING", "").lower() or any("\x00" in v for v in request.GET.values()):
            return HttpResponseBadRequest("NUL characters are not allowed.")
        content_type = request.META.get("CONTENT_TYPE", "")
        if request.method in ("POST", "PUT", "PATCH") and not content_type.startswith("multipart/"):
            body = request.body
            if b"\x00" in body or b"%00" in body or b"\\u0000" in body:
                return HttpResponseBadRequest("NUL characters are not allowed.")
        return self.get_response(request)
