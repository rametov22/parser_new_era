from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import Count, Max, Min, Q
from django.shortcuts import render
from django.utils import timezone

from .models import Content, ScraperLog, YtConnectContent


# Дашборд кэшируется в Redis; ?refresh=1 пересчитывает принудительно.
CACHE_KEY = "parser_stats:dashboard:v3"
CACHE_TTL = 60  # секунд

# TTL «свежести» (после чего запись считается протухшей и подлежит перепарсу):
KP_TTL_DAYS = 5          # kinopoisk_scheduler.REPARSE_TTL_DAYS
RU_TTL_DAYS = 4          # vavada: окно last_update<=today-4 в spawn_iframe_parsers
SERIAL_REFRESH_DAYS = 2  # yangitv.SERIAL_REFRESH_DAYS
STUCK_MINUTES = 30       # in_progress дольше этого = реально завис (порог expire_task)


# ----------------------------------------------------------------------------
# Хелперы
# ----------------------------------------------------------------------------
def _pct(part, whole):
    return (part / whole * 100) if whole else 0


def _grade(pct):
    """Класс здоровья по проценту готовности: ok / warn / bad."""
    if pct >= 80:
        return "ok"
    if pct >= 40:
        return "warn"
    return "bad"


def _age_min(now, dt):
    return int((now - dt).total_seconds() / 60) if dt else None


