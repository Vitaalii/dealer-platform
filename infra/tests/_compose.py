"""Минимальный разбор compose-файлов без pyyaml: блоки сервисов по отступам.

Достаточно для проверок структуры, которые нужны тестам; полную проверку делает
`docker compose config` на сервере (infra/README.md, шаг «Первый запуск»).
"""
import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1]
VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)(:?[-?][^}]*)?\}")


def services(path: Path) -> dict[str, str]:
    """Имя сервиса -> текст его блока (раздел services, отступ 2 пробела)."""
    blocks: dict[str, list[str]] = {}
    in_services = False
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([a-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def strip_comments(text: str) -> str:
    """Текст без строк-комментариев (#): в комментариях бывают примеры вида ${VAR}."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def read(path: Path) -> str:
    return strip_comments(path.read_text(encoding="utf-8"))


def load_module(name: str, path: Path):
    """Загрузка файла как модуля (с регистрацией в sys.modules — её требует dataclasses)."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def variables(text: str) -> dict[str, str]:
    """Имя переменной -> модификатор ('' / ':?' / ':-')."""
    found = {}
    for name, mod in VAR_RE.findall(text):
        kind = mod[:2] if mod else ""
        if found.get(name) != ":?":
            found[name] = kind
    return found


def env_example_keys(path: Path) -> dict[str, str]:
    keys = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            keys[name.strip()] = value
    return keys
