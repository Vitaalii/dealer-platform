"""Вендорские файлы лендинга, раздаваемые ботом (план 2026-10-11-001, U4/U5):
Swiper 11.2.10 и Tabler Icons Webfont 3.44.0 — байт в байт как jsDelivr,
годовой кэш, CORS для любого сайта, верные Content-Type."""
import base64
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "widget-custom" / "vendor"
PREFIX = "/widget-custom/vendor/"

# integrity, которую лендинг ставит на Swiper (SWIPER_SRI_JS / SWIPER_SRI_CSS в
# Tilda/Landing_2.0/docs/tilda-blocks/Ver_4_dev/u2-unifiedbot-script.html):
# если копии на боте разойдутся с ними, загрузчик лендинга отвергнет бот
LANDING_SRI = {
    "swiper/11.2.10/swiper-bundle.min.js":
        "sha384-2UI1PfnXFjVMQ7/ZDEF70CR943oH3v6uZrFQGGqJYlvhh4g6z6uVktxYbOlAczav",
    "swiper/11.2.10/swiper-bundle.min.css":
        "sha384-gAPqlBuTCdtVcYt9ocMOYWrnBZ4XSL6q+4eXqwNycOr4iFczhNKtnYhF3NEXJM51",
}
# Tabler: сверено с cdn.jsdelivr.net 2026-10-11 (у лендинга integrity нет)
TABLER_SRI = {
    "tabler-icons-webfont/3.44.0/tabler-icons.min.css":
        "sha384-ccZHbezhtZWmNy0cg8odL0D/jFU5k5HIls9y78Qd6lWor7rpvFIZtK0fTFG4z456",
    "tabler-icons-webfont/3.44.0/fonts/tabler-icons.woff2":
        "sha384-FPkp7VVNY4lpgdJjPUrwtb2pod9ZoiZjYcE5E4QQepoZr5o9m4KY2W6NpnYWk8O4",
    "tabler-icons-webfont/3.44.0/fonts/tabler-icons.woff":
        "sha384-FRCTSlnBHQPE4rHTp9oAqJa88/wnZOB9J24M8KYkhUsbyQNWWVdEY8BDLmjS+41S",
    "tabler-icons-webfont/3.44.0/fonts/tabler-icons.ttf":
        "sha384-MZQr7GiYLt8ltTgpDIgqlowIaWVDqM1Hxw8RqkWCKX75UZBOGjd/eGR648CNhHKc",
}
EXPECTED_SRI = {**LANDING_SRI, **TABLER_SRI}


def _sri(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def _manifest() -> dict:
    return json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))["files"]


# ---------------- U4: файлы и целостность (без сети) ----------------

def test_manifest_lists_expected_files():
    files = _manifest()
    assert set(EXPECTED_SRI) <= set(files)
    assert {"swiper/11.2.10/LICENSE", "tabler-icons-webfont/3.44.0/LICENSE"} <= set(files)


@pytest.mark.parametrize("rel", sorted(json.loads(
    (VENDOR / "manifest.json").read_text(encoding="utf-8"))["files"]))
def test_file_matches_manifest(rel):
    entry = _manifest()[rel]
    body = (VENDOR / rel).read_bytes()
    assert len(body) == entry["bytes"]
    assert _sri(body) == entry["integrity"], f"{rel}: файл изменён — должен быть байт в байт как jsDelivr"
    assert entry["url"] == f"https://cdn.jsdelivr.net/npm/{_npm(rel)}"


def _npm(rel: str) -> str:
    pkg, version, path = rel.split("/", 2)
    if pkg == "swiper":
        return f"swiper@{version}/{path}"
    return f"@tabler/icons-webfont@{version}/{path if path == 'LICENSE' else 'dist/' + path}"


@pytest.mark.parametrize("rel,integrity", sorted(EXPECTED_SRI.items()))
def test_integrity_pinned(rel, integrity):
    assert _sri((VENDOR / rel).read_bytes()) == integrity
    assert _manifest()[rel]["integrity"] == integrity


@pytest.mark.parametrize("rel", [r for r in EXPECTED_SRI if r.endswith((".js", ".css"))])
def test_gzip_copy_decompresses_to_original(rel):
    body = (VENDOR / rel).read_bytes()
    assert gzip.decompress((VENDOR / f"{rel}.gz").read_bytes()) == body
    assert (VENDOR / f"{rel}.br").is_file()


