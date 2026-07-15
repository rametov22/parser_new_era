"""
Единая утилита для создания/убивания Chrome в парсерах.

Цель:
  - вынести дублирующийся код из vavada.py / kinopoisk.py;
  - стабилизировать headless Chrome (флаги, eager page load, отключение картинок);
  - почистить orphaned chrome/chromedriver процессы, которые копятся
    под pids_limit и приводят к "failed to start a thread for the new session";
  - корректно закрывать драйвер даже при падении renderer.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psutil
from django.conf import settings
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger("chrome_utils")


DEFAULT_CHROME_BIN = "/usr/bin/chromium"
DEFAULT_CHROMEDRIVER_PATH = "/usr/bin/chromedriver"


def get_chrome_count() -> int:
    """Считает активные процессы Chrome/chromium для контроля ресурсов."""
    return sum(
        1
        for proc in psutil.process_iter(["name"])
        if proc.info.get("name") and "chrome" in proc.info["name"].lower()
    )


def _chrome_binary() -> str:
    """Путь к Chrome/Chromium: env → дефолт."""
    return os.environ.get("CHROME_BIN") or config_or_default(
        "CHROME_BIN", DEFAULT_CHROME_BIN
    )


def _chromedriver_binary() -> str:
    """Путь к chromedriver: env → дефолт."""
    return os.environ.get("CHROMEDRIVER_PATH") or config_or_default(
        "CHROMEDRIVER_PATH", DEFAULT_CHROMEDRIVER_PATH
    )


def config_or_default(key: str, default: str) -> str:
    """Пробуем прочитать django-настройку, иначе default."""
    try:
        return getattr(settings, key, default) or default
    except Exception:
        return default


def _chrome_version(binary_path: str) -> str:
    """Определяет версию Chrome для user-agent."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version_line = result.stdout.strip() or result.stderr.strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", version_line)
        if match:
            return match.group(1)
    except Exception as exc:
        logger.warning(f"[chrome-version] не удалось определить версию: {exc}")
    return "120.0.0.0"


def kill_zombie_chrome():
    """
    Убивает orphaned chrome/chromedriver процессы:
      - ppid == 1 (классические зомби);
      - родительский PID больше не существует (воркер умер, не забрал детей).

    НЕ трогает процессы, у которых живой родитель — это защита от соседних
    воркеров Celery, которые сейчас реально парсят.
    """
    current_pid = os.getpid()
    alive_pids = set()
    try:
        alive_pids = {
            p.info["pid"]
            for p in psutil.process_iter(["pid"])
            if p.info.get("pid") and p.info["pid"] != current_pid
        }
    except Exception as exc:
        logger.warning(f"[kill-zombie] не удалось собрать alive pids: {exc}")

    killed = 0
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            pid = proc.info.get("pid")
            ppid = proc.info.get("ppid")
            if pid == current_pid:
                continue
            if "chromedriver" not in name and "chrome" not in name:
                continue

            orphaned = ppid == 1 or (ppid is not None and ppid not in alive_pids)
            if not orphaned:
                continue

            try:
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception as exc:
            logger.warning(f"[kill-zombie] ошибка при обработке процесса: {exc}")

    if killed:
        logger.info(f"[kill-zombie] убито orphaned chrome/chromedriver: {killed}")


def _add_proxy_options(options: Options, user_data_dir: str, proxy_url: str) -> bool:
    """Configure a proxy and return whether an auth extension was created."""
    parsed = urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise ValueError("proxy_url must contain scheme, host and port")

    proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    if parsed.username is None and parsed.password is None:
        options.add_argument(f"--proxy-server={proxy_server}")
        return False

    if parsed.username is None or parsed.password is None:
        raise ValueError("proxy_url must contain both username and password")

    extension_dir = Path(user_data_dir) / "proxy_auth_extension"
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Vavada proxy authentication",
        "version": "1.0.0",
        "permissions": [
            "proxy",
            "storage",
            "webRequest",
            "webRequestAuthProvider",
        ],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
    }
    proxy_config = {
        "mode": "fixed_servers",
        "rules": {
            "singleProxy": {
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "port": parsed.port,
            },
            "bypassList": ["localhost", "127.0.0.1", "::1"],
        },
    }
    credentials = {
        "username": unquote(parsed.username),
        "password": unquote(parsed.password),
    }
    background = (
        f"const proxyConfig = {json.dumps(proxy_config)};\n"
        f"const credentials = {json.dumps(credentials)};\n"
        "chrome.proxy.settings.set({value: proxyConfig, scope: 'regular'});\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  (details, callback) => callback(\n"
        "    details.isProxy ? {authCredentials: credentials} : {}\n"
        "  ),\n"
        "  {urls: ['<all_urls>']},\n"
        "  ['asyncBlocking']\n"
        ");\n"
    )
    (extension_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (extension_dir / "background.js").write_text(background, encoding="utf-8")

    options.add_argument(f"--disable-extensions-except={extension_dir}")
    options.add_argument(f"--load-extension={extension_dir}")
    return True


def _build_options(
    user_data_dir: str,
    binary_path: str,
    proxy_url: str | None = None,
    allow_third_party_cookies: bool = False,
) -> Options:
    """Собирает опции Chrome, оптимизированные для headless-парсинга в контейнере."""
    options = Options()
    options.binary_location = binary_path
    options.page_load_strategy = "eager"

    # Headless + container-friendly flags.
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")

    # Уменьшаем число фоновых процессов и сетевой активности.
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    uses_proxy_extension = False
    if proxy_url:
        uses_proxy_extension = _add_proxy_options(options, user_data_dir, proxy_url)
    if not uses_proxy_extension:
        options.add_argument("--disable-extensions")
    options.add_argument("--disable-sync")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    # Отключаем фичи, которые создают лишние процессы / тяжёлый JS.
    options.add_argument(
        "--disable-features=IsolateOrigins,site-per-process,Translate,"
        "OptimizationHints,InterestFeedContentSuggestions,"
        "CertificateTransparencyComponentUpdater,AutofillServerCommunication,"
        "PrivacySandboxSettings4"
    )

    # Отключаем картинки — снижает нагрузку на renderer при загрузке iframe.cloud.
    options.add_argument("--blink-settings=imagesEnabled=false")

    # Уникальный профиль, чтобы параллельные инстансы не конфликтовали за кеш/lock.
    options.add_argument(f"--user-data-dir={user_data_dir}")

    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")
    options.add_argument("--disable-blink-features=AutomationControlled")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    prefs = {"intl.accept_languages": "ru,ru-RU,en-US,en"}
    if allow_third_party_cookies:
        prefs.update(
            {
                "profile.block_third_party_cookies": False,
                "profile.cookie_controls_mode": 0,
                "profile.default_content_setting_values.cookies": 1,
            }
        )
    options.add_experimental_option("prefs", prefs)

    # User-agent совпадает с реальной версией Chrome.
    version = _chrome_version(binary_path)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )

    return options