# ----------------------------------------------------------------------------
# KP / RU — статистика по Content (is_parsed_*, parsed_at_*, parse_count_*)
# ----------------------------------------------------------------------------
def _stats_for_content_source(source, now, ttl_days, icon, title):
    """
    Статистика одного источника, пишущего в Content: "kp" (Кинопоиск) или
    "ru" (Vavada). Vavada скоупится окном премьеры (settings.PREMIERE дней).

    Запросов РОВНО 2: большой conditional .aggregate() + гистограмма глубины.
    """
    status_field = f"is_parsed_{source}"
    parsed_at_field = f"parsed_at_{source}"
    count_field = f"parse_count_{source}"

    qs = Content.objects.all()
    subtitle = "вся база KinoPoisk"
    scope_note = None
    if source == "ru":
        today = now.date()
        premiere_days = getattr(settings, "PREMIERE", 365)
        start_date = today - timedelta(days=premiere_days)
        qs = qs.filter(
            Q(premiere__range=(start_date, today))
            | Q(premiere_ru__range=(start_date, today))
        )
        subtitle = f"премьеры за {premiere_days} дн."
        scope_note = (
            f"Vavada парсит только премьеры за последние {premiere_days} дн. "
            f"(не всю базу). Сериалы дополнительно обновляются на новые серии."
        )

    h1 = now - timedelta(hours=1)
    d1 = now - timedelta(days=1)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    fresh_after = now - timedelta(days=ttl_days)
    stuck_after = now - timedelta(minutes=STUCK_MINUTES)

    # --- ЗАПРОС 1: всё одним сканом (conditional aggregation) -----------------
    agg = qs.aggregate(
        total=Count("id"),
        parsed_now=Count("id", filter=Q(**{status_field: "parsed"})),
        in_progress=Count("id", filter=Q(**{status_field: "in_progress"})),
        not_parsed=Count("id", filter=Q(**{status_field: "not_parsed"})),
        # «застряло» = in_progress дольше STUCK_MINUTES (а не любой in_progress,
        # который при захвате в очередь — нормальное рабочее состояние).
        stuck=Count(
            "id",
            filter=Q(**{status_field: "in_progress", f"{parsed_at_field}__lt": stuck_after}),
        ),
        ever=Count("id", filter=Q(**{f"{count_field}__gt": 0})),
        fresh=Count(
            "id",
            filter=Q(**{status_field: "parsed", f"{parsed_at_field}__gte": fresh_after}),
        ),
        newest=Max(parsed_at_field),
        oldest=Min(parsed_at_field),
        # Скорость: ТОЛЬКО реально спарсенные (status=parsed). Иначе refill/диспетчер
        # ставит parsed_at на ЗАХВАТЕ (in_progress) и завышает темп.
        act_hour=Count("id", filter=Q(**{status_field: "parsed", f"{parsed_at_field}__gte": h1})),
        act_day=Count("id", filter=Q(**{status_field: "parsed", f"{parsed_at_field}__gte": d1})),
        act_week=Count("id", filter=Q(**{status_field: "parsed", f"{parsed_at_field}__gte": d7})),
        act_month=Count("id", filter=Q(**{status_field: "parsed", f"{parsed_at_field}__gte": d30})),
    )

    total = agg["total"] or 0
    parsed = agg["ever"] or 0          # охват: спарсено хоть раз (монотонно)
    parsed_now = agg["parsed_now"]
    in_progress = agg["in_progress"]
    not_parsed = agg["not_parsed"]
    fresh = agg["fresh"]
    per_day = agg["act_day"]

    coverage_pct = _pct(parsed, total)
    remaining = not_parsed             # живая очередь прямо сейчас
    eta_days = (
        round(remaining / per_day, 1) if per_day > 0 and remaining > 0 else None
    )

    # --- ЗАПРОС 2: глубина перепарса (распределение parse_count) --------------
    depth_rows = list(
        qs.values(count_field).annotate(c=Count("id")).order_by(count_field)[:12]
    )
    depth_breakdown = [
        {"times": (r[count_field] or 0), "count": r["c"], "pct": _pct(r["c"], total)}
        for r in depth_rows
    ]

    # --- Здоровье карточки ----------------------------------------------------
    if per_day == 0 and remaining > 0:
        health_class, health_label = "idle", "Стоит"
    elif remaining == 0:
        health_class, health_label = "ok", "В норме"
    elif coverage_pct >= 80:
        health_class, health_label = "ok", "Работает"
    elif coverage_pct >= 40:
        health_class, health_label = "warn", "Догоняет"
    else:
        health_class, health_label = "bad", "Отстаёт"

    return {
        "source": source,
        "icon": icon,
        "title": title,
        "subtitle": subtitle,
        "scope_note": scope_note,
        "health_class": health_class,
        "health_label": health_label,
        # охват / очередь / скорость / ETA (крупные плитки)
        "total": total,
        "parsed": parsed,
        "coverage_pct": coverage_pct,
        "remaining": remaining,
        "per_day": per_day,
        "eta_days": eta_days,
        # статус-бар «прямо сейчас»
        "parsed_now": parsed_now,
        "in_progress": in_progress,
        "not_parsed": not_parsed,
        "stuck": agg["stuck"],
        "parsed_now_pct": _pct(parsed_now, total),
        "in_progress_pct": _pct(in_progress, total),
        "not_parsed_pct": _pct(not_parsed, total),
        # скорость по периодам (4 ячейки)
        "activity": [
            {"label": "час", "count": agg["act_hour"]},
            {"label": "сутки", "count": agg["act_day"]},
            {"label": "нед", "count": agg["act_week"]},
            {"label": "мес", "count": agg["act_month"]},
        ],
        # свежесть / метки времени
        "reparse_ttl_days": ttl_days,
        "fresh": fresh,
        "fresh_pct": _pct(fresh, total),
        "newest_parse": agg["newest"],
        "oldest_parse": agg["oldest"],
        # глубина перепарса (свёрнуто; честная замена «циклов»)
        "depth_breakdown": depth_breakdown,
    }


