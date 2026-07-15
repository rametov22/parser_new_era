"""Shared proxy rotation for both Vavada Celery queues."""

import hashlib
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

import redis
from django.conf import settings


logger = logging.getLogger("vavada_proxy")


class ProxyConfigurationError(RuntimeError):
    """The proxy pool is enabled but configured incorrectly."""


class ProxyPoolExhausted(RuntimeError):
    """No proxy became available before the configured timeout."""


@dataclass(frozen=True)
class ProxyEndpoint:
    url: str
    label: str
    endpoint_id: str


class ProxyLease:
    def __init__(
        self,
        endpoint: ProxyEndpoint,
        redis_client: redis.Redis,
        lock,
        cooldown_key: str,
        cooldown_seconds: int,
        failure_cooldown_seconds: int,
        owner: str,
    ):
        self.endpoint = endpoint
        self._redis = redis_client
        self._lock = lock
        self._cooldown_key = cooldown_key
        self._cooldown_seconds = cooldown_seconds
        self._failure_cooldown_seconds = failure_cooldown_seconds
        self._owner = owner
        self._released = False

    @property
    def url(self) -> str:
        return self.endpoint.url

    @property
    def label(self) -> str:
        return self.endpoint.label

    def release(self, failed: bool = False) -> None:
        if self._released:
            return
        self._released = True

        try:
            cooldown_seconds = (
                self._failure_cooldown_seconds if failed else self._cooldown_seconds
            )
            if cooldown_seconds > 0:
                self._redis.set(
                    self._cooldown_key,
                    "1",
                    ex=cooldown_seconds,
                )
        except Exception as exc:
            logger.warning(
                "[vavada-proxy] cooldown %s failed: %s",
                self.endpoint.label,
                exc,
            )
        finally:
            try:
                self._lock.release()
            except Exception as exc:
                # The lease has a TTL, so a worker killed by a time limit cannot
                # reserve an address forever.
                logger.warning(
                    "[vavada-proxy] release %s failed: %s",
                    self.endpoint.label,
                    exc,
                )

        logger.info(
            "[vavada-proxy] released %s from %s",
            self.endpoint.label,
            self._owner,
        )


def _redis_client() -> redis.Redis:
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
    )


def _split_proxy_values(raw: str) -> list[str]:
    return [
        value.strip()
        for value in raw.replace(",", "\n").splitlines()
        if value.strip()
    ]


def _proxy_values() -> list[str]:
    values = _split_proxy_values(getattr(settings, "VAVADA_PROXY_URLS", ""))
    proxy_file = str(getattr(settings, "VAVADA_PROXY_FILE", "") or "").strip()
    if not proxy_file:
        return values

    path = Path(proxy_file)
    if not path.is_absolute():
        configured_path = Path(settings.BASE_DIR) / path
        app_path = Path(__file__).resolve().parents[2] / path
        path = configured_path if configured_path.exists() else app_path
    if not path.exists():
        raise ProxyConfigurationError(
            f"Proxy file does not exist: {path}. "
            "Rebuild the backend image to copy the private proxy file."
        )

    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if (
            value
            and not value.startswith("#")
            and not value.lower().startswith("port:")
        ):
            values.append(value)
    return values


def _proxy_url(value: str) -> str:
    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.hostname or not parsed.port:
            raise ProxyConfigurationError("Proxy URL must contain a host and port")
        return value

    default_port = str(getattr(settings, "VAVADA_PROXY_PORT", "") or "").strip()
    host = value
    port = default_port

    if value.count(":") == 1:
        possible_host, possible_port = value.rsplit(":", 1)
        if possible_port.isdigit():
            host, port = possible_host, possible_port

    if not host or not port.isdigit():
        raise ProxyConfigurationError(
            "VAVADA_PROXY_PORT is required when the proxy file contains only IP addresses"
        )

    scheme = str(getattr(settings, "VAVADA_PROXY_SCHEME", "http") or "http")
    username = str(getattr(settings, "VAVADA_PROXY_USERNAME", "") or "")
    password = str(getattr(settings, "VAVADA_PROXY_PASSWORD", "") or "")
    credentials = ""
    if username or password:
        if not username or not password:
            raise ProxyConfigurationError(
                "Both VAVADA_PROXY_USERNAME and VAVADA_PROXY_PASSWORD are required"
            )
        credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"

    return f"{scheme}://{credentials}{host}:{port}"


