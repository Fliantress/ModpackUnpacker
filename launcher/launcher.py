# -*- coding: utf-8 -*-
"""
ModpackUnpacker — тонкий лаунчер.

Скачивает актуальный backend.py с GitHub и запускает его через importlib.
Собирается в exe один раз. Обновления бэкенда — только через git push,
exe пересылать друзьям не нужно.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import sys
from pathlib import Path

import requests
from PySide6.QtWidgets import QApplication, QMessageBox

# ⚠️ ЗАМЕНИ НА СВОЙ РЕПОЗИТОРИЙ
REPO_RAW = os.environ.get(
    "MODPACK_REPO_RAW",
    "https://raw.githubusercontent.com/Fliantress/ModpackUnpacker/main",
)
BACKEND_URL = f"{REPO_RAW}/backend/backend.py"
VERSION_URL = f"{REPO_RAW}/version.json"

APP_NAME = "ModpackUnpacker"
APP_DIR_NAME = "ModpackUnpacker"
LOGGER = logging.getLogger("Launcher")


def data_dir() -> Path:
    """Тот же каталог, что использует backend для конфига и лога."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging() -> Path:
    log_file = data_dir() / "launcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_file


def fetch_json(url: str, timeout: int = 10) -> dict:
    resp = requests.get(
        url, timeout=timeout, headers={"Cache-Control": "no-cache"}
    )
    resp.raise_for_status()
    return resp.json()


def ensure_backend() -> Path:
    """Гарантирует актуальный backend.py локально. Возвращает путь."""
    workdir = data_dir()
    backend_path = workdir / "backend.py"
    meta_path = workdir / "backend_version.json"

    try:
        remote_ver = str(fetch_json(VERSION_URL).get("version", "0"))
    except Exception as exc:
        LOGGER.warning("Не удалось получить version.json: %s", exc)
        if backend_path.exists():
            LOGGER.info("Работаю с локальной копией бэкенда")
            return backend_path
        raise RuntimeError(
            "Нет интернета и нет локальной копии бэкенда. "
            "Подключитесь к сети и запустите снова."
        ) from exc

    local_ver = "0"
    if meta_path.exists() and backend_path.exists():
        try:
            local_ver = str(
                json.loads(meta_path.read_text(encoding="utf-8")).get("version", "0")
            )
        except (json.JSONDecodeError, OSError):
            pass

    if remote_ver != local_ver or not backend_path.exists():
        LOGGER.info("Обновляю бэкенд: %s -> %s", local_ver, remote_ver)
        try:
            resp = requests.get(BACKEND_URL, timeout=30)
            resp.raise_for_status()
            content = resp.content
            if len(content) < 1000:
                raise RuntimeError(
                    f"Бэкенд подозрительно маленький ({len(content)} B)"
                )
            backend_path.write_bytes(content)
            meta_path.write_text(
                json.dumps({"version": remote_ver}), encoding="utf-8"
            )
            LOGGER.info("Бэкенд обновлён до %s (%d B)", remote_ver, len(content))
        except Exception as exc:
            LOGGER.exception("Не удалось скачать бэкенд")
            if not backend_path.exists():
                raise RuntimeError(f"Ошибка загрузки бэкенда: {exc}") from exc
            LOGGER.warning("Использую устаревшую локальную копию")
    else:
        LOGGER.info("Бэкенд актуален (версия %s)", local_ver)

    return backend_path


def load_backend(path: Path):
    spec = importlib.util.spec_from_file_location("modpack_backend", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("Не удалось создать спецификацию модуля бэкенда")
    module = importlib.util.module_from_spec(spec)
    sys.modules["modpack_backend"] = module
    spec.loader.exec_module(module)
    return module


def show_error(message: str, log_file: Path) -> None:
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("Ошибка запуска")
    box.setText(f"{message}\n\nПодробности в логе:\n{log_file}")
    box.exec()


def main() -> int:
    log_file = setup_logging()
    LOGGER.info("Лаунчер стартовал, лог: %s", log_file)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    try:
        backend_path = ensure_backend()
    except Exception as exc:
        LOGGER.exception("Ошибка бутстрапа")
        show_error(str(exc), log_file)
        return 1

    try:
        backend = load_backend(backend_path)
    except Exception as exc:
        LOGGER.exception("Ошибка загрузки бэкенда")
        show_error(f"Не удалось загрузить бэкенд: {exc}", log_file)
        return 1

    if not hasattr(backend, "main"):
        show_error("Бэкенд не содержит функцию main(app).", log_file)
        return 1

    try:
        return backend.main(app)
    except Exception as exc:
        LOGGER.exception("Критическая ошибка в бэкенде")
        show_error(f"Критическая ошибка: {exc}", log_file)
        return 1


if __name__ == "__main__":
    sys.exit(main())