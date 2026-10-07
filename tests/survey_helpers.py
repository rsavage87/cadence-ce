"""
Shared helpers for the survey binder's tests (slice 25). Import what you need: `from survey_helpers import period, rows_of, fake_sections`.
"""
from datetime import date

from apps.reports import survey
from apps.reports.survey import Figure, Gap, Period, Section, Table


def period(start: date, end: date, today: date | None = None) -> Period:
    return Period(start, end, today or end)


def rows_of(section: Section, table_key: str) -> list[list]:
    """Every row of one of the section's tables (calls its lazy rows)."""
    table = section.table(table_key)
    assert table is not None, f"{section.key} has no table {table_key!r}: {[t.key for t in section.tables]}"
    return [list(r) for r in table.rows()]


def gaps_of(section: Section, kind: str | None = None) -> list[Gap]:
    return [g for g in section.gaps if kind is None or g.kind == kind]


def fake_section(key: str, title: str = "", gaps=(), rows=(("A", 1),), columns=("Name", "Count")) -> Section:
    """A section with one table and the given gaps, for tests of the page, print, CSV, and API that need no real data."""
    rows = [list(r) for r in rows]
    return Section(key=key, title=title or key.title(), topic=f"{key} topic", covers="covers", figures=[Figure("Things", len(rows))],
                   gaps=list(gaps), tables=[Table(key="main", title="Main", columns=list(columns), rows=lambda: iter(rows), count=len(rows))],
                   notes=[f"{key} note"])


def fake_sections(monkeypatch, sections: dict[str, Section]):
    """Replace the registry: each real key builds the given fake section (keys not given keep an empty one), so a test of the screens
    does not depend on the sections' rules."""
    real = survey._sections()

    def fake():
        return [(key, title, (lambda p, u, key=key, title=title: sections.get(key) or Section(key=key, title=title, topic="", covers="")))
                for key, title, _build in real]

    monkeypatch.setattr(survey, "_sections", fake)