def create_chrome_driver(
    stealth: bool = False,
    page_load_timeout: int = 30,
    script_timeout: int = 30,
    proxy_url: str | None = None,
    allow_third_party_cookies: bool = False,
):
    """
    Создаёт headless Chrome с максимальной стабильностью.

    Args:
        stealth: если True, применяется selenium-stealth (нужно для Vavada).
        page_load_timeout: сколько секунд ждать загрузку страницы.
        script_timeout: сколько секунд ждать выполнение JS.
        proxy_url: HTTP(S)/SOCKS proxy URL, optionally with basic auth.
        allow_third_party_cookies: разрешить обычные cookies в cross-site iframe.
    """
    kill_zombie_chrome()

    binary_path = _chrome_binary()
    chromedriver_path = _chromedriver_binary()

    user_data_dir = tempfile.mkdtemp(prefix="chrome_profile_")
    options = _build_options(
        user_data_dir,
        binary_path,
        proxy_url=proxy_url,
        allow_third_party_cookies=allow_third_party_cookies,
    )

    service = Service(executable_path=chromedriver_path)
    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception:
        # webdriver.Chrome может упасть, но chromedriver уже запущен — останавливаем.
        with suppress(Exception):
            service.stop()
        # Пытаемся почистить созданный профиль.
        with suppress(Exception):
            shutil.rmtree(user_data_dir, ignore_errors=True)
        raise

    # Сохраняем путь к профилю, чтобы cleanup смог его удалить.
    driver._yt_profile_dir = user_data_dir  # type: ignore[attr-defined]

    try:
        driver.set_page_load_timeout(page_load_timeout)
        driver.set_script_timeout(script_timeout)

        if allow_third_party_cookies:
            driver.execute_cdp_cmd(
                "Network.setCookieControls",
                {"enableThirdPartyCookieRestriction": False},
            )

        if stealth:
            from selenium_stealth import stealth

            stealth(
                driver,
                languages=["ru-RU", "ru", "en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )
        else:
            # Для Kinopoisk достаточно CDP-скрипта, selenium-stealth не нужен.
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                },
            )
    except Exception:
        quit_driver(driver)
        raise

    return driver


def apply_vavada_trust_cookie(driver) -> bool:
    """Install private GreyWeb verification cookies before opening iframe.cloud."""
    trust_value = str(
        getattr(settings, "VAVADA_WD_TRUST_COOKIE", "") or ""
    ).strip()
    approval_value = str(
        getattr(settings, "VAVADA_WD_APPROVAL_COOKIE", "") or ""
    ).strip()
    if not trust_value and not approval_value:
        return False
    if not trust_value or not approval_value:
        raise WebDriverException(
            "Both VAVADA_WD_TRUST_COOKIE and VAVADA_WD_APPROVAL_COOKIE are required"
        )

    cookies = (
        {
            "name": "wd_trust",
            "value": trust_value,
            "domain": ".obrut.show",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
            "priority": "High",
        },
        {
            "name": "wd_approval",
            "value": approval_value,
            "domain": ".obrut.show",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "None",
            "priority": "High",
            "partitionKey": {
                "topLevelSite": "https://iframe.cloud",
                "hasCrossSiteAncestor": True,
            },
        },
    )
    for cookie in cookies:
        result = driver.execute_cdp_cmd("Network.setCookie", cookie)
        if result.get("success") is False:
            raise WebDriverException(
                f"Could not set Vavada {cookie['name']} cookie"
            )

    logger.info(
        "[vavada-cookie] wd_trust + partitioned wd_approval applied "
        "for .obrut.show"
    )
    return True


def quit_driver(driver):
    """Безопасно закрывает Chrome и чистит профиль."""
    if not driver:
        return

    profile_dir = getattr(driver, "_yt_profile_dir", None)

    try:
        driver.quit()
    except WebDriverException:
        # Браузер уже мёртв или renderer отвалился — пробуем остановить сервис.
        try:
            if hasattr(driver, "service") and driver.service:
                driver.service.stop()
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if profile_dir:
            with suppress(Exception):
                shutil.rmtree(profile_dir, ignore_errors=True)