# ----------------------------------------------------------------------------
# Yangi.tv (UZ) — из YtConnectContent, а НЕ из мёртвых Content.is_parsed_uz
# ----------------------------------------------------------------------------
def _stats_for_yangitv(now):
    """
    Пайплайн yangi.tv: Сбор ID → Connect → Player → Сериалы.

    Запросов 4: 1 conditional aggregate по YtConnectContent (обе фазы + сериалы),
    1 count связанных Content, 1 ScraperLog-активность (connect + player),
    1 последний collect_all_ids.
    """
    h1 = now - timedelta(hours=1)
    d1 = now - timedelta(days=1)
    cutoff = now - timedelta(days=SERIAL_REFRESH_DAYS)

    # --- ЗАПРОС 1: статусы обеих фаз + сериалы одним сканом -------------------
    agg = YtConnectContent.objects.aggregate(
        total=Count("id"),
        connect_not_parsed=Count("id", filter=Q(parsing_status="not_parsed")),
        connect_in_progress=Count("id", filter=Q(parsing_status="in_progress")),
        connect_parsed=Count("id", filter=Q(parsing_status="parsed")),
        connect_failed=Count("id", filter=Q(parsing_status="failed")),
        player_not_parsed=Count("id", filter=Q(parsing_status_player="not_parsed")),
        player_in_progress=Count("id", filter=Q(parsing_status_player="in_progress")),
        player_parsed=Count("id", filter=Q(parsing_status_player="parsed")),
        player_failed=Count("id", filter=Q(parsing_status_player="failed")),
        # реальная очередь фазы плеера: connect уже пройден, плеер ещё нет
        # (только такие диспетчер берёт в работу). Чистый player_not_parsed
        # включает ещё не-connect'нутые → завышал бы «в очереди» и ETA.
        player_queue=Count(
            "id",
            filter=Q(parsing_status="parsed", parsing_status_player="not_parsed"),
        ),
        serials_total=Count("id", filter=Q(is_serial=True)),
        serials_parsed=Count(
            "id", filter=Q(is_serial=True, parsing_status_player="parsed")
        ),
        serials_fresh=Count(
            "id",
            filter=Q(
                is_serial=True,
                parsing_status_player="parsed",
                updated_at__gte=cutoff,
            ),
        ),
    )
    total = agg["total"] or 0

    # --- ЗАПРОС 2: связано с основной базой (id_uz заполнен) ------------------
    content_linked = Content.objects.filter(id_uz__isnull=False).count()

    # --- ЗАПРОС 3: скорость connect + player из ScraperLog (default db) -------
    connect_q = Q(task_name__startswith="YT connect ")
    url_q = Q(task_name__startswith="YT movie url ")
    log_agg = (
        ScraperLog.objects.filter(status="success")
        .filter(connect_q | url_q)
        .aggregate(
            c_hour=Count("id", filter=connect_q & Q(created_at__gte=h1)),
            c_day=Count("id", filter=connect_q & Q(created_at__gte=d1)),
            u_hour=Count("id", filter=url_q & Q(created_at__gte=h1)),
            u_day=Count("id", filter=url_q & Q(created_at__gte=d1)),
        )
    )

    # --- ЗАПРОС 4: последний успешный сбор ID --------------------------------
    last_collect = (
        ScraperLog.objects.filter(task_name="YT collect_all_ids", status="success")
        .order_by("-created_at")
        .values("created_at", "message")
        .first()
    )

    connect_parsed = agg["connect_parsed"]
    connect_not_parsed = agg["connect_not_parsed"]
    player_parsed = agg["player_parsed"]
    player_not_parsed = agg["player_not_parsed"]
    player_queue = agg["player_queue"]
    connect_per_day = log_agg["c_day"]
    player_per_day = log_agg["u_day"]
    serials_parsed = agg["serials_parsed"]
    serials_fresh = agg["serials_fresh"]

    player_pct = _pct(player_parsed, total)
    if player_per_day == 0 and player_not_parsed > 0:
        health_class, health_label = "idle", "Стоит"
    else:
        health_class = _grade(player_pct)
        health_label = {"ok": "Работает", "warn": "Догоняет", "bad": "Отстаёт"}[
            health_class
        ]

    return {
        "health_class": health_class,
        "health_label": health_label,
        # стадия 1 — сбор ID
        "collected_total": total,
        "serials_total": agg["serials_total"],
        "last_collect": last_collect["created_at"] if last_collect else None,
        "last_collect_message": last_collect["message"] if last_collect else None,
        # стадия 2 — connect
        "connect_parsed": connect_parsed,
        "connect_in_progress": agg["connect_in_progress"],
        "connect_not_parsed": connect_not_parsed,
        "connect_failed": agg["connect_failed"],
        "connect_pct": _pct(connect_parsed, total),
        "connect_in_progress_pct": _pct(agg["connect_in_progress"], total),
        "connect_failed_pct": _pct(agg["connect_failed"], total),
        "connect_not_parsed_pct": _pct(connect_not_parsed, total),
        "connect_per_day": connect_per_day,
        "connect_eta_days": (
            round(connect_not_parsed / connect_per_day, 1)
            if connect_per_day > 0 and connect_not_parsed > 0
            else None
        ),
        # стадия 3 — player
        "player_parsed": player_parsed,
        "player_in_progress": agg["player_in_progress"],
        "player_not_parsed": player_not_parsed,  # чистый — для мини-стека (сумма ~100%)
        "player_queue": player_queue,            # реальная очередь — для заголовка/ETA
        "player_failed": agg["player_failed"],
        "player_pct": player_pct,
        "player_in_progress_pct": _pct(agg["player_in_progress"], total),
        "player_failed_pct": _pct(agg["player_failed"], total),
        "player_not_parsed_pct": _pct(player_not_parsed, total),
        "player_per_day": player_per_day,
        "player_eta_days": (
            round(player_queue / player_per_day, 1)
            if player_per_day > 0 and player_queue > 0
            else None
        ),
        # стадия 4 — сериалы
        "serials_parsed": serials_parsed,
        "serials_fresh": serials_fresh,
        "serials_waiting": max(serials_parsed - serials_fresh, 0),
        "serial_refresh_days": SERIAL_REFRESH_DAYS,
        # связь
        "content_linked": content_linked,
    }


