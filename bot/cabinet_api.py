"""U4: тонкие aiohttp-хендлеры auth-API кабинета (/cabinet/api/*).

Строгий лист: НЕ импортирует main.py. Вся бизнес-логика — в cabinet_service;
здесь только разбор запроса, вызов сервиса и трансляция ApiError в JSON-ответ.
CORS для /cabinet/api/* обслуживают middleware в main.py (echo-whitelist
дилерских Origin) — хендлеры заголовков CORS не трогают.

Контракт (origin-дока, раздел API Contract; коды ошибок):
- POST /cabinet/api/auth/init          {token} → need_phone | bound | 410 link_expired
- POST /cabinet/api/auth/request-code  {token, phone?, channel, consent?, use_known?}
      → {status: ok|retry_free} | 410/409 phone_mismatch/422 consent_required,
        invalid_phone/423 locked/429 rate_limited (retry_after — честный остаток
        окна; для канальных лимитов, часового и капа дилера дополнительно
        limit_source: "phone" | "link" | "dealer"; кулдаун — без limit_source)
- GET  /cabinet/api/auth/send-status   токен ТОЛЬКО в заголовке X-Cabinet-Token
      → {state: sending|sent|retry_free|none|expired}
- POST /cabinet/api/auth/verify        {token, code} → {access_token, expires_in,
        profile} | 400 invalid_code (счётчик попыток тратится)
        | 400 code_superseded|code_expired (мёртвый код: погашен новой отправкой /
          использован / истёк — счётчик НЕ тратится) | 423 locked/410

U5 (анкета; R24, R25):
- GET  /cabinet/api/me                 Bearer → {profile, form, steps_status,
        submitted, masked_phone, calc} | 401 unauthorized
- PUT  /cabinet/api/form               Bearer, частичные поля анкеты →
        {status: draft_saved} | 401/409 already_submitted/422 validation
- POST /cabinet/api/form/submit        Bearer, финальные поля анкеты
        (+ опциональный блок сверки calc={item_id, price}) →
        {status: submitted} | 401/409 already_submitted|conflict/422 validation

U5 (план 2026-08-06, калькулятор; R15):
- GET  /cabinet/api/car                Bearer → {name, specs{...}, images[]}
        | 401/404 no_snapshot|not_found
"""
import logging
import os

import json
from aiohttp import web

import cabinet_service
from cabinet_service import ApiError

logger = logging.getLogger("cabinet")


def _json_response(data, **kwargs) -> web.Response:
    """JSON без \\u-экранирования кириллицы (ускорение A1, 2026-10-03): ответы
    кабинета с русскими текстами были в ~3 раза тяжелее (auth/init — 124 КБ)."""
    return web.json_response(data, dumps=_dumps_utf8, **kwargs)


