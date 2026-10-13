import os
import json
import logging
import re

import bot_identity
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, select, update, func, text, inspect as sa_inspect
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

# Загружаем URL базы данных из переменных окружения (PostgreSQL для Prod / SQLite для Dev)
# Если переменной нет, используем локальный SQLite как запасной вариант
DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/max_database_v2.db")

# Если используем SQLite, создаем папку для базы
if DB_URL.startswith("sqlite"):
    os.makedirs("data", exist_ok=True)

def engine_kwargs_for(url: str) -> dict:
    """[U5 план 2026-09-08-002] Ночное окно PG (~06:00 UTC) рвёт простаивающие
    коннекты: pool_pre_ping проверяет соединение перед выдачей из пула,
    pool_recycle обновляет его раньше серверного таймаута. Для SQLite оба
    параметра не нужны (aiosqlite без сетевых обрывов)."""
    kwargs = {"echo": False}
    if not url.startswith("sqlite"):
        kwargs.update(pool_pre_ping=True, pool_recycle=1800)
    return kwargs


engine = create_async_engine(DB_URL, **engine_kwargs_for(DB_URL))
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)

    platform_user_id = Column(String, unique=True, index=True, nullable=False)
    channel = Column(String, default="max", nullable=False)
    # U2 мультибота (план 2026-09-15-001, KTD-5/R12): бот пользователя — явные
    # колонки, составной platform_user_id не единственный источник. У легаси-
    # записей ID голые; уникальность platform_user_id НЕ меняется (составные
    # ID уникальны по построению). raw_platform_id — «чистый» ID мессенджера
    # для исходящих вызовов API.
    bot_key = Column(String, nullable=True, index=True)
    raw_platform_id = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    
    # --- Интеграции и Стейт ---
    active_deal_id = Column(String, nullable=True) # Для Bitrix24
    context_json = Column(Text, default='{}')      # Кэш квалификации
    
    # Флаг маршрутизации Smart-bot (n8n AI Agent)
    # Если True - сообщения перехватываются и летят в n8n
    is_smartbot_active = Column(Boolean, default=False)