# ----------------------------------------------------------------------------
# Общий ряд здоровья — считается в Python из уже полученных данных (0 запросов)
# ----------------------------------------------------------------------------
def _build_health(srcs, yt):
    kp, ru = srcs[0], srcs[1]
    online = 0
    online += 1 if kp["per_day"] > 0 else 0
    online += 1 if ru["per_day"] > 0 else 0
    online += 1 if (yt["player_per_day"] > 0 or yt["connect_per_day"] > 0) else 0

    return {
        "parsers_online": online,
        "parsers_total": 3,
        "kp_fresh_pct": kp["fresh_pct"],
        "kp_fresh_class": _grade(kp["fresh_pct"]),
        "kp_ttl_days": KP_TTL_DAYS,
        "ru_pct": ru["coverage_pct"],
        "ru_class": _grade(ru["coverage_pct"]),
        "ru_parsed": ru["parsed"],
        "ru_total": ru["total"],
        "uz_pct": yt["player_pct"],
        "uz_class": _grade(yt["player_pct"]),
        "uz_parsed": yt["player_parsed"],
        "uz_total": yt["collected_total"],
        "parsed_24h": kp["per_day"] + ru["per_day"] + yt["player_per_day"],
        # «застряло/ошибок» = реально зависшие (in_progress > 30 мин) + failed,
        # без нормального рабочего in_progress.
        "stuck_total": (
            kp["stuck"]
            + ru["stuck"]
            + yt["connect_failed"]
            + yt["player_failed"]
        ),
    }


