"""A custom report's cells (slice 18): each value as its column's kind reads, escaped (names and parts are user text), with numbers
aligned right. The CSV carries the plain values; this is the screen's and the print page's reading of them."""
from datetime import date

from django import template
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe

from apps.reports import custom

register = template.Library()

EMPTY = "—"


def _number(v, places: int = 1) -> str:
    return f"{v:,}" if isinstance(v, int) else f"{v:,.{places}f}"


def _trimmed(v) -> str:
    """Hours and quantities as typed: 1.5, 2, 0.25."""
    text = f"{v:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def cell_text(v, kind: str) -> str:
    if v is None:
        return EMPTY
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, date):
        return f"{v:%b} {v.day}, {v.year}"
    if kind == custom.MONEY:
        return f"${v:,.2f}"
    if kind in (custom.HOURS, custom.QUANTITY):
        return _trimmed(v)
    if kind == custom.PERCENT:
        return f"{v:.0f}%"
    if kind in (custom.DAYS, custom.YEARS, custom.NUMBER):
        return _number(v)
    return str(v)


@register.simple_tag
def cr_cells(row, kinds, total=False):
    """The <td> cells of one row. On the totals row (total=True) an empty first cell reads "Total"."""
    cells = []
    for i, (v, kind) in enumerate(zip(row, kinds)):
        classes = []
        if kind in custom.NUMERIC_KINDS:
            classes.append("num")
        if total and i == 0 and v is None:
            text = "Total"
        else:
            text = cell_text(v, kind)
            if v is None:
                classes.append("muted")
        cells.append((mark_safe(f' class="{" ".join(classes)}"') if classes else "", text))
    return format_html_join("", "<td{}>{}</td>", cells)


@register.simple_tag
def cr_head(head):
    """The <th> cells of a head from apps.web.reports_custom.present_custom."""
    return format_html_join("", "<th{}>{}</th>", ((mark_safe(' class="num"') if h["num"] else "", h["label"]) for h in head))

