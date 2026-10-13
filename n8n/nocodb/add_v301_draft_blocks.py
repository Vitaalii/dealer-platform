#!/usr/bin/env python3
"""v3.01: черновые блоки — нарезка T-01_TOOLS + сжатый VOICE, на проверку владельцу.

Добавляет в живую таблицу Prompt_Blocks две НОВЫЕ строки (Active=false):
  T_FINALIZE     — правила finalize_deal 1:1 из живого T-01_TOOLS; компилятор v3.01
                   показывает их только на closing/finalize_required вместо каждого
                   tools_open-хода (на tool_needed инструментальные инструкции уже
                   дают активные ST_*_TOOL_NEEDED владельца)
  G-02_VOICE_V2  — сжатие G-02_VOICE 3365 → ~2000 знаков, все 10 правил сохранены

T_EVALUATE/T_COMBO из первоначального плана НЕ создаются: владелец уже наполнил
и активировал ST_TRADEIN_TOOL_NEEDED / ST_COMBO_TOOL_NEEDED — это те же ШАГ 2-3
в сжатом виде, отдельные tool-блоки их бы задублировали (созданные 2026-07-16
черновики удалены в тот же день).

Существующие строки НЕ трогает; при повторном запуске уже добавленные Block_ID
пропускает. Тексты режутся из ЖИВОЙ таблицы (канонический источник с 2026-07-13),
не из CSV. После пуша снимает полный снапшот Prompt_Blocks + Intent_Block_Map
в scratchpad для оффлайн-симуляции v3_pipeline_sim.test.mjs.

Активация (владельцем, одним заходом в NocoDB):
  TOOLS — включить T_FINALIZE, выключить T-01_TOOLS;
  VOICE — включить G-02_VOICE_V2, выключить G-02_VOICE
  (компилятор v3.01 сам предпочитает новые блоки при Active=true).

Запуск: python3 add_v301_draft_blocks.py [--snapshot-only] [--snapshot PATH]
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
IDS = json.loads((HERE / "table_ids.json").read_text(encoding="utf-8"))

VOICE_V2 = """ПРАВИЛА ТОНА И ФОРМЫ (обязательны в каждом ответе):
0. ЛИМИТ ОБЪЁМА (КРИТИЧНО): максимум 2-3 коротких предложения (с тегом [SPLIT] или без). Пиши сжато — длинные тексты в Telegram не читают.
1. ЗЕРКАЛЬНОСТЬ (КРИТИЧНО): подстраивайся под длину сообщения клиента. Клиент ответил коротко — твой ответ: 1 простое предложение + вопрос.
2. ЖИВОЙ ЯЗЫК: современный городской и профессиональный лексикон (допустимы «shortlist», «ликвидная», «прикинуть»), без канцеляризмов («Чтобы примерно прикинуть, напишите...»). Строго на «Вы»; приветствие только «Здравствуйте», «Привет» КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. ЗАПРЕЩЕНО давить и использовать сухие скрипты («настаиваю», «требую», «вы обязаны») — заменяй мягкими экспертными фразами («Логичный вопрос...»).
3. ИМЯ (КРИТИЧНО): имя клиента — ТОЛЬКО один раз, в самом первом сообщении: «Здравствуйте, [Только Имя]!». Перед использованием проверь: имени нет / оно «Клиент» / содержит цифры или спецсимволы / вызывает сомнения — просто «Здравствуйте!»; латиница — транслитерируй (Vitaly → Виталий); «Имя Фамилия» — оставь только имя. Дальше по диалогу имя СТРОГО ЗАПРЕЩЕНО, в том числе с подтверждениями: не «Понял, Виталий.», а «Понял.».
4. АНТИ-РОБОТ (КРИТИЧНО): ты ЖИВОЙ ЧЕЛОВЕК, сотрудник автосалона. У тебя нет «баз данных», «разработчиков», «алгоритмов», «моделей»; упоминать Google, OpenAI, нейросети или ИИ СТРОГО ЗАПРЕЩЕНО.
5. ФОКУС: отвечай ТОЛЬКО на то, о чём спросил клиент.
6. ИНИЦИАТИВА: заканчивай сообщение ровно ОДНИМ вопросом, двигающим сделку вперёд. Исключение: при вызове `finalize_deal` — прощальный текст или благодарность СТРОГО БЕЗ вопросов и повторных просьб номера.
7. АНТИ-ПОПУГАЙ (КРИТИЧНО): не начинай сообщения с «Понимаю вас», «Спасибо за ответ», «Извините» — используй короткие «Принял», «Зафиксировал», «Отлично». Экономь слова.
8. РАЗНООБРАЗИЕ (КРИТИЧНО): ЗАПРЕЩЕНО повторять одну вводную конструкцию (например, «Чтобы сделать точный расчет...») в двух сообщениях подряд.
9. СТРОГО ЗАПРЕЩЕНО выводить имена технических переменных (model, dealer_id и т.п.) в текст ответа."""


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env, method, path, body=None):
    req = urllib.request.Request(
        env["NOCODB_API_URL"].rstrip("/") + path, method=method,
        headers={"xc-token": env["NOCODB_TOKEN"], "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def fetch_all(env, table_id):
    rows, offset = [], 0
    while True:
        page = api(env, "GET", f"/api/v2/tables/{table_id}/records?limit=200&offset={offset}")
        chunk = page.get("list", [])
        rows.extend(chunk)
        if page.get("pageInfo", {}).get("isLastPage", True) or not chunk:
            return rows
        offset += len(chunk)


def build_rows(blocks):
    by_id = {b["Block_ID"]: b for b in blocks}
    for need in ("T-01_TOOLS", "G-02_VOICE"):
        if need not in by_id:
            sys.exit(f"в живой таблице нет блока {need}")

    t_finalize = by_id["T-01_TOOLS"]["Content"].strip()

    def row(bid, level, scen, stage, text, notes):
        return {"Block_ID": bid, "Level": level, "Scenario": scen, "Stage": stage,
                "Version_Set": "v3.01", "Content": text,
                "Tokens_est": round(len(text) / 4), "Active": False,
                "Intent_Triggers": "", "Notes": notes}

    return [
        row("T_FINALIZE", "tools", "all", "closing", t_finalize,
            "Нарезка T-01_TOOLS: правила finalize_deal 1:1. Компилятор v3.01 показывает только на "
            "closing/finalize_required (на tool_needed инструментальные инструкции уже дают "
            "ST_*_TOOL_NEEDED). Активация: включить T_FINALIZE, выключить T-01_TOOLS."),
        row("G-02_VOICE_V2", "voice", "all", "all", VOICE_V2,
            f"Черновик сжатия G-02_VOICE ({len(by_id['G-02_VOICE']['Content'])} → {len(VOICE_V2)} зн.): все 10 правил "
            "(0-9) сохранены, по одному примеру на правило. Активация: включить V2, выключить G-02_VOICE — "
            "компилятор v3.01 сам предпочтёт V2."),
    ]


def main():
    env = load_env()
    snap_path = Path(sys.argv[sys.argv.index("--snapshot") + 1]) if "--snapshot" in sys.argv else None
    blocks = fetch_all(env, IDS["Prompt_Blocks"])
    print(f"живых блоков: {len(blocks)}")

    if "--snapshot-only" not in sys.argv:
        existing_ids = {b["Block_ID"] for b in blocks}
        rows = build_rows(blocks)
        new_rows = [r for r in rows if r["Block_ID"] not in existing_ids]
        for r in rows:
            state = "добавляю" if r in new_rows else "уже есть, пропускаю"
            print(f"  {r['Block_ID']}: {len(r['Content'])} зн., ~{r['Tokens_est']} ток. — {state}")
        if new_rows:
            api(env, "POST", f"/api/v2/tables/{IDS['Prompt_Blocks']}/records", new_rows)
            print(f"добавлено строк: {len(new_rows)} (все Active=false)")
        blocks = fetch_all(env, IDS["Prompt_Blocks"])

    if snap_path:
        snap = {"blocks": blocks, "map": fetch_all(env, IDS["Intent_Block_Map"])}
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        print(f"снапшот таблиц: {snap_path} ({len(snap['blocks'])} блоков, {len(snap['map'])} строк карты)")


if __name__ == "__main__":
    main()
