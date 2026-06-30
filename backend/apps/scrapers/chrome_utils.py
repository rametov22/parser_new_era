"""
Единая утилита для создания/убивания Chrome в парсерах.

Цель:
  - вынести дублирующийся код из vavada.py / kinopoisk.py;
  - стабилизировать headless Chrome (флаги, eager page load, отключение картинок);
  - почистить orphaned chrome/chromedriver процессы, которые копятся
    под pids_limit и приводят к "failed to start a thread for the new session";
  - корректно закрывать драйвер даже при падении renderer.
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import suppress

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


def _build_options(user_data_dir: str, binary_path: str) -> Options:
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
    options.add_experimental_option(
        "prefs", {"intl.accept_languages": "ru,ru-RU,en-US,en"}
    )

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
):
    """
    Создаёт headless Chrome с максимальной стабильностью.

    Args:
        stealth: если True, применяется selenium-stealth (нужно для Vavada).
        page_load_timeout: сколько секунд ждать загрузку страницы.
        script_timeout: сколько секунд ждать выполнение JS.
    """
    kill_zombie_chrome()

    binary_path = _chrome_binary()
    chromedriver_path = _chromedriver_binary()

    user_data_dir = tempfile.mkdtemp(prefix="chrome_profile_")
    options = _build_options(user_data_dir, binary_path)

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
