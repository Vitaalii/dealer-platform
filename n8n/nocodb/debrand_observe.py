#!/usr/bin/env python3
"""Замер поведения модели до и после де-брендинга промпт-блоков.

План: docs/plans/2026-09-16-001-fix-debrand-prompt-blocks-plan.md, юнит U4.

Снимает по исполнениям `AI Agent v3.0` три оси, по которым план обещал
доказать «стало не хуже»:

  * ФОРМАТ РАСЧЁТНОЙ ФРАЗЫ — попала ли в промпт правленая строка примера
    (REFERENCE-блок), и назвала ли модель в ответе чужую марку;
  * УТЕЧКА ШАБЛОНОВ — вышли ли в текст клиенту дословные [Модель],
    [Комплектация], [credit_price] и прочие плейсхолдеры класса B;
  * ДЛИНА И ТОН — длина ответа в символах и число тегов [SPLIT] на ход.

ВАЖНО про ретенцию: исполнения n8n живут ~2.5 суток. Срез «до» имеет смысл
только если снят ДО правки — потом его взять неоткуда. Отсюда обязательный
--label: файлы срезов не перезаписывают друг друга.

Только чтение: обращается к GET /executions и GET /executions/<id>, ничего
никуда не пишет, кроме локального JSON.

Запуск:
  python3 debrand_observe.py --label before
  python3 debrand_observe.py --label after-3d
  python3 debrand_observe.py --compare before after-3d
  python3 debrand_observe.py --selftest              # контрольные кейсы детектора
"""
import json
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_PATH = ROOT / ".env"
OUT_DIR = ROOT / "flow" / "lib" / "snapshots"

WORKFLOW = "fQAS3wmAjVqrKATM"          # AI Agent v3.0 (прод)
RESPONSE_NODES = ("Final Response", "Reminder Respond", "Bypass Respond")

# Плейсхолдеры класса B: их не подставляет ни один узел кода — заполняет модель.
# Появление любого из них в тексте клиенту и есть «утечка шаблона».
LEAK = re.compile(
    r"\[(Модель|Комплектация|Сумма|Имя клиента|credit_price|base_price|"
    r"tradein_price|down_payment|monthly_payment|term)\]")

# --- Детекция чужой марки -------------------------------------------------
#
# ОСТОРОЖНО: «упоминание марки» само по себе нарушением НЕ является. Дилеры
# торгуют разными брендами, и назвать клиенту его собственную марку — штатное
# поведение: исполнение 4395 (прод, 2026-09-14) при
# "model": "НОВЫЙ HAVAL F7" дало «HAVAL F7 в комплектации Premium» — это верная
# подстановка, а не утечка. Прежняя проверка `re.search("haval|dargo")` считала
# такой ход нарушением и давала ложное срабатывание на легитимном трафике.
#
# Нарушение — ровно два случая:
#   (а) «Dargo» в ответе: это модель из СНЯТОГО примера, её появление означает,
#       что модель процитировала образец вместо данных клиента;
#   (б) марка, которой нет в ai_clean_context этого хода (ни в model, ни в
#       названиях комплектаций) — то есть названа чужая марка.
#
# Границы метода: (б) работает на уровне МАРКИ по словарю ниже. Уровень модели
# (например «Jolion» при model="HAVAL F7") ловится только для Dargo — правилом
# (а). Перечислить все модели всех брендов нечем, и попытка сделать это по
# словарю давала бы ложные срабатывания на перефразировках комплектаций.
BRAND_VOCAB = {
    "Haval": r"haval|хавал",
    "Geely": r"geely|джили",
    "Belgee": r"belgee|белджи",
    "Chery": r"chery|чери|черри",
    "Exeed": r"exeed|эксид",
    "Omoda": r"omoda|омода",
    "Jetour": r"jetour|джетур",
    "Changan": r"changan|чанган",
    "Tank": r"\btank\b",
    "Lada": r"\blada\b|\bлада\b",
    "Moskvich": r"москвич|moskvich",
    "Kia": r"\bkia\b|\bкиа\b",
    "Hyundai": r"hyundai|хендай|хёндэ",
    "Toyota": r"toyota|тойота",
    "Nissan": r"nissan|ниссан",
    "Renault": r"renault|рено(?!с)",
    "Skoda": r"skoda|шкода",
    "Volkswagen": r"volkswagen|фольксваген",
    "BMW": r"\bbmw\b|\bбмв\b",
    "Mercedes": r"mercedes|мерседес",
    "Audi": r"\baudi\b|\bауди\b",
    "Mazda": r"mazda|мазда",
}
# Модель из снятого примера. Правило (а) — безусловное.
# Оговорка: если дилер когда-нибудь возьмёт в продажу Haval Dargo, правило
# начнёт давать ложное срабатывание и его надо будет свести к правилу (б).
RETIRED_MODEL = re.compile(r"\bdargo\b|\bдарго\b", re.I)


