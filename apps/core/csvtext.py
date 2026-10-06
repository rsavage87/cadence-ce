"""
Text that a spreadsheet would run as a formula, both ways: CSV downloads guard it (apps.web.exports), and imports (apps.imports)
take the guard off again, so a file exported from Cadence, edited in Excel, and imported back reads as it did ("-12" stays a tag,
not "'-12"). Here, not in apps.web, because imports serve the command line too and never import apps.web.
"""
import re

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
EMBEDDED_FORMULA = re.compile(r"([;\t\r\n])(?=[=+\-@])")
_GUARDED_EMBEDDED = re.compile(r"([;\t\r\n])'(?=[=+\-@])")


def guard(text: str) -> str:
    """`text` as a CSV cell: an apostrophe before a leading formula character, and after an embedded separator or line break."""
    text = EMBEDDED_FORMULA.sub(r"\1'", text)
    return "'" + text if text.startswith(FORMULA_START) else text


def unguard(text: str) -> str:
    """The inverse of guard(): a cell Cadence wrote comes back as it was stored."""
    if text.startswith("'") and text[1:].startswith(FORMULA_START):
        text = text[1:]
    return _GUARDED_EMBEDDED.sub(r"\1", text)
