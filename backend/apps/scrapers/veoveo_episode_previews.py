from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class VeoVeoEpisodePreviewError(RuntimeError):
    pass


class VeoVeoEpisodePreviewNotFound(VeoVeoEpisodePreviewError):
    pass


class VeoVeoEpisodePreviewDataError(VeoVeoEpisodePreviewError):
    pass


class VeoVeoEpisodePreviewClient:
    def __init__(
        self,
        *,
        timeout: int = 60,
        session: requests.Session | None = None,
    ):
        self.timeout = timeout
        self.session = session or self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=1.0,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "KMAX-VeoVeoEpisodePreviewSync/1.0",
            }
        )
        return session

    def get_episode_previews(
        self,
        *,
        veoveo_id: int,
        player_url: str,
    ) -> list[dict[str, Any]]:
        endpoint, token = episode_api_credentials(player_url)
        try:
            response = self.session.get(
                endpoint,
                params={"content-id": veoveo_id},
                headers={"DLE-API-TOKEN": token},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 404:
                raise VeoVeoEpisodePreviewNotFound(
                    f"VeoVeo episodes are unavailable for id={veoveo_id}"
                ) from exc
            suffix = f" status={status}" if status is not None else ""
            raise VeoVeoEpisodePreviewError(
                f"VeoVeo episode request failed for id={veoveo_id}{suffix}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise VeoVeoEpisodePreviewDataError(
                f"VeoVeo episodes returned invalid JSON for id={veoveo_id}"
            ) from exc
        return normalize_episode_previews(payload)


def episode_api_credentials(player_url: str) -> tuple[str, str]:
    """Build the player catalog endpoint without leaking its token into a URL."""
    if not isinstance(player_url, str) or not player_url.strip():
        raise VeoVeoEpisodePreviewDataError("VeoVeo player URL is empty")

    parsed = urlsplit(player_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VeoVeoEpisodePreviewDataError("VeoVeo player URL is invalid")

    token = next(
        (
            value.strip()
            for value in parse_qs(parsed.query).get("token", [])
            if value.strip()
        ),
        "",
    )
    if not token:
        raise VeoVeoEpisodePreviewDataError("VeoVeo player URL has no token")

    player_path = parsed.path.rstrip("/")
    if not player_path.endswith("/iframe"):
        raise VeoVeoEpisodePreviewDataError(
            "VeoVeo player URL has an unsupported iframe path"
        )
    balancer_path = player_path[: -len("/iframe")]
    endpoint = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{balancer_path}/proxy/playlists/catalog-api/episodes",
            "",
            "",
        )
    )
    return endpoint, token


def normalize_episode_previews(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise VeoVeoEpisodePreviewDataError("VeoVeo episodes response must be a list")

    normalized_by_order: dict[tuple[int, int], dict[str, Any]] = {}
    saw_empty_placeholder = False
    for episode in payload:
        if not isinstance(episode, dict):
            continue
        season_data = episode.get("season")
        if not isinstance(season_data, dict):
            continue
        season = _non_negative_int(season_data.get("order"))
        episode_order = _non_negative_int(episode.get("order"))
        if season is None or episode_order is None:
            continue
        if season == 0 and episode_order == 0:
            saw_empty_placeholder = True
            continue
        if episode_order == 0:
            continue

        variants = []
        raw_variants = episode.get("episodeVariants")
        if isinstance(raw_variants, list):
            for variant in raw_variants:
                if not isinstance(variant, dict):
                    continue
                variants.append(
                    {
                        "variant_id": _positive_int(variant.get("id")),
                        "title": _text(variant.get("title")),
                        "preview_url": _text(variant.get("previewImageFilepath")),
                    }
                )

        preview_url = _text(episode.get("previewImageFilepath"))
        if not preview_url:
            preview_url = next(
                (
                    variant["preview_url"]
                    for variant in variants
                    if variant["preview_url"]
                ),
                None,
            )

        normalized = {
            "season": season,
            "episode": episode_order,
            "episode_id": _positive_int(episode.get("id")),
            "title": _text(episode.get("title")),
            "preview_url": preview_url,
            "variants": variants,
        }
        key = (season, episode_order)
        current = normalized_by_order.get(key)
        if current is None or not current["preview_url"] or preview_url:
            normalized_by_order[key] = normalized

    if payload and not normalized_by_order and not saw_empty_placeholder:
        raise VeoVeoEpisodePreviewDataError(
            "VeoVeo episodes response has no valid season/episode entries"
        )
    return [normalized_by_order[key] for key in sorted(normalized_by_order)]


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
