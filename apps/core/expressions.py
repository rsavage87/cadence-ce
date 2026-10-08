"""
Database expressions every app may use (slice 27: moved here from apps.reports.custom, so apps.pm's on-time rule can use them without
importing apps.reports).
"""
from django.db.models import Func, IntegerField


class DayNumber(Func):
    """A date as its number of days since 1970-01-01, the same integer on SQLite and PostgreSQL, so date arithmetic (days open,
    age, days after a due date) stays in the database where it sorts, averages, and groups. Django's own date subtraction gives a
    duration, which the two databases average differently, and a date plus a timedelta compiles to text on SQLite."""

    output_field = IntegerField()

    def as_sql(self, compiler, connection, **extra):  # PostgreSQL: a date minus a date is a number of days
        sql, params = compiler.compile(self.source_expressions[0])
        return f"(({sql}) - DATE '1970-01-01')", params

    def as_sqlite(self, compiler, connection, **extra):
        sql, params = compiler.compile(self.source_expressions[0])
        return f"CAST(julianday({sql}) - 2440587.5 AS INTEGER)", params