def _dumps_utf8(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


async def _json_body(request: web.Request) -> dict:
    """Оборонительный разбор тела: не-JSON и не-dict → пустой dict."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _error_response(e: ApiError) -> web.Response:
    payload = {"error": e.error}
    payload.update(e.extra)
    return _json_response(payload, status=e.status)


def _client_meta(request: web.Request):
    """IP пишется только как ненадёжный атрибут аудита (R36)."""
    return request.remote, request.headers.get("User-Agent")


def client_ip(request: web.Request):
    """IP клиента для лимитов веб-входа (R42): X-Forwarded-For учитывается,
    только если непосредственный отправитель — доверенный прокси из env
    CABINET_TRUSTED_PROXIES (через запятую). Берётся самый правый адрес
    цепочки, не являющийся доверенным прокси; иначе — request.remote."""
    trusted = {p.strip() for p in os.getenv("CABINET_TRUSTED_PROXIES", "").split(",")
               if p.strip()}
    remote = request.remote
    forwarded = request.headers.get("X-Forwarded-For", "")
    if not trusted or remote not in trusted or not forwarded:
        return remote
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if hop not in trusted:
            return hop
    return remote


async def handle_web_open(request: web.Request) -> web.Response:
    """POST /cabinet/api/web/open — открыть веб-сессию с лендинга (U5).
    Чужой Origin (не из whitelist Website_URL) — 403 forbidden_origin (R49);
    запрос без Origin (не браузер) пропускается — его ограничивают лимиты."""
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/").lower() not in cabinet_service._dealer_origins():
        return _json_response({"error": "forbidden_origin"}, status=403)
    body = await _json_body(request)
    try:
        data = await cabinet_service.open_web_session(
            body, ip=client_ip(request), origin=origin,
            referer=request.headers.get("Referer"))
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_auth_init(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        data = await cabinet_service.auth_init(str(body.get("token") or ""))
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_request_code(request: web.Request) -> web.Response:
    body = await _json_body(request)
    ip, user_agent = _client_meta(request)
    consent = body.get("consent")
    try:
        data = await cabinet_service.request_code(
            str(body.get("token") or ""),
            phone=body.get("phone"),
            channel=str(body.get("channel") or "voice"),
            consent=consent if isinstance(consent, dict) else None,
            use_known=bool(body.get("use_known")),
            ip=ip, user_agent=user_agent,
        )
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_send_status(request: web.Request) -> web.Response:
    # GET: токен только в заголовке (не попадает в access-лог); ?token= не принимается
    token = request.headers.get("X-Cabinet-Token") or ""
    try:
        data = await cabinet_service.send_status(str(token))
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_verify(request: web.Request) -> web.Response:
    body = await _json_body(request)
    ip, user_agent = _client_meta(request)
    try:
        data = await cabinet_service.verify_code(
            str(body.get("token") or ""), body.get("code"),
            ip=ip, user_agent=user_agent,
        )
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_me(request: web.Request) -> web.Response:
    try:
        data = await cabinet_service.get_me(request.headers.get("Authorization"))
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_form_save(request: web.Request) -> web.Response:
    """PUT /cabinet/api/form — автосохранение черновика (R24: только БД)."""
    body = await _json_body(request)
    try:
        data = await cabinet_service.save_form_draft(
            request.headers.get("Authorization"), body)
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_form_submit(request: web.Request) -> web.Response:
    """POST /cabinet/api/form/submit — однократная отправка; финальные значения
    берутся из тела запроса (опциональный блок сверки calc — U5)."""
    body = await _json_body(request)
    try:
        data = await cabinet_service.submit_form(
            request.headers.get("Authorization"), body)
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_car(request: web.Request) -> web.Response:
    """GET /cabinet/api/car — карточка авто по item_id снапшота (U5/R15)."""
    try:
        data = await cabinet_service.get_car_card(request.headers.get("Authorization"))
    except ApiError as e:
        return _error_response(e)
    return _json_response(data)


async def handle_dealer_settings(request: web.Request) -> web.Response:
    """GET /cabinet/api/dealer/{dealer_code} (U13a): настройки дилера для
    лендинга; CORS — общий для /cabinet/api (echo Origin из whitelist)."""
    try:
        data = cabinet_service.dealer_settings(request.match_info.get("dealer_code"))
    except ApiError as e:
        return _error_response(e)
    return _json_response(
        data, headers={"Cache-Control": cabinet_service.DEALER_SETTINGS_CACHE_CONTROL})


async def handle_dealer_legal(request: web.Request) -> web.Response:
    """GET /cabinet/api/dealer/{dealer_code}/legal/{kind} (ускорение A6):
    текст Policy/Agreement по открытию поп-апа, кэш как у настроек дилера."""
    try:
        data = cabinet_service.dealer_legal_text(request.match_info.get("dealer_code"),
                                                 request.match_info.get("kind"))
    except ApiError as e:
        return _error_response(e)
    return _json_response(
        data, headers={"Cache-Control": cabinet_service.DEALER_SETTINGS_CACHE_CONTROL})


def register_routes(app: web.Application) -> None:
    """Регистрация роутов /cabinet/api/* — вызывается из main.py (wiring-only).
    OPTIONS-роуты не нужны: preflight перехватывает cors_middleware."""
    app.router.add_post("/cabinet/api/web/open", handle_web_open)
    app.router.add_post("/cabinet/api/auth/init", handle_auth_init)
    app.router.add_post("/cabinet/api/auth/request-code", handle_request_code)
    app.router.add_get("/cabinet/api/auth/send-status", handle_send_status)
    app.router.add_post("/cabinet/api/auth/verify", handle_verify)
    app.router.add_get("/cabinet/api/me", handle_me)
    app.router.add_put("/cabinet/api/form", handle_form_save)
    app.router.add_post("/cabinet/api/form/submit", handle_form_submit)
    app.router.add_get("/cabinet/api/car", handle_car)
    app.router.add_get("/cabinet/api/dealer/{dealer_code}", handle_dealer_settings)
    app.router.add_get("/cabinet/api/dealer/{dealer_code}/legal/{kind}", handle_dealer_legal)
