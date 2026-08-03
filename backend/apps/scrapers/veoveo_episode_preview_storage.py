from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from threading import Event, Lock
from typing import Any
from urllib.parse import urlsplit

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STORAGE_PREFIX = "veoveo/episode-previews/"
STORAGE_FIELDS = (
    "preview_storage_key",
    "preview_storage_bytes",
    "preview_storage_format",
)
FORMAT_EXTENSIONS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


class EpisodePreviewStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredEpisodePreview:
    key: str
    size: int
    image_format: str
    created: bool


class EpisodePreviewStorageClient:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...],
        max_bytes: int,
        timeout: int,
        storage=None,
        session: requests.Session | None = None,
        known_keys: set[str] | None = None,
        known_keys_lock: Lock | None = None,
        in_flight_keys: dict[str, Event] | None = None,
    ):
        if not allowed_hosts:
            raise ValueError("At least one preview host must be allowed")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.storage = storage or default_storage
        self.session = session or self._build_session()
        self.known_keys = known_keys if known_keys is not None else set()
        self.known_keys_lock = known_keys_lock or Lock()
        self.in_flight_keys = in_flight_keys if in_flight_keys is not None else {}

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
                "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.5",
                "User-Agent": "KMAX-VeoVeoEpisodePreviewMirror/1.0",
            }
        )
        return session

    def download_and_store(self, source_url: str) -> StoredEpisodePreview:
        self._validate_url(source_url)
        try:
            response = self.session.get(
                source_url,
                stream=True,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            suffix = f" status={status}" if status is not None else ""
            raise EpisodePreviewStorageError(
                f"Preview download failed{suffix}"
            ) from exc

        try:
            self._validate_url(response.url)
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError):
                    declared_size = 0
                if declared_size > self.max_bytes:
                    raise EpisodePreviewStorageError(
                        f"Preview exceeds {self.max_bytes} bytes"
                    )

            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    raise EpisodePreviewStorageError(
                        f"Preview exceeds {self.max_bytes} bytes"
                    )
        finally:
            response.close()

        image_format, extension, content_type = self._validate_image(bytes(data))
        digest = sha256(source_url.encode("utf-8")).hexdigest()
        key = f"{STORAGE_PREFIX}{digest[:2]}/{digest}.{extension}"

        while True:
            with self.known_keys_lock:
                if key in self.known_keys:
                    return StoredEpisodePreview(
                        key=key,
                        size=len(data),
                        image_format=image_format,
                        created=False,
                    )
                ready = self.in_flight_keys.get(key)
                if ready is None:
                    ready = Event()
                    self.in_flight_keys[key] = ready
                    break
            ready.wait()

        content = ContentFile(bytes(data), name=key.rsplit("/", 1)[-1])
        content.content_type = content_type
        try:
            saved_key = self.storage.save(key, content)
        except Exception as exc:
            with self.known_keys_lock:
                self.in_flight_keys.pop(key, None)
                ready.set()
            raise EpisodePreviewStorageError("Preview S3 upload failed") from exc

        with self.known_keys_lock:
            self.known_keys.add(key)
            self.known_keys.add(saved_key)
            self.in_flight_keys.pop(key, None)
            ready.set()
        return StoredEpisodePreview(
            key=saved_key,
            size=len(data),
            image_format=image_format,
            created=True,
        )

    def _validate_url(self, url: str) -> None:
        if not isinstance(url, str) or not url.strip():
            raise EpisodePreviewStorageError("Preview URL is empty")
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise EpisodePreviewStorageError("Preview URL host is not allowed")

    @staticmethod
    def _validate_image(data: bytes) -> tuple[str, str, str]:
        if not data:
            raise EpisodePreviewStorageError("Preview response is empty")
        try:
            with Image.open(BytesIO(data)) as image:
                image_format = (image.format or "").upper()
                image.verify()
        except (OSError, UnidentifiedImageError) as exc:
            raise EpisodePreviewStorageError(
                "Preview response is not a valid image"
            ) from exc
        try:
            extension, content_type = FORMAT_EXTENSIONS[image_format]
        except KeyError as exc:
            raise EpisodePreviewStorageError(
                f"Preview image format is not supported: {image_format or 'unknown'}"
            ) from exc
        return image_format, extension, content_type


def merge_episode_preview_storage(
    existing: Any,
    refreshed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_items = (
        [item for item in existing if isinstance(item, dict)]
        if isinstance(existing, list)
        else []
    )
    by_episode_id = {
        item.get("episode_id"): item
        for item in existing_items
        if item.get("episode_id")
    }
    by_order = {
        (item.get("season"), item.get("episode")): item for item in existing_items
    }

    merged = []
    for item in refreshed:
        new_item = dict(item)
        current = by_episode_id.get(item.get("episode_id")) or by_order.get(
            (item.get("season"), item.get("episode"))
        )
        if current and current.get("preview_url") == item.get("preview_url"):
            for field in STORAGE_FIELDS:
                if current.get(field) is not None:
                    new_item[field] = current[field]
        merged.append(new_item)
    return merged


def has_pending_episode_preview_downloads(previews: Any) -> bool:
    if not isinstance(previews, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("preview_url")
        and not item.get("preview_storage_key")
        for item in previews
    )


def list_episode_preview_storage_keys(storage=None) -> set[str]:
    selected_storage = storage or default_storage
    bucket = getattr(selected_storage, "bucket", None)
    if bucket is not None and hasattr(bucket, "objects"):
        return {obj.key for obj in bucket.objects.filter(Prefix=STORAGE_PREFIX)}
    return _list_storage_keys_recursively(selected_storage, STORAGE_PREFIX.rstrip("/"))


def _list_storage_keys_recursively(storage, path: str) -> set[str]:
    try:
        directories, files = storage.listdir(path)
    except FileNotFoundError:
        return set()
    keys = {f"{path}/{filename}" for filename in files}
    for directory in directories:
        keys.update(_list_storage_keys_recursively(storage, f"{path}/{directory}"))
    return keys
