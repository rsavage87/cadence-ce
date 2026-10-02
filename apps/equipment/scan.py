"""
Scan tag (slice 18): which device a scanned or typed code names. The Equipment screen's Scan tag (apps/web/views_scan.py) takes the
code from a handheld scanner, a camera, or the keyboard; this module is the rule for reading it.

A code is one of:
- A printed label's QR code: the device's request link (apps.facility.services.asset_request_url: PORTAL_BASE_URL +
  /r/<slug>/?asset=<tag>). Any scheme and host, or none (the label prints the link without its scheme for people to type):
  PORTAL_BASE_URL differs between deployments, and labels outlive a move. The path must be the portal's request route and the slug
  this facility's. Another facility's label says only that (ANOTHER_FACILITY), never whether its tag is a device here too.
- A link to the device's own page, /equipment/<tag>/ (any scheme and host, or the path alone).
- A plain tag, as a handheld barcode scanner types it (a keyboard wedge, which may end the code with CR/LF or a tab).

Surrounding whitespace and control characters are stripped (clean); a code longer than MAX_LENGTH is refused. Tags have no spaces or
slashes (TAG_VALIDATOR), so a code with a slash is a link. The device is looked up among the devices the caller passes (the user's
own: apps.workorders.scoping.assets) by tag in any letter case, as tags are unique in any letter case (create_asset, the importer).
A device outside them reads exactly like a tag that does not exist (NO_DEVICE).
"""
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit

from django.core.exceptions import ValidationError

from .models import TAG_VALIDATOR, Asset

MAX_LENGTH = 300
TAG_MAX_LENGTH = Asset._meta.get_field("tag").max_length

# This app's routes (config/urls.py "r/" and apps/portal/urls.py "<slug:tenant_slug>/"; apps/web/urls.py "equipment/<str:tag>/"), in any
# letter case: some scanners and QR generators send a link in capitals. tests/test_scan.py reads asset_request_url's links back.
PORTAL_PATH = re.compile(r"^/r/(?P<slug>[-\w]+)/?$", re.IGNORECASE | re.ASCII)
DEVICE_PATH = re.compile(r"^/equipment/(?P<tag>[^/]+)/?$", re.IGNORECASE)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

ANOTHER_FACILITY = "This label belongs to another facility."
TOO_LONG = f"That code is longer than {MAX_LENGTH} characters, so it is not an asset tag or a Cadence label."
NOT_A_LABEL = "That code is not an asset tag or a Cadence label."
REQUEST_FORM = "That link is the request form, not a device's label."
NO_DEVICE = "No device with tag “{tag}”."


def _trimmed(ch: str) -> bool:
    # Whitespace (a scanner's CR/LF or tab), control characters (STX/ETX framing), and format characters (a byte-order mark, a
    # zero-width space pasted along with a tag).
    return ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf")


def clean(code: str) -> str:
    """`code` without surrounding whitespace and control characters."""
    code = code or ""
    start, end = 0, len(code)
    while start < end and _trimmed(code[start]):
        start += 1
    while end > start and _trimmed(code[end - 1]):
        end -= 1
    return code[start:end]


def _link(code: str):
    """The parts of a link: with a scheme, a path alone, or a host and path without a scheme (as the label prints it)."""
    if SCHEME.match(code) or code.startswith("/"):
        return urlsplit(code)
    return urlsplit("//" + code)


def tag_in(code: str, slug: str) -> str:
    """The tag `code` names, for the facility whose portal slug is `slug`. ValidationError, in plain words, when it names none."""
    code = clean(code)
    if len(code) > MAX_LENGTH:
        raise ValidationError(TOO_LONG)
    if "/" not in code:
        return code
    try:
        parts = _link(code)
    except ValueError:  # a malformed host, such as an unclosed [
        raise ValidationError(NOT_A_LABEL) from None
    if m := PORTAL_PATH.match(parts.path):
        if m.group("slug").lower() != slug.lower():
            raise ValidationError(ANOTHER_FACILITY)
        tag = next((clean(value) for key, value in parse_qsl(parts.query) if key.lower() == "asset"), "")
        if not tag:
            raise ValidationError(REQUEST_FORM)
        return tag
    if m := DEVICE_PATH.match(parts.path):
        return clean(unquote(m.group("tag")))
    raise ValidationError(NOT_A_LABEL)


def is_tag(tag: str) -> bool:
    """Whether `tag` could be a device's tag at all (no spaces, slashes, or control characters, within the column); one that cannot
    is looked up nowhere. Control characters matter: a link's ?asset= or path is decoded once more here, so a %00 inside it arrives
    as a NUL the request's own check (apps.core.http.RejectNulMiddleware) never saw, and PostgreSQL refuses a NUL in a query."""
    if not tag or len(tag) > TAG_MAX_LENGTH or any(unicodedata.category(ch) == "Cc" for ch in tag):
        return False
    try:
        TAG_VALIDATOR(tag)
    except ValidationError:
        return False
    return True


def find(code: str, *, slug: str, qs) -> Asset:
    """The device `code` names among `qs` (the devices the user may see, in the request's facility, whose portal slug is `slug`).
    ValidationError with the words to show when there is none: the same NO_DEVICE whether the tag exists nowhere or only outside
    `qs`."""
    tag = tag_in(code, slug)
    if any(unicodedata.category(ch) == "Cc" for ch in tag):  # e.g. a %00 decoded from a link: no tag, and never echoed back
        raise ValidationError(NOT_A_LABEL)
    asset = qs.filter(tag__iexact=tag).first() if is_tag(tag) else None
    if asset is None:
        raise ValidationError(NO_DEVICE.format(tag=tag))
    return asset
