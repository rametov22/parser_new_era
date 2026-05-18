"""
Подбираем минимальный набор хедеров для прямого доступа к видео yangi.tv.

Логика:
  1. Сначала шлём ВСЕ хедеры из приложения — проверяем что 206 (норм).
  2. Затем по одному убираем каждый хедер и смотрим:
     - 206 + не-редирект → хедер НЕ обязателен (можно убрать).
     - 3xx / ошибка → хедер обязателен (оставляем).
  3. В конце печатаем минимальный список.

Запуск:
    python3 scripts/probe_yangi_headers.py
"""

import requests
import sys

DEFAULT_URL = "https://s18.yangi.tv/kinolar/05.2026/362/Mortal%20Kombat%202%202026%20480p%20(yangi.tv).mp4"
URL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

# Полный набор из реального запроса приложения
ALL_HEADERS = {
    "X-Auth-Key": "tN2f9bPzQ6dW8x",
    "X-Session-Id": "gH4k0mLpQ8sR2t",
    "X-Playback-Signature": "aL8n5rQyS0jK3v",
    "User-Agent": "ExoPlayer",
    "X-Client-Secret": "kL5d2pRzT8nV6m",
    "X-Security-Tag": "rP1x5zQwN7mL8c",
    "X-Integrity-Key": "sN3m8vLpQ6kF2z",
    "Icy-MetaData": "1",
    "X-Access-Code": "yC9d6vBnM3fT1j",
    "X-Custom-Sign": "oQ5w3sMnR7yL2t",
    "X-Playback-Token": "mY7h1sJvR0cE4k",
    "X-Stream-Auth": "uD7p1kLtF9xV3z",
    "X-License-Key": "jK2v8bRm54qY6n",
    "X-Secret-Session": "cM1k7dFpT2vZ6n",
    "X-Api-Key": "vB3q7jWxF1tZ9n",
    "X-Verify-Code": "eF6z2pVkN4mH9b",
    "X-Validate-Hash": "wX9q4bRnM1tJ7y",
    "Range": "bytes=0-0",
    "Accept-Encoding": "identity",
    "Connection": "Keep-Alive",
    # Host НЕ передаём — requests сам поставит из URL
}


def probe(headers, label):
    """Делаем запрос без auto-redirect, возвращаем (status, location)."""
    try:
        r = requests.get(URL, headers=headers, allow_redirects=False, timeout=15)
        loc = r.headers.get("Location", "")
        size_hint = r.headers.get("Content-Length", "?")
        ctype = r.headers.get("Content-Type", "?")
        print(
            f"  [{label:32}] {r.status_code} | type={ctype} | len={size_hint} | "
            f"loc={loc[:60]}"
        )
        return r.status_code, loc
    except Exception as e:
        print(f"  [{label:32}] EXC: {type(e).__name__}: {e}")
        return None, None


def is_ok(status):
    """206 = Partial Content (Range OK), 200 = full content. 3xx = редирект."""
    return status in (200, 206)


def main():
    print(f"URL: {URL}\n")

    # === Шаг 1: baseline со всеми хедерами ===
    print("=== Baseline (все хедеры) ===")
    status, _ = probe(ALL_HEADERS, "ALL")
    if not is_ok(status):
        print(
            f"\n[!] Baseline уже падает (status={status}). "
            "URL устарел или сервер вообще блокирует."
        )
        sys.exit(1)
    print()

    # === Шаг 2: убираем по одному ===
    print("=== Убираем по одному хедеру ===")
    required = []
    optional = []
    for hname in list(ALL_HEADERS.keys()):
        reduced = {k: v for k, v in ALL_HEADERS.items() if k != hname}
        status, loc = probe(reduced, f"без {hname}")
        if is_ok(status):
            optional.append(hname)
        else:
            required.append(hname)
    print()

    # === Шаг 3: минимальный набор — только required ===
    print("=== Минимальный набор (только required) ===")
    minimal = {k: ALL_HEADERS[k] for k in required}
    if minimal:
        status, loc = probe(minimal, "MIN")
        if not is_ok(status):
            print("[!] Минимум не работает — какие-то хедеры взаимозависимы.")
    else:
        print("  (нет обязательных по индивидуальной проверке)")
        # Пробуем вообще без хедеров
        status, _ = probe({}, "пустой")
    print()

    # === Итог ===
    print("=" * 60)
    print(f"OPTIONAL (можно убрать, {len(optional)}):")
    for h in optional:
        print(f"  - {h}")
    print(f"\nREQUIRED (нужно оставить, {len(required)}):")
    for h in required:
        print(f"  - {h}")
    print("=" * 60)


if __name__ == "__main__":
    main()
