"""Naming convention: interpolate an output filename stem from a row's fields.

Template lives in the sheet, e.g. '{campaign}_{market}_{format}_{version}'.
Missing keys resolve to empty; result is sanitized to a safe filename stem.
Extension (.mp4/.png) is added by the caller.

Accents are transliterated rather than stripped: a French campaign named
"Soldes Été" has to come out `Soldes-Ete`, not `Soldes--t-`. The output stem is
the filename a media owner receives, so it stays ASCII, but it stays readable.
"""
from __future__ import annotations

import re
import unicodedata

_FIELD = re.compile(r"\{([a-zA-Z0-9_]+)\}")

# Characters NFKD cannot decompose into ASCII + combining marks.
_TRANSLIT = str.maketrans({
    "œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ß": "ss",
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    "&": "-et-", "€": "EUR", "%": "pct", "'": "-", "’": "-",
})


def render_name(template: str, fields: dict) -> str:
    """Interpolate `template` with `fields`, then sanitize.

    >>> render_name('{campaign}_{market}', {'campaign': 'Soldes Été', 'market': 'FR'})
    'Soldes-Ete_FR'
    """
    def repl(m: re.Match) -> str:
        return str(fields.get(m.group(1), "") or "")

    return sanitize(_FIELD.sub(repl, template))


def sanitize(stem: str) -> str:
    """ASCII, filesystem-safe, readable. Never empty."""
    stem = unicodedata.normalize("NFKD", str(stem).translate(_TRANSLIT))
    stem = "".join(c for c in stem if not unicodedata.combining(c))
    stem = re.sub(r"\s+", "-", stem.strip())
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem)      # collapse runs left by dropped chars
    stem = re.sub(r"_{2,}", "_", stem)      # ... and by empty template fields
    return stem.strip("-_") or "creative"