# Служебные слова, которые приходят в ai_clean_context.model вместе с названием
# («НОВЫЙ HAVAL F7») и опознающей силы не несут.
CTX_STOPWORDS = {"новый", "новая", "новое", "авто", "автомобиль", "комплектации",
                 "комплектация", "руб", "the"}


def _ctx_tokens(ctx_model, ctx_trims):
    raw = re.findall(r"[A-Za-zА-Яа-яЁё0-9]{2,}",
                     " ".join([ctx_model or ""] + [t for t in (ctx_trims or []) if t]))
    return [t for t in raw if t.casefold() not in CTX_STOPWORDS]


def brand_violations(text, ctx_model="", ctx_trims=()):
    """Список нарушений в ответе клиенту. Пустой список = нарушений нет.

    ctx_model / ctx_trims — `model` и названия комплектаций из
    ai_clean_context ЭТОГО хода: то, что модель имела право назвать.

    Марка засчитывается законной в двух случаях: она есть прямо в контексте,
    либо она стоит РЯДОМ с названием машины клиента. Второе — не поблажка, а
    необходимость: в боевых данных `model` сплошь и рядом приходит без марки
    (`"model": "CITYRAY"`), и ассистент дописывает её сам. Исполнения 4441 и
    4406 (прод, 2026-09-15) — «получил вашу заявку на покупку Geely CITYRAY в
    комплектации «Спорт»» при `model='CITYRAY'`: марка верна, в контексте её
    просто нет. Без окна такие ходы попадали бы в нарушения.
    """
    text = text or ""
    out = []
    if RETIRED_MODEL.search(text):
        out.append("Dargo (модель из снятого примера)")
    ctx = " ".join([ctx_model or ""] + [t for t in (ctx_trims or []) if t])
    tokens = _ctx_tokens(ctx_model, ctx_trims)
    for canon, pat in BRAND_VOCAB.items():
        hits = list(re.finditer(pat, text, re.I))
        if not hits or re.search(pat, ctx, re.I):
            continue
        # Марка законна, если хотя бы одно её упоминание стоит рядом с
        # названием машины клиента: окно ±40 символов вокруг совпадения.
        near = any(
            any(tok.casefold() in text[max(0, m.start() - 40):m.end() + 40].casefold()
                for tok in tokens)
            for m in hits)
        if not near:
            out.append(f"чужая марка: {canon}")
    return out

# Подпись строки примера REFERENCE-блока — одна на оба состояния, чтобы срезы
# «до» и «после» считали одно и то же. Совпадает и со старым текстом
# («Haval Dargo в комплектации Elite стоит»), и с новым
# («[Модель] в комплектации [Комплектация] стоит»).
REF_LINE = re.compile(r"в комплектации .{0,20}стоит \[(?:credit_price|base_price)\]")


