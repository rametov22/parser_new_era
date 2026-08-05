from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TMDbEpisodePreviewError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TMDbEpisodePreviewClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        read_access_token: str = "",
        api_base_url: str = "https://api.themoviedb.org/3",
        image_base_url: str = "https://image.tmdb.org/t/p/original",
        language: str = "ru-RU",
        timeout: int = 30,
        session: requests.Session | None = None,
    ):
        self.api_key = (api_key or "").strip()
        self.read_access_token = (read_access_token or "").strip()
        self.api_base_url = api_base_url.rstrip("/") + "/"
        self.image_base_url = image_base_url.rstrip("/") + "/"
        self.language = language
        self.timeout = timeout
        self.session = session or self._build_session()
        self._tv_id_cache: dict[str, int | None] = {}
        self._still_cache: dict[tuple[int, int, int], str | None] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key or self.read_access_token)

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.8,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "KMAX-TMDbEpisodePreviewFallback/1.0",
            }
        )
        return session

    def fill_missing_episode_previews(
        self,
        *,
        imdb_id: str | None,
        previews: list[dict[str, Any]],
        max_missing: int,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self.enabled or not imdb_id:
            return previews, 0
        tv_id = self.find_tv_id(imdb_id)
        if not tv_id:
            return previews, 0

        filled = 0
        out = []
        for original in previews:
            if not isinstance(original, dict):
                continue
            item = dict(original)
            if item.get("preview_url"):
                out.append(item)
                continue
            if filled >= max_missing:
                out.append(item)
                continue
            season = _positive_int(item.get("season"))
            episode = _positive_int(item.get("episode"))
            if season is None or episode is None:
                out.append(item)
                continue
            still_url = self.get_episode_still_url(
                tv_id=tv_id,
                season=season,
                episode=episode,
            )
            if still_url:
                item["preview_url"] = still_url
                item["preview_source"] = "tmdb"
                item["tmdb_tv_id"] = tv_id
                filled += 1
            out.append(item)
        return out, filled

    def find_tv_id(self, imdb_id: str) -> int | None:
        imdb_id = (imdb_id or "").strip()
        if not imdb_id:
            return None
        if imdb_id in self._tv_id_cache:
            return self._tv_id_cache[imdb_id]

        payload = self._get(
            f"find/{imdb_id}",
            external_source="imdb_id",
            language=self.language,
        )
        tv_id = None
        for item in payload.get("tv_results") or []:
            if not isinstance(item, dict):
                continue
            tv_id = _positive_int(item.get("id"))
            if tv_id:
                break
        self._tv_id_cache[imdb_id] = tv_id
        return tv_id

    def get_episode_still_url(
        self,
        *,
        tv_id: int,
        season: int,
        episode: int,
    ) -> str | None:
        key = (tv_id, season, episode)
        if key in self._still_cache:
            return self._still_cache[key]

        try:
            payload = self._get(
                f"tv/{tv_id}/season/{season}/episode/{episode}/images"
            )
        except TMDbEpisodePreviewError as exc:
            if exc.status_code == 404:
                self._still_cache[key] = None
                return None
            raise
        stills = [item for item in payload.get("stills") or [] if isinstance(item, dict)]
        if not stills:
            self._still_cache[key] = None
            return None
        selected = max(
            stills,
            key=lambda item: (
                float(item.get("vote_average") or 0),
                int(item.get("vote_count") or 0),
            ),
        )
        file_path = selected.get("file_path")
        still_url = self._image_url(file_path) if isinstance(file_path, str) else None
        self._still_cache[key] = still_url
        return still_url

    def _get(self, path: str, **params) -> dict[str, Any]:
        request_params = {key: value for key, value in params.items() if value}
        headers = {}
        if self.read_access_token:
            headers["Authorization"] = f"Bearer {self.read_access_token}"
        elif self.api_key:
            request_params["api_key"] = self.api_key
        else:
            raise TMDbEpisodePreviewError("TMDb credentials are not configured")

        try:
            response = self.session.get(
                urljoin(self.api_base_url, path),
                params=request_params,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            raise TMDbEpisodePreviewError(
                f"TMDb request failed{suffix}",
                status_code=status,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TMDbEpisodePreviewError("TMDb returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TMDbEpisodePreviewError("TMDb response must be an object")
        return payload

    def _image_url(self, file_path: str) -> str:
        return urljoin(self.image_base_url, file_path.lstrip("/"))


def add_episode_placeholders(
    previews: list[dict[str, Any]],
    episodes_by_season: Any,
    *,
    max_episodes: int,
) -> list[dict[str, Any]]:
    if not isinstance(episodes_by_season, dict) or max_episodes <= 0:
        return previews

    result = [item for item in previews if isinstance(item, dict)]
    existing = {
        (_positive_int(item.get("season")), _positive_int(item.get("episode")))
        for item in result
    }
    added = 0
    for raw_season, raw_count in sorted(
        episodes_by_season.items(),
        key=lambda pair: _positive_int(pair[0]) or 0,
    ):
        season = _positive_int(raw_season)
        count = _positive_int(raw_count)
        if season is None or count is None:
            continue
        for episode in range(1, count + 1):
            if added >= max_episodes:
                return sorted_episode_previews(result)
            key = (season, episode)
            if key in existing:
                continue
            result.append(
                {
                    "title": None,
                    "season": season,
                    "episode": episode,
                    "episode_id": None,
                    "preview_url": None,
                    "variants": [],
                }
            )
            existing.add(key)
            added += 1
    return sorted_episode_previews(result)


def sorted_episode_previews(previews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        previews,
        key=lambda item: (
            _positive_int(item.get("season")) or 0,
            _positive_int(item.get("episode")) or 0,
        ),
    )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None
