"""Request helpers shared across apps."""


def client_ip(request):
    """The caller's address for rate limits: the first X-Forwarded-For hop (the app runs behind a proxy when hosted), else
    REMOTE_ADDR. The header can be forged by a direct caller, so limits keyed on it must never be the only protection."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


class RejectNulMiddleware:
    """Answer 400 to a request that carries a NUL character in its path, query string, or form or JSON body. PostgreSQL text
    cannot hold NUL, so such a value would reach the ORM and fail as a 500 (SQLite takes it); the public portal is the easy target.
    Multipart uploads are left to the form fields, which refuse NUL themselves.

    Slice 23: a multipart body larger than settings.UPLOAD_MAX_BYTES is refused (413) here, before anything reads it: Django parses
    a multipart body (writing its files to disk) as soon as the CSRF check reads the form, before any view could look at its size.
    The length is the request's own Content-Length; Django reads no more than that."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import HttpResponseBadRequest

        if "\x00" in request.path or "%00" in request.META.get("QUERY_STRING", "").lower() or any("\x00" in v for v in request.GET.values()):
            return HttpResponseBadRequest("NUL characters are not allowed.")
        content_type = request.META.get("CONTENT_TYPE", "")
        if content_type.startswith("multipart/"):
            from django.conf import settings
            from django.http import HttpResponse

            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > settings.UPLOAD_MAX_BYTES:
                return HttpResponse("The upload is too large.", status=413, content_type="text/plain")
        if request.method in ("POST", "PUT", "PATCH") and not content_type.startswith("multipart/"):
            body = request.body
            if b"\x00" in body or b"%00" in body or b"\\u0000" in body:
                return HttpResponseBadRequest("NUL characters are not allowed.")
        return self.get_response(request)
