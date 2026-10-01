"""QR codes as inline SVG (slice 11): the request link on asset labels. segno is pure Python, so no image files or network."""
import segno
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe


def qr_svg(text: str, label: str) -> SafeString:
    """`text` as a QR code: black modules on white with the standard four-module quiet zone, error correction M (still
    scans with a scuffed corner). The SVG has a viewBox and no width or height, so CSS sizes it; role="img" and the
    escaped `label` give screen readers a name for it.

    Marking it safe is sound: segno writes only the viewBox, the colors and classes passed here, and path data made of
    numbers and drawing commands. `text` is encoded into the modules and never appears in the markup (no title or desc
    is passed), and `label` is escaped before it goes in."""
    svg = segno.make(text, error="m").svg_inline(dark="#000", light="#fff", border=4, svgclass="qr", lineclass=None, omitsize=True)
    return mark_safe(svg.replace("<svg ", f'<svg role="img" aria-label="{escape(label)}" ', 1))
