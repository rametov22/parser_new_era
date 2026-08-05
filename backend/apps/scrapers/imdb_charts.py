import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from bs4 import BeautifulSoup
from decouple import config
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone
from requests.adapters import HTTPAdapter
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.util.retry import Retry

from .chrome_utils import create_chrome_driver, quit_driver
from .models import ImdbChartEntry


IMDB_TOP_10_WEEK_CHART_KEY = "imdb_top_10_week"
IMDB_ID_RE = re.compile(r"^tt\d+$")
IMDB_TITLE_RE = re.compile(r"/title/(tt\d+)/")
TITLE_POSITION_RE = re.compile(r"^\s*(\d+)\.\s*(.+?)\s*$")
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
KMAX_INTERNAL_URL = config("KMAX_INTERNAL_URL", default="")
KMAX_INTERNAL_TOKEN = config("KMAX_INTERNAL_TOKEN", default="")


class ImdbChartError(RuntimeError):
    pass


class ImdbChartDataError(ImdbChartError):
    pass


@dataclass(frozen=True)
class ImdbChartItem:
    imdb_id: str
    position: int
    title: str = ""
    year: int | None = None
    imdb_rating: Decimal | None = None
    vote_count: int | None = None
    href: str = ""


def fetch_imdb_chart_html(url: str, *, timeout: int) -> str:
    session = _build_session()
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        response_body = getattr(exc.response, "text", "")[:500]
        suffix = f": {response_body}" if response_body else ""
        raise ImdbChartError(f"IMDb chart request failed{suffix}") from exc
    if not response.text.strip():
        raise ImdbChartDataError(
            f"IMDb chart returned empty response: status={response.status_code}"
        )
    if _is_imdb_waf_challenge(response.text):
        if not settings.IMDB_BROWSER_FALLBACK_ENABLED:
            raise ImdbChartDataError(
                "IMDb returned a challenge page instead of chart HTML"
            )
        return fetch_imdb_chart_html_with_browser(
            url,
            timeout=timeout,
            wait_seconds=settings.IMDB_BROWSER_WAIT_SECONDS,
        )
    return response.text


def fetch_imdb_chart_html_with_browser(
    url: str,
    *,
    timeout: int,
    wait_seconds: int,
) -> str:
    driver = None
    try:
        driver = create_chrome_driver(
            stealth=False,
            page_load_timeout=timeout,
            script_timeout=timeout,
        )
        driver.get(url)
        WebDriverWait(
            driver,
            wait_seconds,
            poll_frequency=2,
        ).until(lambda browser: IMDB_TITLE_RE.search(browser.page_source or ""))
        html = driver.page_source or ""
    except TimeoutException as exc:
        html = driver.page_source if driver else ""
        raise ImdbChartDataError(
            "IMDb browser fetch did not reach chart HTML: "
            f"html_len={len(html)} "
            f"waf_challenge={_is_imdb_waf_challenge(html)}"
        ) from exc
    except WebDriverException as exc:
        raise ImdbChartError(f"IMDb browser fetch failed: {exc}") from exc
    finally:
        quit_driver(driver)

    if not html.strip():
        raise ImdbChartDataError("IMDb browser returned empty page source")
    return html


def parse_imdb_chart_items(html: str, *, limit: int = 10) -> list[ImdbChartItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[ImdbChartItem] = []
    seen: set[str] = set()

    for node in soup.select("li.ipc-metadata-list-summary-item"):
        item = _parse_summary_item(node, default_position=len(items) + 1)
        if item is None or item.imdb_id in seen:
            continue
        seen.add(item.imdb_id)
        items.append(item)
        if len(items) >= limit:
            break

    if len(items) < limit:
        for imdb_id in IMDB_TITLE_RE.findall(html):
            if imdb_id in seen:
                continue
            seen.add(imdb_id)
            items.append(ImdbChartItem(imdb_id=imdb_id, position=len(items) + 1))
            if len(items) >= limit:
                break

    if not items:
        lowered = html.lower()
        if "awswafcookiedomainlist" in html or "challenge" in lowered:
            raise ImdbChartDataError(
                "IMDb returned a challenge page instead of chart HTML"
            )
        raise ImdbChartDataError("IMDb chart page has no title ids")
    return items


def sync_imdb_top_10_week_chart(
    *,
    dry_run: bool = False,
    html: str | None = None,
    imdb_ids: list[str] | tuple[str, ...] | None = None,
    push_to_kmax: bool = True,
) -> dict[str, Any]:
    url = settings.IMDB_TOP_10_WEEK_URL
    source = "manual_ids" if imdb_ids is not None else "imdb"
    warning = ""
    if imdb_ids is not None:
        items = items_from_imdb_ids(imdb_ids)
    else:
        try:
            if html is None:
                html = fetch_imdb_chart_html(
                    url,
                    timeout=settings.IMDB_REQUEST_TIMEOUT_SECONDS,
                )
            items = parse_imdb_chart_items(html, limit=10)
        except ImdbChartError as exc:
            items, source = _fallback_items()
            if not items:
                raise
            warning = str(exc)

    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "chart_key": IMDB_TOP_10_WEEK_CHART_KEY,
            "source": source,
            "warning": warning,
            "items": [_raw_item_data(item) for item in items],
        }

    saved = upsert_imdb_chart_items(
        chart_key=IMDB_TOP_10_WEEK_CHART_KEY,
        source_url=url,
        items=items,
    )
    result = {
        "status": "success",
        "dry_run": False,
        "chart_key": IMDB_TOP_10_WEEK_CHART_KEY,
        "source": source,
        "warning": warning,
        "received": len(items),
        "saved": saved,
        "imdb_ids": [item.imdb_id for item in items],
    }
    if push_to_kmax:
        result["kmax"] = refresh_kmax_imdb_top_10_cache(items)
    return result


