"""N5 п.1: какие combo_price Александр называл клиентам и насколько они ниже цены N5.

Источник: архив диалогов NocoDB (Dialogues_Archive): logs = последние 3 исполнения
сессии (flatted execution data n8n), Transcript = полный текст диалога.
"""
import json, re, sys, collections

SRC = sys.argv[1]
rows = json.load(open(SRC))


def unflat(s):
    arr = json.loads(s)
    memo = {}

    def res(i):
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            out = []; memo[i] = out
            out.extend(ref(x) for x in v)
        elif isinstance(v, dict):
            out = {}; memo[i] = out
            for k, x in v.items():
                out[k] = ref(x)
        else:
            out = v; memo[i] = out
        return out

    def ref(x):
        return res(int(x)) if isinstance(x, str) and x.isdigit() else x

    return res(0)


def walk(o, seen=None):
    """Все dict'ы, включая JSON внутри строк."""
    if seen is None:
        seen = set()
    if id(o) in seen:
        return
    seen.add(id(o))
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v, seen)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, seen)
    elif isinstance(o, str) and ('combo_price' in o or 'base_price' in o) and o.lstrip()[:1] in '[{':
        try:
            yield from walk(json.loads(o), seen)
        except Exception:
            pass


def rub(s):
    """Суммы в тексте → рубли. Возвращает список (значение, фрагмент, точность)."""
    out = []
    for m in re.finditer(r'(\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?)\s*(млн|миллион\w*|тыс\w*|т\.р\.|к\b)?', s):
        raw, unit = m.group(1), (m.group(2) or '')
        num = raw.replace(' ', '').replace('\u00a0', '').replace('\u202f', '').replace(',', '.')
        try:
            v = float(num)
        except ValueError:
            continue
        if unit.startswith(('млн', 'миллион')):
            v *= 1_000_000; exact = False
        elif unit.startswith(('тыс', 'т.р', 'к')):
            v *= 1000; exact = '.' not in num
        else:
            exact = True
        if v >= 300_000:
            out.append((round(v), s[max(0, m.start() - 40):m.end() + 10].replace('\n', ' '), exact))
    return out


def bot_messages(transcript):
    msgs = []
    for m in re.finditer(r'🤖 (\d\d:\d\d) \[B\]Бот:\[/B\](.*?)(?=\n(?:🤖|👤|⚙️) \d\d:\d\d|\Z)', transcript or '', re.S):
        msgs.append((m.group(1), m.group(2)))
    return msgs


results = []
stats = collections.Counter()
for r in rows:
    offers = {}   # (model, comp_id) -> dict
    car = {}      # (model, comp_id) -> car-search price
    logs = r.get('logs')
    if logs:
        try:
            execs = json.loads(logs)
        except Exception:
            execs = []
        for e in execs if isinstance(execs, list) else []:
            try:
                data = unflat(e['data'])
            except Exception:
                continue
            for d in walk(data):
                comps = d.get('complectations')
                if not isinstance(comps, list):
                    continue
                model = d.get('model') or (d.get('summary') or {}).get('model_name')
                for c in comps:
                    if not isinstance(c, dict):
                        continue
                    p = c.get('price') or {}
                    key = (str(model), str(c.get('id')))
                    if 'combo_price' in p:
                        offers[key] = {'name': c.get('name'), 'MSRP': p.get('MSRP'), 'credit_bonus': p.get('credit_bonus'),
                                       'tradein_bonus': p.get('tradein_bonus'), 'combo_price': p.get('combo_price'),
                                       'wf': e['workflowId'], 'at': e['startedAt']}
                    if 'base_price' in p and 'tradein_price' in p:
                        car[key] = p
    if offers:
        stats['dialogs_with_combo_output'] += 1
    msgs = bot_messages(r.get('Transcript'))
    for key, o in offers.items():
        cp = car.get(key)
        correct = None
        if cp and cp.get('tradein_price') is not None:
            correct = cp['tradein_price'] - (cp.get('credit_bonus') or 0)
        named = []
        for t, text in msgs:
            for v, frag, exact in rub(text):
                if o['combo_price'] and (v == o['combo_price'] or (not exact and abs(v - o['combo_price']) <= 0.01 * o['combo_price'])):
                    named.append((t, v, frag, exact))
        if named:
            stats['named_pairs'] += 1
        results.append({
            'archive_id': r['Id'], 'date': r['CreatedAt'][:10], 'dealer': r.get('Dealer_Code'), 'model': key[0],
            'comp_id': key[1], 'comp': o['name'], 'wf': o['wf'],
            'combo_price_live': o['combo_price'], 'correct_n5': correct,
            'diff': (correct - o['combo_price']) if correct is not None and o['combo_price'] is not None else None,
            'car_search_price': cp, 'named': named,
        })

json.dump({'stats': stats, 'results': results}, open(SRC.replace('.json', '_diag.json'), 'w'), ensure_ascii=False, indent=1, default=str)
print(dict(stats), 'offers', len(results))
named = [x for x in results if x['named']]
print('named offers', len(named))
for x in named:
    print(x['date'], x['dealer'], x['model'], x['comp'], 'live', x['combo_price_live'], 'N5', x['correct_n5'], 'diff', x['diff'],
          '|', [(t, v, e) for t, v, f, e in x['named']][:2])
