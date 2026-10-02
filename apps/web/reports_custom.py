"""
Presentation for custom reports (slice 18): what a custom report's panel, its print page, and the builder's preview show beyond the
run itself (apps.reports.custom.run), and the builder's form laid out from the source's registry. Services never import this.

The screen and the print page show the first SCREEN_ROWS rows of a run; the CSV carries every row the run listed (up to
custom.MAX_ROWS). The preview runs PREVIEW_ROWS rows and says how many there are in all.
"""
from apps.reports import custom

NUMERIC_KINDS = custom.NUMERIC_KINDS


def _count_text(r: dict, shown: int, preview: bool) -> str:
    total, limit = r["total"], r["limit"]
    unit = "group" if r["grouped"] else "row"
    whole = f"{total:,} {unit}{'' if total == 1 else 's'}"
    if r["grouped"]:
        records = r["records"]
        whole += f" from {records:,} {r['noun'] if records != 1 else r['noun'][:-1]}"
    if shown >= total:
        return f"{whole}."
    if preview:
        return f"Preview: the first {shown:,} of {whole}."
    csv = "the CSV has all of them" if total <= limit else f"the CSV has the first {limit:,}"
    return f"Showing the first {shown:,} of {whole}; {csv}."


def present_custom(r: dict, rows: int | None = None, preview: bool = False) -> dict:
    """The panel's table: its head (labels, which align right), the first `rows` rows (custom.SCREEN_ROWS), and the count in words."""
    shown = r["rows"][:custom.SCREEN_ROWS if rows is None else rows]
    return {"head": [{"label": label, "num": kind in NUMERIC_KINDS} for label, kind in zip(r["columns"], r["kinds"])],
            "rows": shown, "count_text": _count_text(r, len(shown), preview) if r["columns"] else ""}


# --- the builder's form ------------------------------------------------------------------------------------------------------

def blank(source: str, name: str = "") -> dict:
    """What a new report starts with on `source`: its default columns, no filters, no grouping, the source's own order."""
    spec = custom.spec_of(source)
    return {"name": name, "source": source, "columns": list(spec.defaults) if spec else [], "filters": {}, "group_by": "", "sort": ""}


def builder_fields(source: str, typed: dict, errors: dict) -> dict | None:
    """The source's part of the builder (#cr-fields): its columns, filters with this facility's choices, the date range, grouping,
    and sort, each marked with what was typed. None for a source that is not offered."""
    spec = custom.spec_of(source)
    if spec is None:
        return None
    chosen = typed.get("columns") or []
    filters = typed.get("filters") or {}
    out_filters = []
    for f in spec.filters:
        picked = set(filters.get(f.key) or [])
        options = [{"value": v, "label": label, "checked": v in picked} for v, label in custom.filter_options(f)]
        if f.kind == "category":  # a saved category no model has now stays checked, so saving an edit keeps the filter
            offered = {o["value"] for o in options}
            options += [{"value": v, "label": f"{v} (no model has it now)", "checked": True}
                        for v in filters.get(f.key) or [] if isinstance(v, str) and v not in offered]
        out_filters.append({"key": f.key, "label": f.label, "options": options, "long": len(options) > 8,
                            "error": errors.get(f"filter_{f.key}", "")})
    date = filters.get("date") if isinstance(filters.get("date"), dict) else {}
    sort = typed.get("sort") or ""
    sort_key = sort.lstrip("-")
    sorts = [("count", "Count (grouped tables)")] + [(c.key, c.label) for c in spec.columns]
    sorts += [(k, label) for k, label in custom.group_options(spec) if k.endswith("_month")]
    return {
        "columns": [{"key": c.key, "label": c.label, "checked": c.key in chosen} for c in spec.columns],
        "filters": out_filters,
        "date": {"fields": [{"key": k, "label": spec.column(k).label, "selected": k == date.get("field")} for k in spec.dates],
                 "periods": [{"key": k, "label": label, "selected": k == date.get("period")} for k, label in custom.PERIODS.items()],
                 "from": date.get("from") or "", "to": date.get("to") or "", "error": errors.get("date", "")},
        "groups": [{"key": k, "label": label, "selected": k == typed.get("group_by")} for k, label in custom.group_options(spec)],
        "sorts": [{"key": k, "label": label, "selected": k == sort_key} for k, label in sorts],
        "descending": sort.startswith("-"),
        "max_columns": custom.MAX_COLUMNS,
    }
