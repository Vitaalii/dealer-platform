"""N5 п.1, вторая попытка: названный Александром платёж комбо против платежа по цене N5.

Для каждого вызова комбо-инструмента в логах архива берётся корневой
combo_calculation (term, monthly_payment, down_payment, rate_text) и
комплектация, чей расчёт с ним совпадает. Цена N5 — из цены Car Search того же
исполнения (tradein_price − credit_bonus). Платёж N5 пересчитывается
calculatePMT с той же ставкой и сроком (ставка по сетке может сдвинуться при
другом проценте взноса — это оговорено в отчёте).
"""
import json, re, sys, collections

ns = {}
src = open('combo_diag.py').read().split('results = []')[0]
src = src.replace("SRC = sys.argv[1]\nrows = json.load(open(SRC))", "")
exec(src, ns)
unflat, walk, bot_messages = ns['unflat'], ns['walk'], ns['bot_messages']

rows = json.load(open(sys.argv[1]))
TEST_DEALERS = {'99'}


def pmt(loan, rate_year, months):
    r = rate_year / 12
    if r <= 0.00001:
        return loan / months
    return loan * r / (1 - (1 + r) ** -months)


def round1k(v):
    return int((v / 1000) + 0.5) * 1000 if v >= 0 else -int((-v / 1000) + 0.5) * 1000


def amounts_small(text):
    """Суммы 1 000 … 299 999 (платежи): «35 000 руб», «35000», «35 тыс»."""
    out = []
    for m in re.finditer(r'(\d[\d\s  ]{0,8}\d|\d+)(?:[.,](\d+))?\s*(тыс\w*|т\.р\.|к\b)?', text):
        raw = m.group(1).replace(' ', '').replace(' ', '').replace(' ', '')
        try:
            v = float(raw + ('.' + m.group(2) if m.group(2) else ''))
        except ValueError:
            continue
        if m.group(3):
            v *= 1000
        if 1000 <= v < 300_000:
            out.append(round(v))
    return out


table = []
seen_calls = set()
for r in rows:
    logs = r.get('logs')
    if not logs:
        continue
    try:
        execs = json.loads(logs)
    except Exception:
        continue
    car = {}
    calls = []
    for e in execs if isinstance(execs, list) else []:
        try:
            data = unflat(e['data'])
        except Exception:
            continue
        for d in walk(data):
            comps = d.get('complectations')
            if isinstance(comps, list):
                model = d.get('model') or (d.get('summary') or {}).get('model_name')
                for c in comps:
                    if isinstance(c, dict):
                        p = c.get('price') or {}
                        if 'base_price' in p and 'tradein_price' in p:
                            car[(str(model), str(c.get('id')))] = p
            root = d.get('combo_calculation')
            if isinstance(root, dict) and isinstance(comps, list) and root.get('monthly_payment') is not None:
                sel = str((d.get('user_request') or {}).get('selected_complectation_id') or '')
                match = None
                for c in comps:
                    if isinstance(c, dict) and c.get('combo_calculation') == root:
                        match = c; break
                if match is None:
                    for c in comps:
                        if isinstance(c, dict) and str(c.get('id')) == sel:
                            match = c; break
                if match is None and comps:
                    match = comps[0]
                te = d.get('tradein_evaluation') or {}
                calls.append({'model': str(d.get('model')), 'comp': match, 'root': root, 'at': e['startedAt'],
                              'wf': e['workflowId'], 'tradein': te.get('estimated_value'),
                              'payment_req': (d.get('user_request') or {}).get('payment')})
    msgs = bot_messages(r.get('Transcript'))
    for c in calls:
        comp = c['comp'] or {}
        key = (c['model'], str(comp.get('id')))
        sig = (r['Id'],) + key + (c['root'].get('monthly_payment'), c['root'].get('term'))
        if sig in seen_calls:
            continue
        seen_calls.add(sig)
        p = comp.get('price') or {}
        cp = car.get(key)
        root = c['root']
        rate_m = re.search(r'(\d+(?:[.,]\d+)?)\s*%', str(root.get('rate_text') or ''))
        rate = float(rate_m.group(1).replace(',', '.')) / 100 if rate_m else None
        live_price = p.get('combo_price')
        n5_price = (cp['tradein_price'] - (cp.get('credit_bonus') or 0)) if cp else None
        down = root.get('down_payment') or 0
        pay_live = root.get('monthly_payment')
        pay_n5 = None
        if n5_price is not None and rate is not None and root.get('term'):
            loan = n5_price - down
            pay_n5 = round1k(pmt(loan, rate, root['term'])) if loan > 0 else 0
        named = any(pay_live in amounts_small(t) for _, t in msgs) if pay_live else False
        table.append({
            'archive_id': r['Id'], 'date': r['CreatedAt'][:10], 'dealer': r.get('Dealer_Code'),
            'test': r.get('Dealer_Code') in TEST_DEALERS or c['wf'] in ('XYF9BIGG9rTU2xcV', 'pkA3mmrvv9XqmZRE'),
            'wf': c['wf'], 'model': c['model'], 'comp': comp.get('name'), 'status': root.get('status'),
            'term': root.get('term'), 'rate_text': root.get('rate_text'), 'tradein': c['tradein'],
            'down': down, 'payment_requested': c['payment_req'],
            'combo_price_live': live_price, 'combo_price_n5': n5_price,
            'price_diff': (n5_price - live_price) if (n5_price is not None and live_price is not None) else None,
            'payment_live': pay_live, 'payment_n5': pay_n5,
            'payment_diff': (pay_n5 - pay_live) if (pay_n5 is not None and pay_live is not None) else None,
            'payment_named_in_transcript': named,
        })

json.dump(table, open('combo_diag2.json', 'w'), ensure_ascii=False, indent=1)
real = [t for t in table if not t['test']]
print('calls', len(table), 'real', len(real), 'named', sum(t['payment_named_in_transcript'] for t in real))
for t in sorted(real, key=lambda t: t['date']):
    print(t['date'], t['dealer'], t['model'], t['comp'], t['status'], 'term', t['term'], t['rate_text'],
          '| price', t['combo_price_live'], '→', t['combo_price_n5'], '| pay', t['payment_live'], '→', t['payment_n5'],
          '| named' if t['payment_named_in_transcript'] else '')
