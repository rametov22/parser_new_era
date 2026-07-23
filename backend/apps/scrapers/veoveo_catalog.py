import re
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Any

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class VeoVeoCatalogError(RuntimeError):
    pass


class VeoVeoCatalogDataError(VeoVeoCatalogError):
    pass


@dataclass(frozen=True)
class VeoVeoCatalogPage:
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    pages: int
    has_next_page: bool


class VeoVeoCatalogClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: int = 60,
        session: requests.Session | None = None,
    ):
        if not token:
            raise ValueError("VeoVeo API token is empty")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or self._build_session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=1.0,
            allowed_methods=frozenset({"POST"}),
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def get_details_page(
        self,
        *,
        page: int,
        page_size: int,
        from_updated_at: datetime | None = None,
        to_updated_at: datetime | None = None,
    ) -> VeoVeoCatalogPage:
        if (from_updated_at is None) != (to_updated_at is None):
            raise ValueError(
                "from_updated_at and to_updated_at must be provided together"
            )

        payload = {
            "pagination": {
                "page": page,
                "pageSize": page_size,
                "type": "page",
                "order": "ASC",
                "sortBy": "id",
            },
        }
        if from_updated_at is not None and to_updated_at is not None:
            payload.update(
                {
                    "fromUpdatedAt": _api_datetime(from_updated_at),
                    "toUpdatedAt": _api_datetime(to_updated_at),
                }
            )

        try:
            response = self.session.post(
                f"{self.base_url}/v1/contents/details",
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            response_body = getattr(exc.response, "text", "")[:500]
            suffix = f": {response_body}" if response_body else ""
            raise VeoVeoCatalogError(
                f"VeoVeo API request failed{suffix}"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise VeoVeoCatalogDataError(
                "VeoVeo API returned invalid JSON"
            ) from exc

        if not isinstance(body, dict):
            raise VeoVeoCatalogDataError("VeoVeo response must be an object")

        items = body.get("data")
        meta = body.get("meta")
        if not isinstance(items, list) or not isinstance(meta, dict):
            raise VeoVeoCatalogDataError(
                "VeoVeo response must contain data[] and meta{}"
            )

        api_page = _required_non_negative_int(meta.get("page"), "meta.page")
        total = _required_non_negative_int(meta.get("total"), "meta.total")
        pages = _required_non_negative_int(meta.get("pages"), "meta.pages")
        api_page_size = _required_non_negative_int(
            meta.get("pageSize", page_size),
            "meta.pageSize",
        )
        has_next_page = meta.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise VeoVeoCatalogDataError("meta.hasNextPage must be boolean")
        if api_page != page:
            raise VeoVeoCatalogDataError(
                f"Requested page {page}, but API returned page {api_page}"
            )
        if has_next_page and not items:
            raise VeoVeoCatalogDataError(
                f"Page {page} is empty but hasNextPage is true"
            )
        if total and page == 1 and not items:
            raise VeoVeoCatalogDataError(
                "VeoVeo returned an empty first page for a non-empty catalog"
            )

        return VeoVeoCatalogPage(
            items=items,
            page=api_page,
            page_size=api_page_size,
            total=total,
            pages=pages,
            has_next_page=has_next_page,
        )


def normalize_veoveo_content(
    payload: dict[str, Any],
    *,
    seen_at: datetime,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise VeoVeoCatalogDataError("Content item must be an object")

    veoveo_id = _positive_int(payload.get("id"))
    if veoveo_id is None:
        raise VeoVeoCatalogDataError("Content item has no valid positive id")

    episodes_by_season = payload.get("episodesBySeason")
    if not isinstance(episodes_by_season, dict):
        episodes_by_season = {}

    episodes_by_voice_authors = payload.get("episodesByVoiceAuthors")
    if not isinstance(episodes_by_voice_authors, list):
        episodes_by_voice_authors = []

    seasons_count = _non_negative_int(payload.get("seasonsCount"))
    episodes_count = _non_negative_int(payload.get("episodesCount"))
    last_season, last_episode = derive_last_season_episode(
        episodes_by_season=episodes_by_season,
        episodes_by_voice_authors=episodes_by_voice_authors,
        seasons_count=seasons_count,
        episodes_count=episodes_count,
    )

    content_type = payload.get("contentType")
    if isinstance(content_type, dict):
        content_type = content_type.get("slug") or content_type.get("name")

    voice_authors = payload.get("voiceAuthorsV2")
    if not isinstance(voice_authors, list):
        voice_authors = []

    languages = payload.get("languages")
    if not isinstance(languages, list):
        languages = []

    return {
        "veoveo_id": veoveo_id,
        "kinopoisk_id": _positive_int(payload.get("kinopoiskId")),
        "imdb_id": _text(payload.get("imdbId"), 32),
        "title": _text(payload.get("title"), 255),
        "original_title": _text(payload.get("originalTitle"), 255),
        "year": _integer(payload.get("year")),
        "content_type": _text(content_type, 32),
        "is_available": True,
        "player_url": _text(payload.get("playerUrl")),
        "video_quality": _text(payload.get("videoQuality"), 32),
        "duration": _non_negative_int(payload.get("duration")),
        "age_restriction": _age_restriction(payload.get("ageRestriction")),
        "audio_tracks_raw": _text(payload.get("audioTracks")),
        "voice_authors": voice_authors,
        "languages": languages,
        "seasons_count": seasons_count,
        "episodes_count": episodes_count,
        "episodes_by_season": episodes_by_season,
        "episodes_by_voice_authors": episodes_by_voice_authors,
        "last_season": last_season,
        "last_episode": last_episode,
        "provider_created_at": _provider_datetime(payload.get("createdAt")),
        "provider_updated_at": _provider_datetime(payload.get("updatedAt")),
        "premiere_at": _provider_datetime(payload.get("premiereAt")),
        "last_season_premiere_at": _provider_datetime(
            payload.get("lastSeasonPremiereAt")
        ),
        "exclusive_start_at": _provider_datetime(
            payload.get("exclusiveStartAt")
        ),
        "exclusive_end_at": _provider_datetime(payload.get("exclusiveEndAt")),
        "is_lgbt": (
            payload.get("isLgbt")
            if isinstance(payload.get("isLgbt"), bool)
            else None
        ),
        "last_seen_at": seen_at,
        "synced_at": seen_at,
    }


def derive_last_season_episode(
    *,
    episodes_by_season: dict[str, Any],
    episodes_by_voice_authors: list[Any],
    seasons_count: int | None,
    episodes_count: int | None,
) -> tuple[int | None, int | None]:
    episodes_for_season: dict[int, set[int]] = {}

    for voice_author in episodes_by_voice_authors:
        if not isinstance(voice_author, dict):
            continue
        seasons = voice_author.get("seasons")
        if not isinstance(seasons, list):
            continue
        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_number = _positive_int(season.get("seasonOrdering"))
            if season_number is None:
                continue
            episodes = season.get("episodes")
            if not isinstance(episodes, list):
                continue
            target = episodes_for_season.setdefault(season_number, set())
            target.update(
                episode_number
                for value in episodes
                if (episode_number := _positive_int(value)) is not None
            )

    counts_by_season: dict[int, int] = {}
    for raw_season, raw_count in episodes_by_season.items():
        season_number = _positive_int(raw_season)
        episode_count = _non_negative_int(raw_count)
        if season_number is not None and episode_count is not None:
            counts_by_season[season_number] = episode_count

    known_seasons = set(episodes_for_season) | set(counts_by_season)
    if known_seasons:
        last_season = max(known_seasons)
    else:
        last_season = _positive_int(seasons_count)

    if last_season is None:
        return None, None

    explicit_episodes = episodes_for_season.get(last_season) or set()
    if explicit_episodes:
        return last_season, max(explicit_episodes)

    if last_season in counts_by_season:
        return last_season, counts_by_season[last_season]

    if last_season == 1:
        return last_season, _non_negative_int(episodes_count)

    return last_season, None


def _api_datetime(value: datetime) -> str:
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return (
        value.astimezone(datetime_timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _provider_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    parsed = parse_datetime(cleaned)
    if parsed is None:
        for format_string in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(cleaned, format_string)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _text(value: Any, max_length: int | None = None) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:max_length] if max_length else text


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _non_negative_int(value: Any) -> int | None:
    number = _integer(value)
    return number if number is not None and number >= 0 else None


def _age_restriction(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _positive_int(value: Any) -> int | None:
    number = _integer(value)
    return number if number is not None and number > 0 else None


def _required_non_negative_int(value: Any, field_name: str) -> int:
    number = _non_negative_int(value)
    if number is None:
        raise VeoVeoCatalogDataError(
            f"{field_name} must be a non-negative integer"
        )
    return number