class BotRegistrySnapshot(Base):
    """U1 (план 2026-09-15-001, Д3): снапшот реестра ботов в БД приложения.

    Реестровые поля пишет синхронизация NocoDB→БД (bot_registry._sync_snapshot,
    одной транзакцией); файл в data/ и постоянный том не используются — снапшот
    переживает redeploy вместе с БД. Токенов и секретов здесь НЕТ (R8) — только
    ИМЯ env-переменной и необратимый intent-хэш производного секрета.

    Секретный блок (secret_intent_hash / secret_enforced / confirmed_url /
    confirmed_at) — источник истины гейта проверки секрета (Фаза 0.3, R18);
    его пишет ТОЛЬКО scripts/register_webhooks.py (U7), синхронизация реестра
    эти колонки не трогает."""
    __tablename__ = "bot_registry_snapshot"

    bot_key = Column(String, primary_key=True)
    channel = Column(String, nullable=False)
    brands = Column(String, default="", nullable=False)      # csv, нормализованные ключи
    token_env = Column(String, nullable=False)               # имя переменной, не значение
    environment = Column(String, default="all", nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    is_legacy = Column(Boolean, default=False, nullable=False)
    link = Column(String, nullable=True)   # U9: публичная ссылка бота (/bot-links)
    # U9a: своя база вебхука бота (прокси/Cloudflare Worker обхода блокировки
    # TG); пусто — вебхук прямо на PUBLIC_BASE_URL
    webhook_base = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    secret_intent_hash = Column(String(64), nullable=True)
    secret_enforced = Column(Boolean, default=False, nullable=False)
    confirmed_url = Column(String, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)


class PendingSync(Base):
    __tablename__ = "pending_sync"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    method = Column(String, nullable=False)
    payload = Column(Text, nullable=False) # JSON dump
    attempts = Column(Integer, default=0)
    status = Column(String, default="pending") # pending, fatal
    
    # Заменили на String под формат MAX
    user_id = Column(String, nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

# =======================
# Модели клиентского кабинета (U1)
# =======================

class CabinetToken(Base):
    __tablename__ = "cabinet_tokens"
    __table_args__ = (
        # Составной индекс под выборки воркеров (handoff-таймауты, экспирация, сигнал A)
        Index("ix_cabinet_tokens_status_bound", "status", "bound_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    dealer_code = Column(String, nullable=False)   # ключ в CACHED_DEALERS
    deal_id = Column(String, nullable=True)        # текущая сделка токена (дозаполняется/перепривязывается)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)  # SHA-256; сам токен не хранится
    # U4/R19: nonce детерминированного вывода токена — plaintext в БД не хранится (R4),
    # но URL восстановим: token = HMAC(CABINET_HMAC_KEY, связка + nonce)
    issue_nonce = Column(String(32), nullable=True)
    # Сделка, участвовавшая в выводе токена НА МОМЕНТ ВЫДАЧИ. deal_id меняется
    # (дозаполнение сделки, склейка deal hunt), а вывод «того же URL» (R19)
    # обязан остаться воспроизводимым — иначе каждая смена сделки отзывала бы
    # токен и роняла открытый кабинет клиента. NULL — старая строка: вывод
    # идёт по deal_id, как раньше (пустая строка эквивалентна NULL в выводе).
    issue_deal_id = Column(String, nullable=True)
    bound_phone = Column(String, nullable=True)    # E.164; NULL до первой верификации (TOFU)
    status = Column(String, default="active", nullable=False)  # active / revoked / expired

    # Счётчик неверных вводов кода — живёт на токене за окно, не на коде
    failed_attempts = Column(Integer, default=0, nullable=False)
    attempts_window_start = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    bound_at = Column(DateTime(timezone=True), nullable=True)      # момент верификации; перезапускается при перепривязке на новую заявку (часы таймаут-передачи, план 2026-08-21-002)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)  # последний сигнал жизни (любой auth-вызов API)
    handoff_at = Column(DateTime(timezone=True), nullable=True)    # атомарный захват передачи заявки
    expires_at = Column(DateTime(timezone=True), nullable=True)    # TTL: 72 ч до привязки (веб-вход — 40 мин), 30 дн скользящих после

    # Веб-вход (план 2026-09-30-001, U1): источник выдачи токена. «web» — сессия
    # открыта с лендинга без диалога (только звонок, свои окна и TTL); NULL и
    # «chat» — ссылка Александра (строки, созданные до колонки, остаются чатовыми).
    entry_source = Column(String, nullable=True, default="chat")

    # Один active-токен на user_id — частичный уникальный индекс в init_db()

class CabinetOtp(Base):
    __tablename__ = "cabinet_otp"
    __table_args__ = (
        # Составной индекс под выборки otp_worker (экспирация кодов)
        Index("ix_cabinet_otp_status_sent", "status", "sent_at"),
        # Лимиты по номеру телефона (R4/R5): выборки по phone+channel; строк
        # на номер единицы, составного индекса по времени не требуется.
        Index("ix_cabinet_otp_phone_channel", "phone", "channel"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_id = Column(Integer, ForeignKey("cabinet_tokens.id"), index=True, nullable=False)

    phone = Column(String, nullable=False)         # куда отправлен код (нормализованный)
    code_hmac = Column(String(64), nullable=False) # HMAC-SHA256(код, server key); код не хранится
    external_id = Column(String(36), unique=True, index=True, nullable=False)  # uuid4 для ProstoSMS
    channel = Column(String, nullable=False)       # выбранный клиентом канал: voice / sms

    voice_msg_id = Column(String, nullable=True)   # provider_msg_id: голосовая отправка
    sms_msg_id = Column(String, nullable=True)     # provider_msg_id: SMS-отправка
    delivery_state = Column(String, nullable=True) # последний принятый статус доставки — запись-факт, на поведение не влияет

    sent_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)    # TTL кода 5 минут

    # Жизненный цикл: sending → sent → verified / superseded / expired / failed_send
    status = Column(String, default="sending", nullable=False)

    # Одна запись в статусах sending/sent на token_id — частичный уникальный индекс в init_db()

class CabinetSession(Base):
    __tablename__ = "cabinet_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_id = Column(Integer, ForeignKey("cabinet_tokens.id"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)

    session_hash = Column(String(64), unique=True, nullable=False)  # SHA-256 от bearer-значения

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)  # 40 мин скользящих, 12 ч абсолютных

    # Аудит
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

class CabinetProfile(Base):
    __tablename__ = "cabinet_profile"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True, nullable=False)

    # Карточка «Ваш запрос»
    deal_id = Column(String, nullable=True)
    dealer_code = Column(String, nullable=True)
    brand_model = Column(String, nullable=True)
    scenario_type = Column(String, nullable=True)

    # Желаемый платёж из квалификации; копируется при генерации ссылки, кабинетом не перезаписывается
    qualification_payment = Column(Integer, nullable=True)

    steps_status = Column(Text, default='{}')  # JSON: phone_confirmed / form_submitted

    # U1/U5 (R14): замороженный снапшот входных данных калькулятора — Text+JSON
    # (конвенция steps_status/context_json): schema_version, item_id, price,
    # target_payment, rates, program, found_info, ti_valuation. Наполняет U5;
    # U1 читает профильные значения (steering / ti_valuation) при сабмите.
    calc_snapshot = Column(Text, nullable=True)
    calc_frozen_at = Column(DateTime(timezone=True), nullable=True)

    # Веб-вход (план 2026-09-30-001, U1): данные сессии, открытой с лендинга.
    # lead_code + lead_nonce_hash — повтор «открыть сессию» отдаёт тот же адрес
    # только при совпадении nonce (R64); return_url — страница лендинга для 8f
    # (прошла whitelist Website_URL); web_plate/web_mileage — трейд-ин с лендинга;
    # link_fallback — проверенный form.LINK из запроса (запасной для «Вход в ЛК»);
    # valuation_id — оценка, результат которой ждёт эта сессия (U7);
    # web_form — блок form лендинга (поля формы Tilda, UTM, cookies) и IP
    # клиента, JSON {"fields": {...}, "client_ip": ...} — только для сделки
    # бота (R61–R62); обезличивается через 30 дней (R71).
    lead_code = Column(String, nullable=True, index=True)
    lead_nonce_hash = Column(String(64), nullable=True)
    return_url = Column(String, nullable=True)
    web_plate = Column(String, nullable=True)
    web_mileage = Column(Integer, nullable=True)
    link_fallback = Column(String, nullable=True)
    valuation_id = Column(Integer, nullable=True)
    web_form = Column(Text, nullable=True)

class CabinetForm(Base):
    __tablename__ = "cabinet_form"
    __table_args__ = (
        UniqueConstraint("user_id", "deal_id", name="uq_cabinet_form_user_deal"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    deal_id = Column(String, nullable=False)

    # Кредит
    credit_initial = Column(Integer, nullable=True)  # взнос
    credit_term = Column(Integer, nullable=True)     # срок
    credit_payment = Column(Integer, nullable=True)  # платёж
    income = Column(Integer, nullable=True)
    employment = Column(String, nullable=True)       # U1-дизайн: плитки «Профессиональная деятельность»
    work_experience = Column(String, nullable=True)
    family_composition = Column(String, nullable=True)

    # U1 (дизайн): анкетные плитки и комментарий клиента
    marital_status = Column(String, nullable=True)   # плитки «Семейное положение»
    education = Column(String, nullable=True)        # плитки «Образование»
    owners_by_pts = Column(String, nullable=True)    # плитки «Количество владельцев по ПТС»
    pts_type = Column(String, nullable=True)         # плитки «Оригинал ПТС»
    keys_count = Column(String, nullable=True)       # плитки «Количество ключей»
    client_comment = Column(String, nullable=True)   # textarea комментария клиента
    combo_extra_down = Column(Integer, nullable=True)  # комбо: «Доплата к взносу»

    # Трейд-ин
    tradein_make = Column(String, nullable=True)
    tradein_model = Column(String, nullable=True)
    tradein_year = Column(Integer, nullable=True)
    tradein_mileage = Column(Integer, nullable=True)
    tradein_condition = Column(String, nullable=True)  # U1-дизайн: плитки «Общее техническое состояние»
    tradein_damage = Column(String, nullable=True)
    tradein_engine = Column(String, nullable=True)
    tradein_plate = Column(String, nullable=True)
    tradein_steering = Column(String, nullable=True)  # веб-вход U9: плитки «Руль» (8c/8e)
    tradein_vin = Column(String, nullable=True)
    tradein_owners = Column(Integer, nullable=True)

    # Поля income / work_experience / family_composition / tradein_damage /
    # tradein_engine / tradein_vin / tradein_owners выведены из состава анкеты
    # (R1, план 2026-08-06): колонки остаются, из FORM_FIELDS и карты UF удалены.

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())  # последнее автосохранение
    submitted_at = Column(DateTime(timezone=True), nullable=True)  # NULL = черновик; read-only после заполнения

class Consent(Base):
    __tablename__ = "consents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    token_id = Column(Integer, ForeignKey("cabinet_tokens.id"), index=True, nullable=False)

    consent_version = Column(String, nullable=False)        # версия текста согласия
    consent_text_hash = Column(String(64), nullable=False)  # SHA-256 текста, который видел клиент

    # Фиксация до отправки первого кода
    granted_at = Column(DateTime(timezone=True), server_default=func.now())
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)

class CabinetEvent(Base):
    __tablename__ = "cabinet_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=True)
    token_id = Column(Integer, ForeignKey("cabinet_tokens.id"), index=True, nullable=True)

    # link_opened / code_requested / code_sent /
    # send_failed / verified / verify_failed / form_submitted / session_expired
    event_type = Column(String, index=True, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    ip = Column(String, nullable=True)
    meta_json = Column(Text, default='{}')  # телефоны — только маской

class CabinetWebOpen(Base):
    """Веб-вход (план 2026-09-30-001, U1): журнал попыток «открыть сессию» для
    лимитов R42–R43 — открытия (kind='open') и повторы с неверным nonce
    (kind='bad_nonce'). Хранится 48 ч (R71), затем удаляется воркером."""
    __tablename__ = "cabinet_web_opens"
    __table_args__ = (
        Index("ix_cabinet_web_opens_ip_created", "ip", "kind", "created_at"),
        Index("ix_cabinet_web_opens_dealer_created", "dealer_code", "kind", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ip = Column(String, nullable=True)
    dealer_code = Column(String, nullable=True)
    kind = Column(String, nullable=False, default="open")   # open / bad_nonce
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CabinetValuation(Base):
    """Веб-вход (план 2026-09-30-001, U1/U7): оценка трейд-ина через вебхук
    n8n и её 24-часовой кэш по ключу «дилер + нормализованный госномер».

    status: pending → success / found_no_price / not_found / failed.
    our_failure=True — сбой на нашей стороне (секрет, неимпортированный
    вебхук, сеть): в кэш не идёт (R65). Одна запись pending на ключ —
    частичный уникальный индекс в init_db(). Через 30 дней plate и
    found_info обезличиваются (R71)."""
    __tablename__ = "cabinet_valuations"
    __table_args__ = (
        Index("ix_cabinet_valuations_key", "dealer_code", "plate", "finished_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dealer_code = Column(String, nullable=False)
    plate = Column(String, nullable=True)            # NULL после обезличивания
    status = Column(String, nullable=False, default="pending")
    reason = Column(String, nullable=True)           # причина неуспеха (для 8e и строки R58)
    our_failure = Column(Boolean, nullable=False, default=False)
    found_info = Column(Text, nullable=True)         # JSON; NULL после обезличивания
    price_from = Column(Integer, nullable=True)
    price_to = Column(Integer, nullable=True)
    request_id = Column(String(36), nullable=True)
    token_id = Column(Integer, ForeignKey("cabinet_tokens.id"), nullable=True)  # сессия-инициатор
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    anonymized_at = Column(DateTime(timezone=True), nullable=True)


# Идемпотентные добавления колонок для существующих БД: таблица → {колонка: DDL-тип}.
# Выполняются ТОЛЬКО недостающие ALTER (фактический список колонок даёт инспектор),
# каждый — в собственной транзакции. Прежний паттерн «всё в одном engine.begin()
# + try/except pass вокруг заведомо падающих ALTER» на PostgreSQL травил общую
# транзакцию первым же упавшим оператором: COMMIT превращался в ROLLBACK,
# откатывался и create_all (новые таблицы!), а в лог шло ложное «✅». На SQLite
# семантики отравленной транзакции нет, поэтому сюита была зелёной.
# Легаси-RENAME max_user_id → platform_user_id удалён: он отработал один раз
# в истории и с тех пор только травил транзакцию (platform_user_id на проде есть).
SCHEMA_COLUMN_UPGRADES = {
    "users": {
        "platform_user_id": "VARCHAR",
        "channel": "VARCHAR DEFAULT 'max'",
        # U2 мультибота (план 2026-09-15-001): аддитивные nullable-колонки,
        # старый код их не читает — выкат безопасен, откат схемы не требуется
        "bot_key": "VARCHAR",
        "raw_platform_id": "VARCHAR",
    },
    # U9: публичная ссылка бота в снапшоте (таблица уже существует на деве)
    # U9a: webhook_base — своя база вебхука (прокси-воркер TG)
    "bot_registry_snapshot": {
        "link": "VARCHAR",
        "webhook_base": "VARCHAR",
    },
    # U4/R19: nonce детерминированного вывода токена; U7/R19: сделка вывода токена
    "cabinet_tokens": {
        "issue_nonce": "VARCHAR",
        "issue_deal_id": "VARCHAR",
        # Веб-вход (план 2026-09-30-001, U1); существующие строки — NULL = чат
        "entry_source": "VARCHAR",
    },
    # U1 (план 2026-08-06): состав анкеты по дизайну
    "cabinet_form": {
        "marital_status": "VARCHAR",
        "education": "VARCHAR",
        "owners_by_pts": "VARCHAR",
        "pts_type": "VARCHAR",
        "keys_count": "VARCHAR",
        "client_comment": "VARCHAR",
        "tradein_steering": "VARCHAR",   # веб-вход U9 (план 2026-09-30-001)
        "combo_extra_down": "INTEGER",
    },
    # U1/U5 (R14): снапшот калькулятора. Тип времени — как DateTime(timezone=True)
    # в модели: на PostgreSQL timestamptz, SQLite тип-имя не проверяет.
    "cabinet_profile": {
        "calc_snapshot": "TEXT",
        "calc_frozen_at": "TIMESTAMP WITH TIME ZONE",
        # Веб-вход (план 2026-09-30-001, U1)
        "lead_code": "VARCHAR",
        "lead_nonce_hash": "VARCHAR",
        "return_url": "VARCHAR",
        "web_plate": "VARCHAR",
        "web_mileage": "INTEGER",
        "link_fallback": "VARCHAR",
        "valuation_id": "INTEGER",
        "web_form": "TEXT",
    },
}

# Частичные уникальные индексы кабинета — боевая гарантия против гонок.
# Одинаковый DDL для PostgreSQL и SQLite (частичные уникальные индексы
# в SQLite — с 3.8.0), идемпотентно через IF NOT EXISTS:
# (а) один active-токен на пользователя; (б) одна запись OTP в полёте
# (sending/sent) на токен — закрывает двойную отправку.
SCHEMA_PARTIAL_UNIQUE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cabinet_tokens_active_user "
    "ON cabinet_tokens (user_id) WHERE status = 'active'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cabinet_otp_inflight_token "
    "ON cabinet_otp (token_id) WHERE status IN ('sending', 'sent')",
    # (в) веб-вход: одна оценка в ожидании на «дилер + госномер» — второй
    # сессии с тем же ключом достаётся результат первой (один вызов вебхука)
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cabinet_valuations_pending "
    "ON cabinet_valuations (dealer_code, plate) WHERE status = 'pending'",
    # (г) индекс поиска профиля по коду формы — create_all не создаёт индексы
    # на уже существующей таблице, поэтому явно
    "CREATE INDEX IF NOT EXISTS ix_cabinet_profile_lead_code "
    "ON cabinet_profile (lead_code)",
)

# Легаси-колонки, удалённые из модели (план 2026-08-27-001, U1: снятие
# автофолбэка). На базе, созданной старой схемой, NOT NULL fallback_attempts
# ронял бы КАЖДУЮ вставку OTP (модель колонку больше не передаёт), маскируясь
# под 429 в обработчике гонки. DROP COLUMN поддержан SQLite >=3.35 и PostgreSQL.
# sms_external_id здесь НЕТ: SQLite не умеет DROP для UNIQUE-колонки, а вреда
# от неё нет — nullable, вставки кладут NULL (UNIQUE по NULL не срабатывает);
# на легаси-базах остаётся безвредным пережитком до пересоздания.
SCHEMA_COLUMN_DROPS = {
    "cabinet_otp": ("fallback_at", "fallback_attempts"),
}


def _schema_columns(sync_conn):
    """Фактические колонки таблиц из SCHEMA_COLUMN_UPGRADES (нет таблицы — нет ключа)."""
    insp = sa_inspect(sync_conn)
    return {t: {c["name"] for c in insp.get_columns(t)}
            for t in SCHEMA_COLUMN_UPGRADES if insp.has_table(t)}


def _schema_missing(sync_conn):
    """Чего не хватает до полной схемы: таблицы метаданных + колонки апгрейдов."""
    insp = sa_inspect(sync_conn)
    missing = [t for t in Base.metadata.tables if not insp.has_table(t)]
    for table, columns in SCHEMA_COLUMN_UPGRADES.items():
        if not insp.has_table(table):
            continue  # таблица уже учтена выше, если её нет в метаданных — не наша
        present = {c["name"] for c in insp.get_columns(table)}
        missing += [f"{table}.{c}" for c in columns if c not in present]
    return missing


async def init_db():
    # 1. Таблицы — отдельной транзакцией (create_all идемпотентен)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Недостающие колонки: спросить инспектора, выполнить только нужные ALTER —
    #    каждый в собственной транзакции, штатных падений нет вовсе
    async with engine.connect() as conn:
        existing = await conn.run_sync(_schema_columns)
    for table, columns in SCHEMA_COLUMN_UPGRADES.items():
        for column, ddl_type in columns.items():
            if column in existing.get(table, set()):
                continue
            async with engine.begin() as conn:
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))

    # 2b. Легаси-колонки: удалить, если инспектор их видит — каждая своей
    #     транзакцией (оставленный NOT NULL fallback_attempts ломал бы вставки)
    async with engine.connect() as conn:
        def _present(sync_conn):
            insp = sa_inspect(sync_conn)
            return {t: {c["name"] for c in insp.get_columns(t)}
                    for t in SCHEMA_COLUMN_DROPS if insp.has_table(t)}
        present_drop = await conn.run_sync(_present)
    for table, columns in SCHEMA_COLUMN_DROPS.items():
        for column in columns:
            if column not in present_drop.get(table, set()):
                continue
            async with engine.begin() as conn:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))

    # 2c. U2 мультибота (план 2026-09-15-001): бэкфилл идентичности бота — на
    #     КАЖДОМ старте, а не разово: закрывает окно «строка создана между
    #     деплоями U2 и U3 и осталась без bot_key» (ревью данных). Предикаты:
    #     только bot_key IS NULL и только голые ID — составная строка без
    #     bot_key (аномалия) НЕ портится легаси-значениями. Неизвестный
    #     channel остаётся NULL — легальное состояние, уходит в легаси-фолбэк
    #     KTD-6. Идемпотентно.
    #     U9 (2026-09-23): ключи легаси-ботов зависят от окружения; при
    #     незаданном BOT_ENV — исторические имена (миграция ниже не выполняется).
    _bot_env = os.getenv("BOT_ENV", "").strip().lower()
    if _bot_env == "prod":
        _legacy_max, _legacy_tg = "max-geely", "tg-geely"
    elif _bot_env == "dev":
        _legacy_max, _legacy_tg = "max-test", "tg-test"
    else:
        _legacy_max, _legacy_tg = "max-main", "tg-main"
    legacy_bot_by_channel = {"max": _legacy_max, "telegram": _legacy_tg, "web": "web"}
    for channel_value, legacy_key in legacy_bot_by_channel.items():
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET bot_key = :bk, raw_platform_id = platform_user_id "
                     "WHERE bot_key IS NULL AND channel = :ch "
                     "AND platform_user_id NOT LIKE :composite"),
                {"bk": legacy_key, "ch": channel_value,
                 "composite": f"%{bot_identity.SEPARATOR}%"})

    # 2d. U9-миграция исторических легаси-ключей на ключи окружения
    #     (max-main→max-geely/max-test и т.п.): идемпотентно, на каждом
    #     старте, ТОЛЬКО при известном BOT_ENV — угадывать окружение нельзя.
    #     Откат — обратный UPDATE (runbook выката).
    if _bot_env in ("prod", "dev"):
        for old_key, new_key in (("max-main", _legacy_max), ("tg-main", _legacy_tg)):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET bot_key = :new WHERE bot_key = :old"),
                    {"new": new_key, "old": old_key})

    # 3. Частичные уникальные индексы — идемпотентно, каждый отдельно
    for ddl in SCHEMA_PARTIAL_UNIQUE_INDEXES:
        async with engine.begin() as conn:
            await conn.execute(text(ddl))

    # 4. Отчёт об успехе не должен врать: печатаем только после проверки,
    #    что таблицы и колонки действительно на месте
    async with engine.connect() as conn:
        missing = await conn.run_sync(_schema_missing)
    if missing:
        raise RuntimeError(f"init_db: схема неполна после инициализации: {missing}")
    print("✅ Database initialized successfully.")

