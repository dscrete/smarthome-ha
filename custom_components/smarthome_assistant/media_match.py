"""Deterministic choice of one Music Assistant search result.

Pure functions only (no Home Assistant imports) so the scoring can be unit
tested directly. Core supplies the words a person said; this module decides
which catalog item they most plausibly meant, or that none is close enough.
The model never chooses a URI.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata
from typing import Any, Iterable

KINDS = ("track", "artist", "album", "playlist", "radio")
# Music Assistant search response keys for each requested kind.
RESULT_KEYS = {"track": "tracks", "artist": "artists", "album": "albums",
               "playlist": "playlists", "radio": "radio"}

# Accept a candidate at or above this score. Station names are looser
# ("radio one" vs "BBC Radio 1"), so radio uses a lower bar.
ACCEPT = {"radio": 0.6}
DEFAULT_ACCEPT = 0.72
# Below acceptance, offer the best item as "did you mean" only above this.
SUGGEST = 0.45
# Required artist similarity when the person named an artist.
ARTIST_REQUIRED = 0.6

# Alternative renditions are only wanted when asked for by name.
_VARIANT_WORDS = ("live", "karaoke", "instrumental", "remix", "cover", "acoustic",
                  "sped up", "slowed", "nightcore", "tribute", "8d", "reverb",
                  "originally performed", "demo")
_VARIANT_PENALTY = 0.15
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DASH_SUFFIX = re.compile(
    r"\s+-\s+.*\b(remaster(ed)?|live|version|edit|mix|mono|stereo|deluxe|bonus)\b.*$")
_NON_WORD = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")
# Speech recognisers write small numbers as words; catalogs use digits.
_NUMBERS = {word: str(index) for index, word in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}


def normalize(value: Any, *, strip_variants: bool = True) -> str:
    """Fold case/accents and remove decoration that people do not say."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = text.replace("&", " and ")
    if strip_variants:
        text = _BRACKETS.sub(" ", text)
        text = _DASH_SUFFIX.sub(" ", text)
    text = _NON_WORD.sub(" ", text.replace("'", ""))
    text = " ".join(_NUMBERS.get(word, word) for word in text.split())
    if text.startswith("the "):
        text = text[4:]
    return text


def similarity(query: Any, name: Any) -> float:
    """Score how well a spoken phrase matches a catalog name, 0..1."""
    q, n = normalize(query), normalize(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q.replace(" ", "") == n.replace(" ", ""):
        return 0.98
    ratio = SequenceMatcher(None, q, n).ratio()
    q_words, n_words = q.split(), n.split()
    shorter, longer = sorted((q_words, n_words), key=len)
    if shorter and _contains_run(longer, shorter):
        ratio = max(ratio, 0.8 + 0.15 * len(shorter) / len(longer))
    return ratio


def _contains_run(words: list[str], run: list[str]) -> bool:
    size = len(run)
    return any(words[index:index + size] == run for index in range(len(words) - size + 1))


def _artist_names(item: dict[str, Any]) -> list[str]:
    artists = item.get("artists") or []
    return [str(artist.get("name")) for artist in artists
            if isinstance(artist, dict) and artist.get("name")]


def _variant_adjustment(query: str, item: dict[str, Any]) -> float:
    """Prefer the rendition asked for: penalise unrequested variants, reward requested ones."""
    described = normalize(f"{item.get('name', '')} {item.get('version', '')}", strip_variants=False)
    asked = normalize(query, strip_variants=False)
    adjustment = 0.0
    for word in _VARIANT_WORDS:
        pattern = rf"\b{re.escape(word)}\b"
        in_item, in_query = bool(re.search(pattern, described)), bool(re.search(pattern, asked))
        if in_item and not in_query:
            adjustment = min(adjustment, -_VARIANT_PENALTY)
        elif in_query:
            adjustment += 0.05 if in_item else -0.05
    return adjustment


def score_item(kind: str, query: str, item: dict[str, Any], *, artist: str | None = None,
               position: int = 0, total: int = 1) -> float:
    """Score one search result; zero means it must not be chosen.

    Scores are not capped here so tie-breaks survive between exact matches;
    ``choose`` reports a value clamped to 0..1.
    """
    if not isinstance(item, dict) or not item.get("uri") or not item.get("name"):
        return 0.0
    title = similarity(query, item["name"])
    if item.get("version"):
        title = max(title, similarity(query, f"{item['name']} {item['version']}"))
    if artist and kind in {"track", "album"}:
        names = _artist_names(item)
        artist_score = max((similarity(artist, name) for name in names), default=0.0)
        if artist_score < ARTIST_REQUIRED:
            return 0.0
        score = 0.7 * title + 0.3 * artist_score
    else:
        score = title
    score += _variant_adjustment(query, item)
    if item.get("favorite") or str(item.get("uri", "")).startswith("library://"):
        score += 0.03
    # Keep Music Assistant's provider ranking as a small tiebreak.
    score += 0.02 * (1 - position / max(total, 1))
    return max(0.0, score)


def choose(kind: str, query: str, items: Iterable[dict[str, Any]], *,
           artist: str | None = None) -> dict[str, Any]:
    """Return ``{"item", "score"}`` for a confident match, else ``{"suggestion"}``."""
    if kind not in KINDS:
        raise ValueError("unsupported media kind")
    candidates = [item for item in items if isinstance(item, dict)]
    scored = sorted(((score_item(kind, query, item, artist=artist, position=index,
                                 total=len(candidates)), index, item)
                     for index, item in enumerate(candidates)),
                    key=lambda entry: (-entry[0], entry[1]))
    if not scored or scored[0][0] <= 0:
        return {"item": None, "score": 0.0, "suggestion": None}
    best_score, _index, best = scored[0]
    reported = round(min(best_score, 1.0), 3)
    if best_score >= ACCEPT.get(kind, DEFAULT_ACCEPT):
        return {"item": best, "score": reported, "suggestion": None}
    return {"item": None, "score": reported,
            "suggestion": describe(best) if best_score >= SUGGEST else None}


def choose_spoken(kind: str, query: str, items: Iterable[dict[str, Any]], *,
                  artist: str | None = None) -> dict[str, Any]:
    """Like ``choose``, but honour an unsplit "<title> by <artist>" first.

    "Hello by Adele" must not accept Lionel Richie's "Hello" just because the
    title words are contained in the query. Titles that merely contain "by"
    ("Stand By Me") fall back to the whole phrase when the split finds nothing.
    """
    items = list(items)
    if not artist and kind in {"track", "album"} and (split := split_by_artist(query)):
        by_artist = choose(kind, split[0], items, artist=split[1])
        if by_artist["item"] is not None:
            return by_artist
    return choose(kind, query, items, artist=artist)


def split_by_artist(query: str) -> tuple[str, str] | None:
    """Split "<title> by <artist>" when the interpreter left them together."""
    match = re.match(r"^\s*(.+?)\s+by\s+(.+?)\s*$", query or "", re.IGNORECASE)
    if not match or not match.group(1).strip() or not match.group(2).strip():
        return None
    return match.group(1).strip(), match.group(2).strip()


def describe(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Speakable facts about a chosen item; never the raw provider payload."""
    if not item:
        return None
    album = item.get("album")
    return {
        "name": item.get("name"),
        "artists": _artist_names(item),
        "album": album.get("name") if isinstance(album, dict) else None,
        "version": item.get("version") or None,
        "media_type": str(item.get("media_type")) if item.get("media_type") is not None else None,
        "uri": item.get("uri"),
    }
