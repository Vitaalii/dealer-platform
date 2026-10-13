---
title: Вебхук Telegram живёт за Cloudflare Worker — прямой setWebhook на свой хост убил бы бота
date: 2026-09-25
category: integration-issues
module: multibot
problem_type: hidden_infrastructure
root_cause: undocumented_proxy
resolution_type: code_fix
severity: high
component: backend
symptoms:
  - "getWebhookInfo боевого TG-бота показывает чужой хост *.workers.dev, не PUBLIC_BASE_URL"
  - "план перерегистрации предполагал прямой setWebhook на свой хост"
related_components:
  - backend
  - infra
tags: [telegram, webhook, cloudflare-worker, proxy, multibot, register-webhooks]
---

# Вебхук TG за прокси-воркером: база вебхука — свойство бота, не окружения

## Problem

Скрипт перерегистрации (U7/U9) всегда прописывал вебхук на
`PUBLIC_BASE_URL`. На этапе 2.4 прод-выката выяснилось: боевой TG-бот
подписан на Cloudflare Worker (`https://sparkling-sun-8c0c.….workers.dev/tg_webhook`)
— обход блокировки Telegram; прямая доставка TG→наш хост не работает.
Прямой `setWebhook` на PUBLIC_BASE_URL молча убил бы приём TG-событий.
Старый воркер пересылал ТОЛЬКО фиксированный путь `/tg_webhook` — путь на
бота `/tg_webhook/<bot_key>` получил бы 404.

## Solution

1. **База вебхука — свойство бота**: колонка `Webhook_Base` в NocoDB Bots
   (+ снапшот `bot_registry_snapshot.webhook_base`, переживает недоступность
   NocoDB). Пусто → PUBLIC_BASE_URL. `register_webhooks.py expected_url`
   строит `<база бота>/tg_webhook/<bot_key>`; --check/--apply/--enforce и
   сторож (сверяет `confirmed_url` — фактический URL) работают без
   спец-логики.
2. **Воркер сохраняет путь**: пересылает и `/tg_webhook`, и
   `/tg_webhook/<bot_key>` на тот же путь нашего хоста; заголовки — целиком,
   поэтому производный секрет (`X-Telegram-Bot-Api-Secret-Token`) проходит
   и проверяется как обычно. Один воркер обслуживает всех TG-ботов — путь
   различает.
3. **Порядок перехода строго**: сначала обновить ВОРКЕР (новая версия
   обратно совместима — старый путь работает, бот не молчит ни секунды),
   потом код/образ, потом `--apply`. Наоборот нельзя: старый воркер отдаёт
   404 на путь с bot_key.
4. Откат TG — на воркер (`setWebhook?url=<воркер>/tg_webhook`), НИКОГДА на
   прямой хост.

## Prevention

- Перед планированием перерегистрации СНАЧАЛА снять фактическое состояние
  (`getWebhookInfo` / `GET /subscriptions`) — инфраструктура может
  отличаться от предположений плана.
- Любая «база URL» в интеграции — потенциально per-объект, не per-окружение.
