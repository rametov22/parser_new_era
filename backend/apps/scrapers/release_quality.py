import re
from collections.abc import Iterable
from typing import Any


_TEXT_KEYS = {
    "audio",
    "audio_track",
    "label",
    "name",
    "quality",
    "title",
    "track",
    "translation",
    "translator",
    "voice",
}
_SKIP_KEYS = {"file", "href", "link", "src", "url"}
_URLISH_RE = re.compile(
    r"(^[a-z][a-z0-9+.-]*:|//|\.m3u8(\?|#|$)|\.mp4(\?|#|$)|"
    r"\.mkv(\?|#|$)|\.avi(\?|#|$)|\.ts(\?|#|$))",
    re.IGNORECASE,
)
_PIRATED_RELEASE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?<![a-z0-9])cam[\s._-]*rip(?![a-z0-9])",
        r"(?<![a-z0-9])hd[\s._-]*cam(?![a-z0-9])",
        r"(?<![a-z0-9])cam(?![a-z0-9])",
        r"(?<![a-z0-9])hd[\s._-]*ts(?![a-z0-9])",
        r"(?<![a-z0-9])tele[\s._-]*sync(?![a-z0-9])",
        r"(?<![a-z0-9])telesync(?![a-z0-9])",
        r"(?<![a-z0-9])ts(?![a-z0-9])",
        r"(?<![a-z0-9])tele[\s._-]*cine(?![a-z0-9])",
        r"(?<![a-z0-9])telecine(?![a-z0-9])",
        r"(?<![a-z0-9])tc(?![a-z0-9])",
        r"(?<![a-z0-9])dvd[\s._-]*scr(?:eener)?(?![a-z0-9])",
        r"(?<![a-z0-9])screener(?![a-z0-9])",
        r"(?<![а-яёa-z0-9])экранк[аи](?![а-яёa-z0-9])",
        r"(?<![а-яёa-z0-9])кам[\s._-]*рип(?![а-яёa-z0-9])",
        r"(?<![а-яёa-z0-9])тс(?![а-яёa-z0-9])",
    )
)


def has_pirated_release(*sources: Any) -> bool:
    labels = list(_iter_text_values(sources))
    return bool(labels) and all(_matches_pirated_release(text) for text in labels)


def _matches_pirated_release(text: str) -> bool:
    if not text or _looks_like_url(text):
        return False
    normalized = (
        text.replace("\u00a0", " ")
        .replace("–", "-")
        .replace("—", "-")
        .strip()
        .lower()
    )
    return any(pattern.search(normalized) for pattern in _PIRATED_RELEASE_PATTERNS)


def _looks_like_url(value: str) -> bool:
    return bool(_URLISH_RE.search(value.strip()))


def _iter_text_values(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        if not parent_key or parent_key.lower() in _TEXT_KEYS:
            yield value
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).lower()
            if key_text in _SKIP_KEYS:
                continue
            yield from _iter_text_values(nested, parent_key=key_text)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_text_values(nested, parent_key=parent_key)