# =======================
# Функции для работы с БД
# =======================

async def get_or_create_user(platform_user_id: str, full_name: str = "", username: str = "",
                             channel: str = "max", bot_key: str = None,
                             raw_platform_id: str = None) -> User:
    """Получает пользователя из БД или создает нового с защитой от 'состояния гонки'.

    U2 мультибота (KTD-5): bot_key/raw_platform_id пишутся при создании и
    ДОЗАПОЛНЯЮТСЯ у существующих строк, где они NULL (например, пользователь
    создан путём «контакт как первое событие» до прихода идентичности).
    Непустые значения не перезаписываются (R12)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.platform_user_id == platform_user_id))
        user = result.scalar_one_or_none()

        if not user:
            user = User(platform_user_id=platform_user_id, full_name=full_name,
                        username=username, channel=channel,
                        bot_key=bot_key, raw_platform_id=raw_platform_id)
            session.add(user)
            try:
                await session.commit()
                await session.refresh(user)
                return user
            except IntegrityError:
                await session.rollback()
                result = await session.execute(select(User).where(User.platform_user_id == platform_user_id))
                user = result.scalar_one_or_none()

        if user is not None:
            fills = {}
            if bot_key and not user.bot_key:
                fills["bot_key"] = bot_key
            if raw_platform_id and not user.raw_platform_id:
                fills["raw_platform_id"] = raw_platform_id
            if fills:
                await session.execute(
                    update(User).where(User.platform_user_id == platform_user_id).values(**fills))
                await session.commit()
                for k, v in fills.items():
                    setattr(user, k, v)
        return user

async def ensure_single_active(session, user_id: int) -> bool:
    """Дружелюбная предпроверка перед выдачей токена кабинета: True, если у user_id
    нет active-токена и вставка нового пройдёт. Боевую гарантию от гонок даёт
    частичный уникальный индекс uq_cabinet_tokens_active_user — IntegrityError
    оттуда всё равно нужно обрабатывать."""
    result = await session.execute(
        select(CabinetToken.id)
        .where(CabinetToken.user_id == user_id, CabinetToken.status == "active")
        .limit(1)
    )
    return result.scalar_one_or_none() is None

# --- ЕДИНЫЙ ИСТОЧНИК ИСТИНЫ ПО ТЕЛЕФОНАМ (перенос прод-3.1.2) ---
# Предикат бота: 11 цифр с 7/8 либо 10 цифр с 9, букв <5, длина <=20.
# НЕ путать с cabinet_service.normalize_phone — тот строже (только мобильные
# +79xx, требование шлюза ProstoSMS) и объединению не подлежит.

def normalize_phone(value):
    """Возвращает канонический '+7XXXXXXXXXX' или None, если это не телефон РФ.

    ВНИМАНИЕ: результат используется ТОЛЬКО для проверки валидности.
    В базу по-прежнему пишется исходная строка — формат хранения не меняется,
    чтобы не сломать поиск контактов в Bitrix."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or len(s) > 20:
        return None
    if len(re.sub(r'[^A-Za-zА-Яа-яЁё]', '', s)) >= 5:
        return None
    d = re.sub(r'\D', '', s)
    if len(d) == 11 and d[0] in ('7', '8'):
        return '+7' + d[1:]
    if len(d) == 10 and d[0] == '9':
        return '+7' + d
    return None