def test_vendor_check_tool_passes():
    """br в Python без пакета brotli не распаковать — полная сверка (байты,
    SHA-384, .gz и .br) делается инструментом на node, как сборка кабинета."""
    if shutil.which("node") is None:
        pytest.fail("node не найден — проверка вендорских файлов не может быть снята молча")
    res = subprocess.run(["node", str(ROOT / "tools" / "vendor-assets.mjs"), "--check"],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr


def test_tabler_css_fonts_are_next_to_it():
    css = (VENDOR / "tabler-icons-webfont/3.44.0/tabler-icons.min.css").read_text(encoding="utf-8")
    for name in ("tabler-icons.woff2", "tabler-icons.woff", "tabler-icons.ttf"):
        assert f'url("./fonts/{name}' in css
        assert (VENDOR / "tabler-icons-webfont/3.44.0/fonts" / name).is_file()


@pytest.mark.parametrize("pkg", ["swiper/11.2.10", "tabler-icons-webfont/3.44.0"])
def test_license_present(pkg):
    text = (VENDOR / pkg / "LICENSE").read_text(encoding="utf-8")
    assert "MIT" in text and "Copyright" in text


def test_git_keeps_bytes():
    assert "widget-custom/vendor/** -text" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


def test_vendor_files_go_into_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert not any(line.strip().startswith(("widget-custom", "*.woff", "*.ttf", "*.css"))
                   for line in ignored)


# ---------------- U5: раздача — та же цепочка middleware и хук, что в main.py ----------------

import pytest_asyncio  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import main as main_mod  # noqa: E402

IMMUTABLE = "public, max-age=31536000, immutable"
SWIPER_JS = PREFIX + "swiper/11.2.10/swiper-bundle.min.js"
SWIPER_CSS = PREFIX + "swiper/11.2.10/swiper-bundle.min.css"
TABLER_CSS = PREFIX + "tabler-icons-webfont/3.44.0/tabler-icons.min.css"
FONTS = PREFIX + "tabler-icons-webfont/3.44.0/fonts/"


@pytest_asyncio.fixture
async def client():
    app = web.Application(middlewares=[main_mod.error_middleware, main_mod.cors_middleware,
                                       main_mod.compression_middleware])
    app.router.add_static("/widget-custom/", path=str(ROOT / "widget-custom"))
    app.on_response_prepare.append(main_mod.widget_static_utf8_charset)

    async def api(request):
        return web.json_response({"ok": True})

    app.router.add_get("/cabinet/api/ping", api)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,ctype,rel", [
    (SWIPER_JS, "application/javascript; charset=utf-8", "swiper/11.2.10/swiper-bundle.min.js"),
    (SWIPER_CSS, "text/css; charset=utf-8", "swiper/11.2.10/swiper-bundle.min.css"),
    (TABLER_CSS, "text/css; charset=utf-8", "tabler-icons-webfont/3.44.0/tabler-icons.min.css"),
    (FONTS + "tabler-icons.woff2", "font/woff2", "tabler-icons-webfont/3.44.0/fonts/tabler-icons.woff2"),
    (FONTS + "tabler-icons.woff", "font/woff", "tabler-icons-webfont/3.44.0/fonts/tabler-icons.woff"),
    (FONTS + "tabler-icons.ttf", "font/ttf", "tabler-icons-webfont/3.44.0/fonts/tabler-icons.ttf"),
])
@pytest.mark.parametrize("origin", ["https://demo.autodc.org", "https://stranger.example"])
async def test_served_with_type_cache_cors_and_exact_bytes(client, path, ctype, rel, origin):
    resp = await client.get(path, headers={"Origin": origin, "Accept-Encoding": "identity"})
    assert resp.status == 200
    assert resp.headers["Content-Type"] == ctype
    assert resp.headers["Cache-Control"] == IMMUTABLE
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert _sri(await resp.read()) == _manifest()[rel]["integrity"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enc,suffix", [("br", ".br"), ("gzip", ".gz")])
@pytest.mark.parametrize("path", [SWIPER_JS, SWIPER_CSS, TABLER_CSS])
async def test_precompressed_served(client, enc, suffix, path):
    resp = await client.get(path, headers={"Accept-Encoding": enc}, auto_decompress=False)
    assert resp.headers.get("Content-Encoding") == enc
    assert await resp.read() == (ROOT / path.lstrip("/")).with_name(
        Path(path).name + suffix).read_bytes()
    assert ("javascript" if path.endswith(".js") else "text/css") in resp.headers["Content-Type"]
    assert resp.headers["Cache-Control"] == IMMUTABLE


@pytest.mark.asyncio
async def test_font_with_css_query_string(client):
    """CSS Tabler ссылается на шрифт с ?v3.44.0 — тот же файл и заголовки."""
    resp = await client.get(FONTS + "tabler-icons.woff2?v3.44.0")
    assert resp.status == 200 and resp.headers["Content-Type"] == "font/woff2"
    assert resp.headers["Cache-Control"] == IMMUTABLE


@pytest.mark.asyncio
async def test_etag_revalidation_304(client):
    first = await client.get(SWIPER_JS, headers={"Accept-Encoding": "identity"})
    resp = await client.get(SWIPER_JS, headers={"If-None-Match": first.headers["ETag"],
                                                "Accept-Encoding": "identity"})
    assert resp.status == 304


@pytest.mark.asyncio
async def test_missing_vendor_file_404_without_long_cache(client):
    resp = await client.get(PREFIX + "swiper/11.2.10/nope.js")
    assert resp.status == 404
    assert resp.headers.get("Cache-Control") != IMMUTABLE


@pytest.mark.asyncio
async def test_regression_other_static_and_cabinet_api(client):
    """Регресс: прочая статика и CORS кабинета — как до вендорских файлов."""
    assert (await client.get("/widget-custom/widget.js")).headers["Cache-Control"] == "no-cache"
    assert (await client.get("/widget-custom/cabinet-app/Production/app.js")).headers[
        "Cache-Control"] == "no-cache"
    build = json.loads((ROOT / "widget-custom/cabinet-app/Production/manifest.json").read_text())
    v = build["files"]["app.js"]["version"]
    assert (await client.get(f"/widget-custom/cabinet-app/Production/app.js?v={v}")).headers[
        "Cache-Control"] == IMMUTABLE
    api = await client.get("/cabinet/api/ping", headers={"Origin": "https://stranger.example"})
    assert api.headers.get("Access-Control-Allow-Origin") != "*"
