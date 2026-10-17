"""Точка входа: python -m platform_core. Сначала проверка ключей, потом веб-сервер."""
import logging
import os

from .config import check_or_exit


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = check_or_exit(os.environ)
    from .app import run  # aiohttp импортируется только после успешной проверки ключей

    run(settings)


if __name__ == "__main__":
    main()