# ----------------------------------------------------------------------------
# Операционное здоровье: работают ли парсеры, тех. ошибки, Chrome, очереди
# ----------------------------------------------------------------------------
def _ops_for_log(now, prefix, error_prefix=None):
    """
    Здоровье одного парсера по ScraperLog (default db): успехи/ошибки за
    час/сутки, время последнего успеха, классификация ошибок по сигнатуре
    (Chrome не стартует / капча / таймаут / доступ). 1 запрос.

    error_prefix — отдельный префикс для логов ОШИБОК, если он отличается от
    успешных (у yangi player: успех "YT movie url ", ошибка "YT player ").
    """
    if error_prefix is None:
        error_prefix = prefix
    ok_pref = Q(task_name__startswith=prefix)
    err_pref = Q(task_name__startswith=error_prefix)

    d1 = now - timedelta(days=1)
    h1 = now - timedelta(hours=1)

    chrome_q = (
        Q(message__icontains="failed to start a thread")
        | Q(message__icontains="session not created")
        | Q(message__icontains="devtoolsactiveport")
        | Q(message__icontains="cannot connect to chrome")
        | Q(message__icontains="chrome not reachable")
        | Q(message__icontains="invalid session id")
        | Q(message__icontains="no such window")
        | Q(message__icontains="renderer")
        | Q(message__icontains="tab crashed")
        | Q(message__icontains="chromedriver")
    )
    captcha_q = (
        Q(message__icontains="captcha")
        | Q(message__icontains="showcaptcha")
        | Q(message__icontains="robot")
    )
    timeout_q = (
        Q(message__icontains="timeout")
        | Q(message__icontains="timed out")
        | Q(message__icontains="timelimit")
    )
    access_q = (
        Q(message__icontains="403")
        | Q(message__icontains="forbidden")
        | Q(message__icontains="401")
        | Q(message__icontains="unauthenticated")
    )
    err24 = Q(status="error", created_at__gte=d1)

    a = ScraperLog.objects.filter(ok_pref | err_pref).aggregate(
        ok_1h=Count("id", filter=ok_pref & Q(status="success", created_at__gte=h1)),
        err_1h=Count("id", filter=err_pref & Q(status="error", created_at__gte=h1)),
        ok_24h=Count("id", filter=ok_pref & Q(status="success", created_at__gte=d1)),
        err_24h=Count("id", filter=err_pref & Q(status="error", created_at__gte=d1)),
        last_ok=Max("created_at", filter=ok_pref & Q(status="success")),
        sig_chrome=Count("id", filter=err_pref & err24 & chrome_q),
        sig_captcha=Count("id", filter=err_pref & err24 & captcha_q),
        sig_timeout=Count("id", filter=err_pref & err24 & timeout_q),
        sig_access=Count("id", filter=err_pref & err24 & access_q),
    )

    ok_1h, err_1h = a["ok_1h"], a["err_1h"]
    ok_24h, err_24h = a["ok_24h"], a["err_24h"]
    err_rate = _pct(err_1h, ok_1h + err_1h)
    last_ok = a["last_ok"]

    if last_ok is None and ok_24h == 0 and err_24h == 0:
        status, cls = "Не запускался", "idle"
    elif ok_1h == 0 and err_1h > 0:
        status, cls = "Падает", "bad"
    elif ok_1h == 0 and ok_24h == 0:
        status, cls = "Стоит", "idle"
    elif ok_1h == 0:
        status, cls = "Редко", "warn"
    elif err_rate >= 30:
        status, cls = "С ошибками", "warn"
    else:
        status, cls = "Работает", "ok"

    sigs = [
        {"label": "Chrome не стартует", "count": a["sig_chrome"]},
        {"label": "Капча/бот-блок", "count": a["sig_captcha"]},
        {"label": "Таймаут", "count": a["sig_timeout"]},
        {"label": "Доступ/403", "count": a["sig_access"]},
    ]
    sigs = sorted([s for s in sigs if s["count"] > 0], key=lambda s: -s["count"])

    return {
        "status": status,
        "status_class": cls,
        "ok_1h": ok_1h,
        "err_1h": err_1h,
        "ok_24h": ok_24h,
        "err_24h": err_24h,
        "err_rate": err_rate,
        "last_ok": last_ok,
        "last_ok_age_min": _age_min(now, last_ok),
        "chrome_issue": a["sig_chrome"],
        "signatures": sigs,
        "has_errors": True,
        "stuck": None,
    }


def _redis_ops(now):
    """Глубины очередей + Chrome-хартбиты воркеров из Redis. Никогда не падает."""
    queues = None
    chrome = None
    try:
        import json
        import redis
        from datetime import datetime

        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=int(settings.REDIS_PORT),
            password=settings.REDIS_PASSWORD,
            decode_responses=True,
        )
        queues = []
        for key, label in (
            ("kp_films_queue", "KP фильмы"),
            ("kp_pages_queue", "KP страницы"),
            ("vavada_queue", "Vavada"),
            ("vavada_serials_queue", "Vavada сериалы"),
        ):
            try:
                queues.append({"label": label, "depth": r.llen(key)})
            except Exception:
                pass

        chrome = []
        for key in ("kp_films", "vavada", "vavada_serials"):
            raw = r.get(f"chrome_health:{key}")
            if not raw:
                continue
            try:
                d = json.loads(raw)
                ts = datetime.fromisoformat(d["ts"])
                age = int((now - ts).total_seconds())
                cnt = int(d.get("chrome", 0))
                if age > 600:
                    cls = "idle"            # хартбит устарел — воркер молчит
                elif cnt >= 300:
                    cls = "bad"             # похоже на течь Chrome
                elif cnt >= 100:
                    cls = "warn"
                else:
                    cls = "ok"
                chrome.append(
                    {"label": key, "count": cnt, "age_sec": age, "cls": cls}
                )
            except Exception:
                pass
    except Exception:
        pass
    return {"queues": queues, "chrome": chrome}


