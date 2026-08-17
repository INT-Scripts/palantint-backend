"""Name normalization shared by the ingest pipeline and the API.

Lives in `db/` rather than `api/` on purpose: the ETL loaders (a separate
package that only depends on `db` and `core`) must compute exactly the same
key the API matches on — any drift between the two would silently break every
name-based link.
"""
import re
import unicodedata
from typing import Optional

_NAME_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)


def person_name_key(*parts: Optional[str]) -> str:
    """Accent-, case- and order-insensitive key for a human name.

    "Jean-Pierre DURAND", "durand jean pierre" and Person(first_name="Jean-Pierre",
    last_name="Durand") all collapse to "durand jean pierre" — the course catalog
    writes "Prénom NOM" while compound surnames ("Emelina CUCUNUBA BARRERA") make
    the first/last split unreliable, so tokens are sorted instead of positional.
    """
    raw = " ".join(p for p in parts if p)
    folded = unicodedata.normalize("NFKD", raw)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    tokens = [t for t in _NAME_SEPARATORS.split(folded.lower()) if t]
    return " ".join(sorted(tokens))
