#!/usr/bin/env python3
"""Все харнессы — на сборках (то, что вставляется в Tilda): Production-test
для U6 и тестового U2, Production — для боевого U2.

    npm run test:harness

1. Проверяет, что Production актуален (tools/build-production.mjs --check).
2. Собирает и прогоняет в headless Chromium (python-пакет playwright):
   - docs/testing/lk-tab-harness       — вкладка ЛК, ширины 1000/768/390 и ?site=prod на 1000;
     там же — плавная прокрутка над таб-баром (шаги колеса по 4 px) на 641–1000;
   - docs/testing/dealer-settings-harness — U2/U7, настройки дилера и MODE;
   - docs/testing/phone-validation-harness --prod — маска и валидация телефона.
Выход 1 при любом провале.
"""
import subprocess, sys, threading, functools, http.server, socketserver, pathlib
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
T = ROOT / 'docs' / 'testing'

def sh(args, cwd=ROOT):
    r = subprocess.run(args, cwd=cwd)
    if r.returncode: sys.exit(r.returncode)

sh(['node', 'tools/build-production.mjs', '--check'])

def run_page(directory, page, widths):
    H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    class Srv(socketserver.TCPServer): allow_reuse_address = True
    srv = Srv(('127.0.0.1', 0), lambda *a, **k: H(*a, **k)); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok = True
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w in widths:
            pg = b.new_page(viewport={'width': w, 'height': 900})
            errs = []
            pg.on('pageerror', lambda e: errs.append(str(e)))
            pg.goto(f'http://127.0.0.1:{port}/{page}')
            pg.wait_for_function('typeof window.runAll === "function"')
            res = pg.evaluate('runAll()')
            fails = [l for l in res.split('\n') if l.startswith('FAIL')]
            print(f'  {directory.name} @ {w}px: ' + res.split('\n')[-1])
            for l in fails: print('    ' + l)
            if errs: print('    ошибки страницы:', errs[:3])
            if fails or errs: ok = False
            pg.close()
        b.close()
    srv.shutdown()
    return ok

ok = True
print('== вкладка ЛК ==')
sh(['python3', 'build_harness.py'], cwd=T / 'lk-tab-harness')
ok &= run_page(T / 'lk-tab-harness', 'harness.html', (1000, 768, 390))
# бот не через ngrok, как в боевой сборке: прогрев качает скрипты кабинета
ok &= run_page(T / 'lk-tab-harness', 'harness.html?site=prod', (1000,))

def wheel_check(directory, page):
    """Плавная прокрутка над таб-баром, как у трекпада: 25 шагов колеса по
    4 px. Вся прокрутка должна уйти странице, бар по вертикали не
    сдвигается (план 2026-10-08-001, U3). До правки (overflow-y: auto) на
    641–990 px бар съедал первый шаг: страница проходила 96 из 100 px.
    Полупиксельный ход бара на Retina в headless не виден — здесь рамка 1 px."""
    H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    class Srv(socketserver.TCPServer): allow_reuse_address = True
    srv = Srv(('127.0.0.1', 0), lambda *a, **k: H(*a, **k)); port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok = True
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w in (1000, 900, 768, 641):
            for dpr in (1, 2):
                pg = b.new_page(viewport={'width': w, 'height': 700}, device_scale_factor=dpr)
                pg.goto(f'http://127.0.0.1:{port}/{page}'); pg.wait_for_timeout(1300)
                r = pg.evaluate("""() => { document.body.style.paddingBottom = '3000px';
                    window.ucQuiz.step = 2;
                    window.ucQuiz.selectedCar = { id: 1, fields: { 'Model': 'HAVAL JOLION', 'Complectation': 'Tech+',
                        'Model id': '14', 'Complectation id': '11', 'Price new': 2029000,
                        'Pics': [{ url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' }] } };
                    window.dispatchEvent(new CustomEvent('uc:step', { detail: { step: 2, scroll: false } }));
                    var t = document.getElementById('uc-calc-tabbar');
                    window.scrollTo(0, t.getBoundingClientRect().top + scrollY - 200);
                    var r = t.getBoundingClientRect();
                    return { x: r.left + r.width / 2, y: r.top + r.height / 2, y0: scrollY }; }""")
                pg.mouse.move(r['x'], r['y'])
                for _ in range(25):
                    pg.mouse.wheel(0, 4); pg.wait_for_timeout(16)
                pg.wait_for_timeout(300)
                s = pg.evaluate("({ dy: scrollY, st: document.getElementById('uc-calc-tabbar').scrollTop })")
                dy = round(s['dy'] - r['y0'])
                good = dy == 100 and s['st'] == 0
                ok &= good
                print(f"  плавная прокрутка над таб-баром @ {w}px dpr {dpr}: страница {dy}/100 px, бар scrollTop {s['st']}" + ('' if good else '  ⛔'))
                pg.close()
        b.close()
    srv.shutdown()
    return ok

print('== таб-бар: плавная прокрутка (трекпад) ==')
ok &= wheel_check(T / 'lk-tab-harness', 'harness.html')

print('== телефон (Production-test) ==')
sh(['python3', 'build_harness.py', '--prod'], cwd=T / 'phone-validation-harness')
ok &= run_page(T / 'phone-validation-harness', 'harness.html', (1000,))

print('== настройки дилера U2/U7 ==')
sh(['python3', 'build_harness.py'], cwd=T / 'dealer-settings-harness')
r = subprocess.run(['python3', 'run.py'], cwd=T / 'dealer-settings-harness', capture_output=True, text=True)
print('  ' + r.stdout.strip().split('\n')[-1])
for l in r.stdout.split('\n'):
    if l.startswith('FAIL'): print('    ' + l)
ok &= r.returncode == 0

print('ВСЁ ЗЕЛЁНОЕ' if ok else '⛔ ЕСТЬ ПРОВАЛЫ')
sys.exit(0 if ok else 1)
