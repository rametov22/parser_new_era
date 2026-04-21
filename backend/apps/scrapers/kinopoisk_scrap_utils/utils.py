import re


def normalize_film_href(films_href: str) -> str:
    return films_href.replace("series/", "film/")


def clean_role(role_text: str) -> str:
    if not role_text:
        return ""

    role_text = role_text.replace("\xa0", " ")

    role_text = re.sub(r",\s*[\$0-9 ].*$", "", role_text)

    return role_text.strip()


def extract_int(text):
    if not text:
        return None
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


PLACEHOLDER_PATTERNS = [
    "placeholder",
    "projector-logo",
    "common-static",
]


def is_placeholder(url: str) -> bool:
    if not url:
        return True

    if url.endswith(".svg"):
        return True

    for pattern in PLACEHOLDER_PATTERNS:
        if pattern in url:
            return True

    return False