def is_valid_phone(value) -> bool:
    """Единственная проверка «есть ли у пользователя телефон»."""
    return normalize_phone(value) is not None


# U7 (R8/AE6): опциональный страж записи телефона из чата. Устанавливается
# cabinet_service.setup() (async callable (platform_user_id, phone) -> bool);
# False — запись пропускается: OTP-подтверждённый номер не перезаписывается
# номером из чата. None (кабинет не сконфигурирован) — поведение прежнее.
PHONE_WRITE_GUARD = None


async def save_user_phone(platform_user_id: str, phone: str, full_name: str = "Unknown",
                          username: str = "", channel: str = "max",
                          bot_key: str = None, raw_platform_id: str = None) -> bool:
    """Сохраняет номер телефона пользователя. Возвращает True, если запись
    выполнена. Две РАЗЛИЧИМЫЕ причины False: невалидный номер (перенос
    3.1.2, лог здесь — последний рубеж от мусора в БД) и страж кабинета
    (R8/AE6 — свой комментарий/алерт о расхождении внутри стража).
    Значение сохраняется КАК ПРИСЛАНО, без канонизации (см. normalize_phone).

    U2 мультибота: channel/bot_key/raw_platform_id пробрасываются в
    get_or_create_user — «контакт как первое событие» на TG-пути (main.py
    вызывает save_user_phone ДО process_input) раньше создавал пользователя
    с дефолтным channel='max' и оставлял бы его без идентичности бота."""
    if not is_valid_phone(phone):
        logger.warning("save_user_phone: отклонено — невалидный номер "
                       "(user=%s, длина=%d)", platform_user_id, len(str(phone or "")))
        return False
    await get_or_create_user(platform_user_id, full_name, username, channel=channel,
                             bot_key=bot_key, raw_platform_id=raw_platform_id)
    if PHONE_WRITE_GUARD is not None:
        try:
            allowed = await PHONE_WRITE_GUARD(platform_user_id, phone)
        except Exception:
            allowed = True  # fail-open: страж не должен ломать штатную воронку
        if not allowed:
            return False
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(User)
            .where(User.platform_user_id == platform_user_id)
            .values(phone=phone)
        )
        await session.commit()
    return True

def record_sync_failure(task: PendingSync) -> bool:
    """Учёт неуспешного реплея pending_sync (R35): инкрементирует attempts,
    на пороге PENDING_SYNC_MAX_ATTEMPTS (дефолт 10) переводит строку в fatal.

    Возвращает True ровно один раз — в момент перехода pending → fatal
    (сигнал вызывающему коду отправить однократный техалерт). Уже-fatal
    строки повторный вызов не трогает. Мутирует ORM-объект; commit — на
    вызывающей стороне."""
    if task.status == "fatal":
        return False
    task.attempts = (task.attempts or 0) + 1
    max_attempts = int(os.getenv("PENDING_SYNC_MAX_ATTEMPTS", "10"))
    if task.attempts >= max_attempts:
        task.status = "fatal"
        return True
    return False