def refresh_kmax_imdb_top_10_cache(items: list[ImdbChartItem]) -> dict[str, Any] | str:
    if not KMAX_INTERNAL_URL or not KMAX_INTERNAL_TOKEN:
        return "skipped"
    if not items:
        return "skipped_empty"

    url = (
        f"{KMAX_INTERNAL_URL.rstrip('/')}"
        "/ru/api/v1/home/internal/imdb/top-10-week/refresh/"
    )
    payload = {
        "imdb_ids": [item.imdb_id for item in items],
        "items": [_raw_item_data(item) for item in items],
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {KMAX_INTERNAL_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        response_body = getattr(exc.response, "text", "")[:500]
        suffix = f": {response_body}" if response_body else ""
        raise ImdbChartError(f"Kmax IMDb cache refresh failed{suffix}") from exc


def upsert_imdb_chart_items(
    *,
    chart_key: str,
    source_url: str,
    items: list[ImdbChartItem],
) -> int:
    if not items:
        return 0

    _ensure_imdb_chart_table()
    now = timezone.now()
    imdb_ids = [item.imdb_id for item in items]

    with transaction.atomic(using="main_db"):
        ImdbChartEntry.objects.filter(
            chart_key=chart_key,
            is_active=True,
        ).exclude(imdb_id__in=imdb_ids).update(
            is_active=False,
            updated_at=now,
        )

        saved = 0
        for item in items:
            entry, created = ImdbChartEntry.objects.get_or_create(
                chart_key=chart_key,
                imdb_id=item.imdb_id,
                defaults={
                    "position": item.position,
                    "title": item.title,
                    "year": item.year,
                    "imdb_rating": item.imdb_rating,
                    "vote_count": item.vote_count,
                    "source_url": source_url,
                    "raw_data": _raw_item_data(item),
                    "is_active": True,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "fetched_at": now,
                },
            )
            if not created:
                entry.position = item.position
                entry.title = item.title
                entry.year = item.year
                entry.imdb_rating = item.imdb_rating
                entry.vote_count = item.vote_count
                entry.source_url = source_url
                entry.raw_data = _raw_item_data(item)
                entry.is_active = True
                entry.last_seen_at = now
                entry.fetched_at = now
                entry.updated_at = now
                entry.save(
                    update_fields=[
                        "position",
                        "title",
                        "year",
                        "imdb_rating",
                        "vote_count",
                        "source_url",
                        "raw_data",
                        "is_active",
                        "last_seen_at",
                        "fetched_at",
                        "updated_at",
                    ]
                )
            saved += 1
    return saved


def items_from_imdb_ids(values: list[str] | tuple[str, ...]) -> list[ImdbChartItem]:
    items = []
    for imdb_id in normalize_imdb_ids(values):
        items.append(ImdbChartItem(imdb_id=imdb_id, position=len(items) + 1))
        if len(items) >= 10:
            break
    if not items:
        raise ImdbChartDataError("IMDb fallback ids are empty")
    return items


def normalize_imdb_ids(values) -> list[str]:
    imdb_ids = []
    seen = set()
    for value in values or []:
        imdb_id = str(value or "").strip()
        if not IMDB_ID_RE.match(imdb_id) or imdb_id in seen:
            continue
        seen.add(imdb_id)
        imdb_ids.append(imdb_id)
    return imdb_ids


def active_imdb_chart_items(
    *,
    chart_key: str = IMDB_TOP_10_WEEK_CHART_KEY,
) -> list[ImdbChartItem]:
    _ensure_imdb_chart_table()
    entries = (
        ImdbChartEntry.objects.filter(chart_key=chart_key, is_active=True)
        .order_by("position", "id")[:10]
    )
    return [
        ImdbChartItem(
            imdb_id=entry.imdb_id,
            position=entry.position,
            title=entry.title,
            year=entry.year,
            imdb_rating=entry.imdb_rating,
            vote_count=entry.vote_count,
            href=(
                entry.raw_data.get("href", "")
                if isinstance(entry.raw_data, dict)
                else ""
            ),
        )
        for entry in entries
    ]


def _fallback_items() -> tuple[list[ImdbChartItem], str]:
    fallback_ids = normalize_imdb_ids(
        getattr(settings, "IMDB_TOP_10_WEEK_FALLBACK_IDS", ())
    )
    if fallback_ids:
        return items_from_imdb_ids(fallback_ids), "fallback_ids"

    cached_items = active_imdb_chart_items()
    if cached_items:
        return cached_items, "cached"
    return [], ""


def _parse_summary_item(node, *, default_position: int) -> ImdbChartItem | None:
    href = ""
    imdb_id = ""
    for link in node.select('a[href*="/title/tt"]'):
        href = link.get("href", "")
        match = IMDB_TITLE_RE.search(href)
        if match:
            imdb_id = match.group(1)
            break
    if not imdb_id:
        return None

    title_text = _text(node.select_one(".ipc-title__text"))
    position = default_position
    title = title_text
    if title_text:
        title_match = TITLE_POSITION_RE.match(title_text)
        if title_match:
            position = int(title_match.group(1))
            title = title_match.group(2)

    metadata_text = " ".join(
        _text(item) for item in node.select(".dli-title-metadata li")
    )
    year = _parse_year(metadata_text)
    rating = _decimal(_text(node.select_one(".ipc-rating-star--rating")))
    votes = _parse_vote_count(_text(node.select_one(".ipc-rating-star--voteCount")))

    return ImdbChartItem(
        imdb_id=imdb_id,
        position=position,
        title=title,
        year=year,
        imdb_rating=rating,
        vote_count=votes,
        href=href,
    )


def _ensure_imdb_chart_table():
    with connections["main_db"].cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS content_app_imdb_chart_entry (
                id BIGSERIAL PRIMARY KEY,
                chart_key varchar(64) NOT NULL,
                imdb_id varchar(16) NOT NULL,
                position smallint NOT NULL CHECK (position >= 0),
                title varchar(255) NOT NULL DEFAULT '',
                year integer NULL,
                imdb_rating numeric(4, 1) NULL,
                vote_count integer NULL CHECK (vote_count >= 0),
                source_url text NOT NULL DEFAULT '',
                raw_data jsonb NOT NULL DEFAULT '{}'::jsonb,
                is_active boolean NOT NULL DEFAULT true,
                first_seen_at timestamptz NOT NULL,
                last_seen_at timestamptz NOT NULL,
                fetched_at timestamptz NOT NULL,
                updated_at timestamptz NOT NULL,
                CONSTRAINT uniq_imdb_chart_entry_chart_imdb
                    UNIQUE (chart_key, imdb_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS imdb_chart_active_position_idx
            ON content_app_imdb_chart_entry
                (chart_key, is_active, position)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS imdb_chart_fetched_at_idx
            ON content_app_imdb_chart_entry (chart_key, fetched_at)
            """
        )


def _raw_item_data(item: ImdbChartItem) -> dict[str, Any]:
    data = asdict(item)
    if item.imdb_rating is not None:
        data["imdb_rating"] = str(item.imdb_rating)
    return data


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(429, 500, 502, 503, 504),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _is_imdb_waf_challenge(html: str) -> bool:
    lowered = (html or "").lower()
    return (
        "awswafcookiedomainlist" in lowered
        or "token.awswaf.com" in lowered
        or "challenge-container" in lowered
    )


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _parse_year(value: str) -> int | None:
    match = YEAR_RE.search(value or "")
    return int(match.group(1)) if match else None


def _decimal(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _parse_vote_count(value: str) -> int | None:
    if not value:
        return None
    cleaned = value.replace("\xa0", " ").strip(" ()")
    if not cleaned:
        return None
    multiplier = 1
    suffix = cleaned[-1:].lower()
    if suffix == "k":
        multiplier = 1_000
        cleaned = cleaned[:-1]
    elif suffix == "m":
        multiplier = 1_000_000
        cleaned = cleaned[:-1]
    cleaned = cleaned.replace(",", ".").replace(" ", "")
    try:
        return int(Decimal(cleaned) * multiplier)
    except InvalidOperation:
        return None
