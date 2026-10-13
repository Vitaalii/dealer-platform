"""Ускорение A1–A3 (решение владельца 2026-10-03): сборка кабинета Production,
сжатие, версия в адресе и долгий кэш, сжатие JSON-ответов бота."""
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import zlib

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import main as main_mod

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "widget-custom" / "cabinet-app"
PROD = SRC / "Production"
FILES = ["calc_engine.browser.js", "calc_model.js", "app.js"]


def _manifest():
    return json.loads((PROD / "manifest.json").read_text(encoding="utf-8"))


# ---------------- сборка: актуальность и целостность (без node) ----------------

def test_build_not_stale_vs_sources():
    """Исходник правили, сборку забыли — красный тест: npm run build:cabinet."""
    m = _manifest()
    assert m["order"] == FILES
    for name in FILES:
        src_sha = hashlib.sha256((SRC / name).read_bytes()).hexdigest()
        assert m["files"][name]["source_sha256"] == src_sha, \
            f"{name}: сборка устарела — npm run build:cabinet"


@pytest.mark.parametrize("name", FILES)
def test_build_files_consistent(name):
    code = (PROD / name).read_bytes()
    info = _manifest()["files"][name]
    assert info["version"] == hashlib.sha256(code).hexdigest()[:10]
    assert gzip.decompress((PROD / f"{name}.gz").read_bytes()) == code
    br = PROD / f"{name}.br"
    assert br.is_file() and br.stat().st_size == info["br_bytes"]
    assert b"//" not in code.split(b"\n", 1)[0][:2], "комментарии в начале сборки"
    assert len(code) < (SRC / name).stat().st_size


def _node():
    if shutil.which("node") is None:
        pytest.fail("node не найден — проверка сборки не может быть снята молча")


def test_build_parses_and_calc_model_suite_on_build():
    """Минификация не меняет расчёт: прогон модели величин на сборке."""
    _node()
    for name in FILES:
        proc = subprocess.run(["node", "--check", str(PROD / name)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(["node", "tests/js/calc_model.test.mjs"], cwd=ROOT, capture_output=True,
                          text=True, timeout=60, env={**os.environ, "CABINET_BUILD": "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_mode_when_node_modules_present():
    """npm run check:cabinet — тот же вердикт, что и тест выше (если terser установлен)."""
    if not (ROOT / "node_modules" / "terser").is_dir():
        pytest.skip("node_modules нет — проверка по манифесту выше")
    _node()
    proc = subprocess.run(["node", "tools/build-cabinet.mjs", "--check"], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------- раздача: версия, кэш, заранее сжатые копии ----------------

@pytest_asyncio.fixture
async def static_client():
    app = web.Application(middlewares=[main_mod.compression_middleware])
    app.router.add_static("/widget-custom/", path=str(ROOT / "widget-custom"))
    app.on_response_prepare.append(main_mod.widget_static_utf8_charset)

    async def big_json(request):
        return web.json_response({"text": "Политика " * 400})

    async def small_json(request):
        return web.json_response({"ok": True})

    app.router.add_get("/big", big_json)
    app.router.add_get("/small", small_json)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_versioned_build_long_cache(static_client):
    v = _manifest()["files"]["app.js"]["version"]
    resp = await static_client.get(f"/widget-custom/cabinet-app/Production/app.js?v={v}",
                                   headers={"Accept-Encoding": "identity"})
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "charset=utf-8" in resp.headers["Content-Type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?v=stale00000", "?v=1"])
async def test_unversioned_or_stale_version_no_cache(static_client, query):
    resp = await static_client.get(f"/widget-custom/cabinet-app/Production/app.js{query}")
    assert resp.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_source_files_stay_no_cache(static_client):
    v = _manifest()["files"]["app.js"]["version"]
    resp = await static_client.get(f"/widget-custom/cabinet-app/app.js?v={v}")
    assert resp.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize("enc", ["br", "gzip"])
async def test_precompressed_build_served(static_client, enc):
    resp = await static_client.get("/widget-custom/cabinet-app/Production/app.js",
                                   headers={"Accept-Encoding": enc}, auto_decompress=False)
    assert resp.headers.get("Content-Encoding") == enc
    body = await resp.read()
    suffix = ".br" if enc == "br" else ".gz"
    assert body == (PROD / f"app.js{suffix}").read_bytes()
    assert "javascript" in resp.headers["Content-Type"]


# ---------------- A1: сжатие JSON-ответов ----------------

@pytest.mark.asyncio
async def test_json_over_1kb_gzipped(static_client):
    resp = await static_client.get("/big", headers={"Accept-Encoding": "gzip"},
                                   auto_decompress=False)
    assert resp.headers.get("Content-Encoding") == "gzip"
    raw = await resp.read()
    assert json.loads(zlib.decompress(raw, 16 + zlib.MAX_WBITS))["text"].startswith("Политика")


@pytest.mark.asyncio
async def test_small_json_and_no_accept_encoding_uncompressed(static_client):
    small = await static_client.get("/small", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in small.headers
    big = await static_client.get("/big", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in big.headers


def test_cabinet_json_not_ascii_escaped():
    import cabinet_api
    resp = cabinet_api._json_response({"text": "Политика"})
    assert "Политика".encode("utf-8") in resp.body and b"\\u" not in resp.body


def test_compression_middleware_registered_in_app():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "middlewares=[error_middleware, cors_middleware,\n                                       compression_middleware]" in src