def load_vavada_proxy_pool() -> tuple[ProxyEndpoint, ...]:
    if not getattr(settings, "VAVADA_PROXY_ENABLED", False):
        return ()

    endpoints = []
    seen = set()
    for value in _proxy_values():
        url = _proxy_url(value)
        parsed = urlsplit(url)
        if not parsed.hostname or not parsed.port:
            raise ProxyConfigurationError("Proxy URL must contain a host and port")

        identity = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        endpoint_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        if endpoint_id in seen:
            continue
        seen.add(endpoint_id)
        endpoints.append(
            ProxyEndpoint(
                url=url,
                label=f"{parsed.hostname}:{parsed.port}",
                endpoint_id=endpoint_id,
            )
        )

    if not endpoints:
        raise ProxyConfigurationError(
            "VAVADA_PROXY_ENABLED is true, but the proxy pool is empty"
        )
    return tuple(endpoints)


def _pool_namespace(pool: tuple[ProxyEndpoint, ...]) -> str:
    payload = "\n".join(sorted(endpoint.endpoint_id for endpoint in pool))
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]
    return f"vavada:proxy:{digest}"


def _reset_completed_cycle(
    client: redis.Redis,
    counts_key: str,
    pool_size: int,
    requests_per_ip: int,
) -> None:
    script = """
    local values = redis.call('HVALS', KEYS[1])
    if #values < tonumber(ARGV[1]) then
        return 0
    end
    for _, value in ipairs(values) do
        if tonumber(value) < tonumber(ARGV[2]) then
            return 0
        end
    end
    redis.call('DEL', KEYS[1])
    return 1
    """
    client.eval(script, 1, counts_key, pool_size, requests_per_ip)


def acquire_vavada_proxy(owner: str) -> Optional[ProxyLease]:
    """Lease one proxy without sharing its IP with another active task."""
    pool = load_vavada_proxy_pool()
    if not pool:
        return None

    client = _redis_client()
    namespace = _pool_namespace(pool)
    counts_key = f"{namespace}:cycle-counts"
    requests_per_ip = max(
        int(getattr(settings, "VAVADA_PROXY_REQUESTS_PER_IP", 1)),
        1,
    )
    lease_ttl = max(
        int(getattr(settings, "VAVADA_PROXY_LEASE_TTL_SECONDS", 300)),
        30,
    )
    min_interval = max(
        int(getattr(settings, "VAVADA_PROXY_MIN_INTERVAL_SECONDS", 2)),
        0,
    )
    cycle_cooldown = max(
        int(getattr(settings, "VAVADA_PROXY_CYCLE_COOLDOWN_SECONDS", 10)),
        min_interval,
    )
    failure_cooldown = max(
        int(getattr(settings, "VAVADA_PROXY_FAILURE_COOLDOWN_SECONDS", 300)),
        cycle_cooldown,
    )
    wait_timeout = max(
        int(getattr(settings, "VAVADA_PROXY_WAIT_TIMEOUT_SECONDS", 30)),
        1,
    )
    deadline = time.monotonic() + wait_timeout
    randomizer = random.SystemRandom()

    while time.monotonic() < deadline:
        _reset_completed_cycle(client, counts_key, len(pool), requests_per_ip)
        counts = {
            endpoint_id: int(count)
            for endpoint_id, count in client.hgetall(counts_key).items()
        }
        candidates = [
            endpoint
            for endpoint in pool
            if counts.get(endpoint.endpoint_id, 0) < requests_per_ip
        ]
        randomizer.shuffle(candidates)

        for endpoint in candidates:
            lock_key = f"{namespace}:lease:{endpoint.endpoint_id}"
            cooldown_key = f"{namespace}:cooldown:{endpoint.endpoint_id}"
            if client.exists(cooldown_key):
                continue

            lock = client.lock(lock_key, timeout=lease_ttl, blocking_timeout=0)
            if not lock.acquire(blocking=False):
                continue

            try:
                # Recheck after taking the lock because another process may have
                # finished the previous request between exists() and acquire().
                if client.exists(cooldown_key):
                    lock.release()
                    continue

                count = int(client.hincrby(counts_key, endpoint.endpoint_id, 1))
                client.expire(counts_key, 7 * 24 * 60 * 60)
                cooldown_seconds = (
                    cycle_cooldown if count >= requests_per_ip else min_interval
                )
                logger.info(
                    "[vavada-proxy] %s leased %s (%s/%s in cycle)",
                    owner,
                    endpoint.label,
                    count,
                    requests_per_ip,
                )
                return ProxyLease(
                    endpoint=endpoint,
                    redis_client=client,
                    lock=lock,
                    cooldown_key=cooldown_key,
                    cooldown_seconds=cooldown_seconds,
                    failure_cooldown_seconds=failure_cooldown,
                    owner=owner,
                )
            except Exception:
                try:
                    lock.release()
                except Exception:
                    pass
                raise

        time.sleep(randomizer.uniform(0.2, 0.6))

    raise ProxyPoolExhausted(
        f"No Vavada proxy was available for {owner!r} during {wait_timeout}s"
    )