def load_env():
    env = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env, path, timeout=45):
    req = urllib.request.Request(
        env["N8N_API_URL"].rstrip("/") + path,
        headers={"X-N8N-API-KEY": env["N8N_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def find_prompt_tokens(node):
    """promptTokens лежит в выводе саб-ноды LM на неустойчивой глубине —
    ищем рекурсивно по ключу, а не по пути (та же причина, что в guard_report)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("promptTokens", "prompt_tokens") and isinstance(v, int):
                return v
            got = find_prompt_tokens(v)
            if got is not None:
                return got
    elif isinstance(node, list):
        for it in node:
            got = find_prompt_tokens(it)
            if got is not None:
                return got
    return None


def ai_context(run_data):
    """`model` и названия комплектаций из ai_clean_context этого хода.

    Это ровно то, что модель имела право назвать клиенту. Собирает Data
    Selector; Prompt Compiler кладёт то же самое в <DATA_FROM_DATABASE>.
    """
    for node in ("Data Selector", "Prompt Compiler"):
        for run in run_data.get(node, []) or []:
            for arr in (run.get("data", {}) or {}).get("main", []) or []:
                for item in arr or []:
                    ctx = (item.get("json") or {}).get("ai_clean_context")
                    if isinstance(ctx, dict):
                        trims = []
                        for c in ctx.get("complectations") or []:
                            if isinstance(c, dict):
                                trims += [c.get("name") or "", c.get("full_name") or ""]
                        return str(ctx.get("model") or ""), [t for t in trims if t]
    return "", []


def node_json(run_data, node_name, key):
    for run in run_data.get(node_name, []) or []:
        for arr in (run.get("data", {}) or {}).get("main", []) or []:
            for item in arr or []:
                val = (item.get("json") or {}).get(key)
                if isinstance(val, str) and val:
                    return val
    return None


def collect(env, limit, budget_s):
    execs = api(env, f"/executions?workflowId={WORKFLOW}&limit={limit}")["data"]
    rows, errors = [], []
    started = time.time()
    for e in execs:
        if time.time() - started > budget_s:
            errors.append(f"остановка по времени после {len(rows)} исполнений")
            break
        try:
            full = api(env, f"/executions/{e['id']}?includeData=true", timeout=60)
        except Exception as err:                       # таймауты API — штатны
            errors.append(f"exec {e['id']}: {err}")
            continue
        rd = full.get("data", {}).get("resultData", {}).get("runData", {})
        prompt = node_json(rd, "Prompt Compiler", "system_prompt_compiled")
        text = None
        for n in RESPONSE_NODES:
            text = node_json(rd, n, "text") or text
        if text is None:
            continue
        ctx_model, ctx_trims = ai_context(rd)
        rows.append({
            "id": e["id"],
            "startedAt": e["startedAt"],
            "status": e.get("status"),
            "reply_chars": len(text),
            "split_count": text.count("[SPLIT]"),
            "leaks_in_reply": sorted(set(LEAK.findall(text))),
            # Контекст хода сохраняем вместе с результатом: без него проверку
            # нельзя перепроверить задним числом, когда исполнение уже
            # выпало из ретенции (~2.5 суток).
            "ctx_model": ctx_model,
            "ctx_trims": ctx_trims,
            "brand_violations": brand_violations(text, ctx_model, ctx_trims),
            "ref_line_in_prompt": bool(prompt and REF_LINE.search(prompt)),
            "placeholder_in_prompt": bool(prompt and LEAK.search(prompt)),
            "prompt_tokens": find_prompt_tokens(rd),
        })
    return rows, errors


def summarize(rows):
    if not rows:
        return {}
    lens = [r["reply_chars"] for r in rows]
    splits = [r["split_count"] for r in rows]
    carried = [r for r in rows if r["placeholder_in_prompt"]]
    return {
        "ответов": len(rows),
        "медиана длины ответа": statistics.median(lens),
        "медиана [SPLIT] на ход": statistics.median(splits),
        "ходов с плейсхолдером в промпте": len(carried),
        "из них с утечкой в ответ": sum(1 for r in carried if r["leaks_in_reply"]),
        "ходов со строкой примера REFERENCE в промпте":
            sum(1 for r in rows if r["ref_line_in_prompt"]),
        # Только нарушения: чужая марка или Dargo. Законно названная марка
        # клиента сюда НЕ попадает — см. brand_violations().
        "ответов с нарушением по марке":
            sum(1 for r in rows if r.get("brand_violations")),
    }


def report(label, rows, errors):
    print(f"\n=== срез «{label}» ===")
    if rows:
        print(f"окно: {rows[-1]['startedAt'][:16]} … {rows[0]['startedAt'][:16]}")
    for k, v in summarize(rows).items():
        print(f"  {k}: {v}")
    for r in rows:
        if r.get("brand_violations"):
            print(f"  (!) exec {r['id']} {r['brand_violations']} "
                  f"при model={r.get('ctx_model')!r}")
    for e in errors:
        print(f"  (!) {e}")


# Контрольные кейсы детектора. Гоняются флагом --selftest; поводом завести их
# стало ложное срабатывание прежней проверки на исполнении 4395.
SELFTEST = [
    # (описание, текст ответа, model, комплектации, ждём нарушений)
    ("легитимная марка клиента (реальный exec 4395)",
     "Здравствуйте! Получил вашу заявку на покупку HAVAL F7 в комплектации "
     "Premium в кредит с желаемым платежом 30000 руб.",
     "НОВЫЙ HAVAL F7", ["Premium"], False),
    ("легитимная марка клиента, кириллица",
     "Хавал Джолион в комплектации Elite стоит 2 330 000 руб.",
     "Хавал Jolion", ["Elite"], False),
    ("легитимный Geely",
     "Geely Monjaro в комплектации Flagship стоит 3 290 000 рублей.",
     "Geely Monjaro", ["Flagship"], False),
    ("марки нет в контексте, но она рядом с моделью клиента (реальный exec 4441)",
     "Здравствуйте, Виталий! Получил вашу заявку на покупку Geely CITYRAY "
     "в комплектации «Спорт» в кредит с желаемым платежом 30 000 руб.",
     "CITYRAY", ["Спорт"], False),
    ("то же на комбо-ходе (реальный exec 4406)",
     "Если взять эту сумму как первый взнос за новый Geely CITYRAY в "
     "комплектации «Люкс», то минимальный платеж составит 41 000 руб.",
     "CITYRAY", ["Люкс"], False),
    ("ответ без марки вовсе",
     "Уточните, пожалуйста, какую именно модель вы рассматриваете?",
     "", [], False),
    ("ИСКУССТВЕННЫЙ: Haval Dargo при model=Geely — правила (а) и (б)",
     "Haval Dargo в комплектации Elite стоит 3 290 000 руб.",
     "Geely Monjaro", ["Flagship"], True),
    ("ИСКУССТВЕННЫЙ: чужая марка без Dargo — правило (б)",
     "Belgee X50 в комплектации Comfort стоит 1 990 000 руб.",
     "Geely Monjaro", ["Flagship"], True),
    ("ИСКУССТВЕННЫЙ: Dargo при пустом контексте — правило (а)",
     "Например, Dargo в комплектации Elite стоит 2 300 000 руб.",
     "", [], True),
]


def selftest():
    bad = 0
    print("контрольные кейсы детектора чужой марки:\n")
    for desc, text, model, trims, expect in SELFTEST:
        got = brand_violations(text, model, trims)
        ok = bool(got) == expect
        bad += not ok
        print(f"  [{'OK ' if ok else 'СБОЙ'}] {desc}")
        print(f"         ждали нарушение={expect}, получили {got or 'нет'}")
    print(f"\nсбоев: {bad}")
    return bad


def main():
    args = sys.argv[1:]

    if "--selftest" in args:
        sys.exit(1 if selftest() else 0)

    env = load_env()

    # Перепроверки задним числом нет сознательно: она требовала бы хранить в
    # репозитории тексты ответов реальным клиентам. Нарушения считаются в
    # момент съёма, в файл идёт вердикт и контекст хода, но не переписка.
    # Проверить сам детектор можно в любой момент флагом --selftest.

    if "--compare" in args:
        i = args.index("--compare")
        a, b = args[i + 1], args[i + 2]
        for lbl in (a, b):
            data = json.loads((OUT_DIR / f"observe_{lbl}.json").read_text(encoding="utf-8"))
            report(lbl, data["rows"], data.get("errors", []))
        return

    if "--label" not in args:
        print(__doc__)
        sys.exit(2)
    label = args[args.index("--label") + 1]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 60
    budget = int(args[args.index("--budget") + 1]) if "--budget" in args else 240

    rows, errors = collect(env, limit, budget)
    out = OUT_DIR / f"observe_{label}.json"
    out.write_text(json.dumps(
        {"label": label, "workflow": WORKFLOW, "rows": rows, "errors": errors},
        ensure_ascii=False, indent=1), encoding="utf-8")
    report(label, rows, errors)
    print(f"\nсохранено: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
