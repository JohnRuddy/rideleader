import re
import unicodedata

_ARTICLES = re.compile(r"\b(the|a|an|of)\b\s*", re.I)


def ascii_fold(s: str) -> str:
    """Café -> Cafe. Many head units have fonts without accented glyphs."""
    s = s.replace("ø", "o").replace("Ø", "O").replace("ß", "ss")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def short_name(name: str, limit: int, fold: bool = True) -> str:
    """Fit a name into `limit` characters: fold accents, collapse spaces, drop articles, then cut."""
    s = re.sub(r"\s+", " ", ascii_fold(name) if fold else name).strip()
    if len(s) > limit:
        s = _ARTICLES.sub("", s).strip() or s
    if len(s) > limit:
        s = s[:limit].rstrip()
    return s or "Point"
