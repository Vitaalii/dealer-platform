"""Заглушка бота: держит место сервиса bot в compose до этапа 7. Отвечает только на GET /health.

Делает ту же проверку ключей при старте, что и сервис платформы (ARCHITECTURE.md, 7.4),
пока по пустому списку. Только стандартная библиотека Python.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUIRED_VARS: tuple[str, ...] = ()  # ключи бота добавит этап 7


def missing_vars(environ, required=REQUIRED_VARS) -> list[str]:
    return [name for name in required if not environ.get(name, "").strip()]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"status": "ok", "service": "bot-stub"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # запросы /health в лог не пишем
        pass


def make_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    missing = missing_vars(os.environ)
    if missing:
        print("CONFIG ERROR: missing " + ", ".join(missing), file=sys.stderr, flush=True)
        return 1
    port = int(os.environ.get("BOT_HTTP_PORT") or 8080)
    print(f"bot-stub listening on {port}", flush=True)
    make_server(port).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