def _ops_health(now, srcs):
    """Собирает операционную сводку по всем парсерам."""
    kp = srcs[0]
    parsers = [
        {
            "title": "Кинопоиск",
            "icon": "🎬",
            "kind": "throughput",  # нет ScraperLog — статус по потоку/застрявшим
            "status": kp["health_label"],
            "status_class": kp["health_class"],
            "ok_24h": kp["per_day"],
            "ok_1h": kp["activity"][0]["count"],
            "stuck": kp["stuck"],
            "last_ok": kp["newest_parse"],
            "last_ok_age_min": _age_min(now, kp["newest_parse"]),
            "has_errors": False,
            "err_1h": None,
            "err_24h": None,
            "err_rate": None,
            "chrome_issue": 0,
            "signatures": [],
        }
    ]
    # (title, icon, success-prefix, error-prefix). У yangi player ошибки
    # логируются под "YT player ", а успехи — под "YT movie url ".
    for title, icon, prefix, err_prefix in (
        ("Vavada", "📺", "Vavada parser ", None),
        ("Vavada сериалы", "🔁", "Vavada serial refresh", None),
        ("Yangi.tv connect", "🔗", "YT connect ", None),
        ("Yangi.tv player", "🎞️", "YT movie url ", "YT player "),
    ):
        d = _ops_for_log(now, prefix, err_prefix)
        d.update({"title": title, "icon": icon, "kind": "log"})
        parsers.append(d)

    # Агрегируем сигнатуры ошибок за 24ч по всем лог-парсерам.
    sig_totals = {}
    for d in parsers:
        for s in d["signatures"]:
            sig_totals[s["label"]] = sig_totals.get(s["label"], 0) + s["count"]
    error_signatures = sorted(
        [{"label": k, "count": v} for k, v in sig_totals.items() if v > 0],
        key=lambda s: -s["count"],
    )

    redis_ops = _redis_ops(now)
    return {
        "parsers": parsers,
        "error_signatures": error_signatures,
        "chrome_errors_24h": sig_totals.get("Chrome не стартует", 0),
        "queues": redis_ops["queues"],
        "chrome": redis_ops["chrome"],
    }


def _build_dashboard():
    """Считает весь дашборд. Результат кэшируется в Redis."""
    now = timezone.now()

    srcs = [
        _stats_for_content_source("kp", now, KP_TTL_DAYS, "🎬", "Кинопоиск"),
        _stats_for_content_source("ru", now, RU_TTL_DAYS, "📺", "Vavada"),
    ]
    yt = _stats_for_yangitv(now)
    health = _build_health(srcs, yt)
    ops = _ops_health(now, srcs)

    return {
        "srcs": srcs,
        "yangitv": yt,
        "health": health,
        "ops": ops,
        "computed_at": now,
    }


@staff_member_required
def parser_stats(request):
    """
    Дашборд состояния парсеров. Результат кэшируется в Redis на CACHE_TTL секунд;
    ?refresh=1 — принудительный пересчёт.
    """
    force = request.GET.get("refresh") == "1"

    payload = None if force else cache.get(CACHE_KEY)
    if payload is None:
        payload = _build_dashboard()
        cache.set(CACHE_KEY, payload, CACHE_TTL)

    computed_at = payload["computed_at"]
    now = timezone.now()
    age_seconds = int((now - computed_at).total_seconds())

    return render(
        request,
        "scrapers/parser_stats.html",
        {
            "srcs": payload["srcs"],
            "yangitv": payload["yangitv"],
            "health": payload["health"],
            "ops": payload["ops"],
            "computed_at": computed_at,
            "age_seconds": age_seconds,
            "cache_ttl": CACHE_TTL,
            "now": now,
        },
    )
