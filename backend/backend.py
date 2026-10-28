# -*- coding: utf-8 -*-
"""
ModpackUnpacker — backend.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import requests
from PySide6.QtCore import (
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

APP_NAME = "ModpackUnpacker"
APP_DIR_NAME = "ModpackUnpacker"
LOG_FILENAME = "downloader.log"
CONFIG_FILENAME = "config.json"

CF_API_BASE = "https://api.curseforge.com/v1"
CF_DOWNLOAD_BASE = "https://www.curseforge.com/api/v1"
CF_PROJECT_URL = "https://www.curseforge.com/projects"
# Прямая ссылка на страницу создания ключа — чтобы не искать вручную
CF_CONSOLE_URL = "https://console.curseforge.com/#/api-keys"

RETRY_DELAYS = (1, 3, 9)
MAX_LOG_LINES = 2000
PROGRESS_THROTTLE_MS = 150
EXCLUDED_ROOT_NAMES = {"manifest.json", "modlist.html"}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


def get_data_dir() -> Path:
    """Каталог для конфига и лога — совпадает с data_dir() в лаунчере."""
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


def safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "unnamed"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KiB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MiB"
    return f"{size / 1024**3:.2f} GiB"

def _windows_known_downloads() -> Optional[Path]:
    """
    Windows: путь к «Загрузкам» из реестра.
    Учитывает перенос папки (Properties → Location → Move).
    """
    try:
        import winreg
    except ImportError:
        return None

    # FOLDERID_Downloads
    value_name = "{374DE290-123F-4565-9164-39C4925E467B}"

    candidates = [
        # User Shell Folders — приоритет: сюда Windows пишет при переносе
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        # Shell Folders — запасной вариант (там обычно уже развёрнутые пути)
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
    ]

    for subkey in candidates:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                raw, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if not isinstance(raw, str) or not raw.strip():
            continue

        # В User Shell Folders значения могут содержать %USERPROFILE%
        expanded = os.path.expandvars(raw)
        path = Path(expanded)
        if path.is_dir():
            return path

    return None


def get_downloads_dir() -> Path:
    """
    Папка «Загрузки» с учётом возможного переноса пользователем.
    Windows — реестр; остальные ОС — стандартный ~/Downloads.
    """
    if platform.system() == "Windows":
        detected = _windows_known_downloads()
        if detected is not None:
            return detected

    home = Path.home()
    for name in ("Downloads", "Загрузки", "downloads"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home / "Downloads"


def get_default_packs_root() -> Path:
    """Корень по умолчанию: <папка загрузок>/ModpackUnpacker."""
    return get_downloads_dir() / APP_DIR_NAME


def suggest_output_for_zip(zip_path: Path) -> Path:
    name = re.sub(r'[<>:"/\\|?*]', "_", zip_path.stem).strip() or "modpack"
    return get_default_packs_root() / name


def unique_pack_path(base: Path) -> Path:
    if not base.exists():
        return base
    if base.is_dir() and not any(base.iterdir()):
        return base
    i = 2
    while True:
        candidate = base.parent / f"{base.name} ({i})"
        if not candidate.exists():
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# Проверка API-ключа
# ---------------------------------------------------------------------------


def validate_api_key(api_key: str) -> tuple[bool, str]:
    if not api_key or not api_key.strip():
        return False, "Ключ не задан."

    if not api_key.startswith("$2a$10$"):
        return False, "Ключ не похож на ключ CurseForge (ожидается префикс $2a$10$)."

    try:
        resp = requests.get(
            f"{CF_API_BASE}/games",
            headers={**DEFAULT_HEADERS, "x-api-key": api_key.strip()},
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"Сеть недоступна: {exc}"

    if resp.status_code == 200:
        return True, ""
    if resp.status_code in (401, 403):
        return False, "Ключ недействителен (просрочен или отозван)."
    if resp.status_code == 429:
        return False, "Слишком много запросов (429). Подожди немного."
    return False, f"Неожиданный ответ API: HTTP {resp.status_code}"


class KeyValidator(QThread):
    """Асинхронная валидация ключа при старте — чтобы не вешать UI."""

    result = Signal(bool, str)

    def __init__(self, key: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.key = key

    def run(self) -> None:
        try:
            ok, err = validate_api_key(self.key)
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"Ошибка проверки: {exc}"
        self.result.emit(ok, err)


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------


class ConfigManager:
    """JSON-хранилище для ключа и прочих настроек."""

    def __init__(self) -> None:
        self.path = get_data_dir() / CONFIG_FILENAME
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self._data, dict):
                    self._data = {}
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    def get_api_key(self) -> Optional[str]:
        key = self._data.get("api_key")
        if isinstance(key, str) and key.strip():
            return key.strip()
        return None

    def set_api_key(self, key: str) -> None:
        self._data["api_key"] = key.strip()
        self._save()

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Не удалось сохранить config.json: %s", exc
            )


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------


@dataclass
class ModFile:
    project_id: int
    file_id: int
    required: bool = True
    file_name: Optional[str] = None
    download_url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ArchivePlan:
    zip_path: Path
    has_overrides: bool
    root_names: list[str] = field(default_factory=list)
    overrides_prefix: str = "overrides/"
    case: str = "B"  # "A" | "B" | "C"


# ---------------------------------------------------------------------------
# Сигналы воркера
# ---------------------------------------------------------------------------


class WorkerSignals(QObject):
    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(bool, str)
    api_key_invalid = Signal(str)
    open_folder_requested = Signal(str)


# ---------------------------------------------------------------------------
# Воркер
# ---------------------------------------------------------------------------


class ModpackWorker(QThread):
    """Фоновый поток: валидация ключа, анализ ZIP, распаковка, загрузка модов."""

    def __init__(
        self,
        zip_path: Path,
        output_dir: Path,
        threads: int,
        skip_optional: bool,
        api_key: Optional[str],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.zip_path = zip_path
        self.output_dir = output_dir
        self.threads = max(1, min(threads, 32))
        self.skip_optional = skip_optional
        self.api_key = api_key.strip() if api_key else None
        self.signals = WorkerSignals()
        self._cancel_event = threading.Event()
        self._logger = logging.getLogger("ModpackWorker")

        self._total_tasks = 0
        self._completed_tasks = 0
        self._last_progress_time = 0.0

    def cancel(self) -> None:
        self._cancel_event.set()
        self.signals.log.emit("⏹ Запрошена отмена. Завершаю текущие задачи...")

    def run(self) -> None:
        try:
            self._run_impl()
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Непредвиденная ошибка в воркере")
            self.signals.log.emit(f"❌ Непредвиденная ошибка: {exc}")
            self.signals.finished.emit(False, str(exc))

    def _run_impl(self) -> None:
        if self.api_key:
            self.signals.log.emit("🔑 Проверяю API-ключ...")
            ok, err = validate_api_key(self.api_key)
            if not ok:
                self.signals.log.emit(f"❌ {err}")
                self.signals.api_key_invalid.emit(err)
                self.signals.finished.emit(
                    False, f"API-ключ недействителен: {err}"
                )
                return
            self.signals.log.emit("✅ API-ключ действителен.")

        start_time = time.monotonic()
        self.signals.log.emit(f"📦 Архив: {self.zip_path}")
        self.signals.log.emit(f"📁 Выходная папка: {self.output_dir}")

        manifest = self._read_manifest()
        if manifest is None:
            return

        plan = self._analyze_archive()
        if plan is None:
            return
        self._log_plan(plan)
        if not self._extract_archive(plan):
            return

        if self._cancel_event.is_set():
            self._finish_cancelled()
            return

        mod_files = self._parse_mod_files(manifest)
        if not mod_files:
            self.signals.log.emit("ℹ️ В manifest.files[] нет модов для загрузки.")
            self._finalize(start_time, [])
            return

        failed = self._download_mods(mod_files)
        self._finalize(start_time, failed)

    def _read_manifest(self) -> Optional[dict[str, Any]]:
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                try:
                    raw = zf.read("manifest.json")
                except KeyError:
                    self._error("manifest.json не найден в корне архива.")
                    return None
        except zipfile.BadZipFile:
            self._error("Файл не является корректным ZIP-архивом.")
            return None
        except OSError as exc:
            self._error(f"Не удалось открыть ZIP: {exc}")
            return None

        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._error(f"manifest.json содержит некорректный JSON: {exc}")
            return None

        if not isinstance(manifest, dict):
            self._error("manifest.json имеет неожиданную структуру.")
            return None

        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            (self.output_dir / "manifest.json").write_bytes(raw)
        except OSError as exc:
            self._logger.warning(
                "Не удалось сохранить копию manifest.json: %s", exc
            )

        return manifest

    def _analyze_archive(self) -> Optional[ArchivePlan]:
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
        except (zipfile.BadZipFile, OSError) as exc:
            self._error(f"Не удалось прочитать список файлов ZIP: {exc}")
            return None

        if not names:
            self._error("ZIP-архив пуст.")
            return None

        has_overrides = any(
            n == "overrides/" or n.startswith("overrides/") for n in names
        )

        root_names: set[str] = set()
        for name in names:
            first = name.split("/", 1)[0]
            if first in EXCLUDED_ROOT_NAMES or first == "overrides":
                continue
            root_names.add(first)

        plan = ArchivePlan(
            zip_path=self.zip_path,
            has_overrides=has_overrides,
            root_names=sorted(root_names),
        )

        if has_overrides and not root_names:
            plan.case = "A"
        elif has_overrides and root_names:
            plan.case = "C"
        else:
            plan.case = "B"

        return plan

    def _log_plan(self, plan: ArchivePlan) -> None:
        if plan.case == "A":
            self.signals.log.emit(
                "🔍 Найден overrides/, разворачиваю только его содержимое."
            )
        elif plan.case == "B":
            self.signals.log.emit(
                "🔍 overrides/ не найден, разворачиваю архив как есть."
            )
        else:
            self.signals.log.emit(
                "🔍 Найден overrides/ и корневые папки. "
                "Копирую корень, затем overrides/ с приоритетом."
            )

    def _count_extract_files(self, plan: ArchivePlan) -> int:
        """Считает файлы, которые реально будут извлечены (без manifest/modlist)."""
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                n = 0
                prefix_len = len(plan.overrides_prefix)
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    if plan.case == "A":
                        if name.startswith(plan.overrides_prefix) and len(name) > prefix_len:
                            n += 1
                    else:
                        first = name.split("/", 1)[0]
                        if first in EXCLUDED_ROOT_NAMES:
                            continue
                        n += 1
                return n
        except (zipfile.BadZipFile, OSError):
            return 0

    def _extract_archive(self, plan: ArchivePlan) -> bool:
        try:
            total = self._count_extract_files(plan)
            if plan.case == "A":
                self.signals.log.emit(
                    f"📂 Разворачиваю {total} файлов из overrides/ "
                    f"(конфиги, скрипты, квесты, ресурсы)..."
                )
            else:
                self.signals.log.emit(
                    f"📂 Разворачиваю {total} файлов архива..."
                )

            with zipfile.ZipFile(self.zip_path, "r") as zf:
                if plan.case in ("B", "C"):
                    self._extract_root(zf)
                    if self._cancel_event.is_set():
                        return False
                if plan.case in ("A", "C"):
                    self._extract_overrides(zf, plan)
                    if self._cancel_event.is_set():
                        return False

        except (zipfile.BadZipFile, OSError) as exc:
            self._error(f"Ошибка при распаковке ZIP: {exc}")
            return False

        self.signals.log.emit("✅ Содержимое архива развёрнуто.")
        return True

    def _extract_root(self, zf: zipfile.ZipFile) -> None:
        for name in zf.namelist():
            if self._cancel_event.is_set():
                return
            if name.endswith("/"):
                continue
            first = name.split("/", 1)[0]
            if first in EXCLUDED_ROOT_NAMES or first == "overrides":
                continue
            self._copy_member(zf, name, name)

    def _extract_overrides(self, zf: zipfile.ZipFile, plan: ArchivePlan) -> None:
        prefix = plan.overrides_prefix
        for name in zf.namelist():
            if self._cancel_event.is_set():
                return
            if name.endswith("/") or not name.startswith(prefix):
                continue
            relative = name[len(prefix):]
            if not relative:
                continue
            self._copy_member(zf, name, relative)

    def _copy_member(
        self, zf: zipfile.ZipFile, member_name: str, relative_dest: str
    ) -> None:
        dest = (self.output_dir / relative_dest).resolve()
        try:
            dest.relative_to(self.output_dir.resolve())
        except ValueError:
            self.signals.log.emit(f"⚠️ Пропущен подозрительный путь: {member_name}")
            return

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member_name, "r") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
        except OSError as exc:
            self._logger.warning("Не удалось скопировать %s: %s", member_name, exc)
            self.signals.log.emit(f"⚠️ Не удалось скопировать {member_name}: {exc}")

    def _parse_mod_files(self, manifest: dict[str, Any]) -> list[ModFile]:
        files = manifest.get("files")
        if not isinstance(files, list):
            self.signals.log.emit(
                "⚠️ В manifest.json отсутствует или некорректен files[]."
            )
            return []

        result: list[ModFile] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item["projectID"])
                fid = int(item["fileID"])
                required = bool(item.get("required", True))
            except (KeyError, TypeError, ValueError):
                self.signals.log.emit(f"⚠️ Пропущена некорректная запись: {item!r}")
                continue

            if self.skip_optional and not required:
                self._logger.debug("Пропущен опциональный мод %s/%s", pid, fid)
                continue

            result.append(ModFile(project_id=pid, file_id=fid, required=required))

        return result

    def _download_mods(self, mod_files: list[ModFile]) -> list[ModFile]:
        mods_dir = self.output_dir / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)

        self._total_tasks = len(mod_files)
        self._completed_tasks = 0
        self._last_progress_time = 0.0
        self._emit_progress(0, "Подготовка к загрузке модов...")

        failed: list[ModFile] = []

        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            future_map = {
                executor.submit(self._download_one, mf, mods_dir): mf
                for mf in mod_files
            }

            for future in as_completed(future_map):
                mf = future_map[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    mf.error = str(exc)
                    self._logger.exception(
                        "Ошибка загрузки %s/%s", mf.project_id, mf.file_id
                    )
                    self.signals.log.emit(
                        f"❌ {mf.project_id}/{mf.file_id}: {exc}"
                    )

                if mf.error:
                    failed.append(mf)

                self._completed_tasks += 1
                self._emit_progress(
                    self._completed_tasks,
                    f"Моды: {self._completed_tasks}/{self._total_tasks}",
                )

                if self._cancel_event.is_set():
                    for f in future_map:
                        f.cancel()
                    break

        return failed

    def _download_one(self, mf: ModFile, mods_dir: Path) -> None:
        if self._cancel_event.is_set():
            mf.error = "Отменено пользователем"
            return

        if self.api_key:
            ok = self._resolve_via_api(mf)
            if not ok:
                return

        if not mf.download_url:
            mf.download_url = (
                f"{CF_DOWNLOAD_BASE}/mods/{mf.project_id}/files/{mf.file_id}/download"
            )

        if mf.file_name:
            existing = mods_dir / mf.file_name
            if existing.is_file() and existing.stat().st_size > 0:
                self._logger.debug("Файл уже существует: %s", existing)
                return

        last_exc: Optional[Exception] = None
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            if self._cancel_event.is_set():
                mf.error = "Отменено пользователем"
                return
            try:
                self._download_with_requests(mf, mods_dir)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self._logger.warning(
                    "Попытка %d для %s/%s провалена: %s",
                    attempt, mf.project_id, mf.file_id, exc,
                )
                if attempt <= len(RETRY_DELAYS):
                    if self._cancel_event.wait(timeout=RETRY_DELAYS[attempt - 1]):
                        mf.error = "Отменено пользователем"
                        return

        mf.error = str(last_exc) if last_exc else "Неизвестная ошибка"

    def _resolve_via_api(self, mf: ModFile) -> bool:
        url = f"{CF_API_BASE}/mods/{mf.project_id}/files/{mf.file_id}"
        headers = {**DEFAULT_HEADERS, "x-api-key": self.api_key or ""}

        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            self._logger.warning(
                "API error для %s/%s: %s, fallback на прямой URL",
                mf.project_id, mf.file_id, exc,
            )
            mf.download_url = (
                f"{CF_DOWNLOAD_BASE}/mods/{mf.project_id}/files/{mf.file_id}/download"
            )
            return True

        if resp.status_code == 429:
            mf.error = "API rate limit (429). Подожди."
            return False
        if resp.status_code in (401, 403):
            mf.error = f"API отклонил ключ: HTTP {resp.status_code}"
            return False
        if resp.status_code != 200:
            self._logger.warning(
                "API вернул HTTP %d для %s/%s, fallback на прямой URL",
                resp.status_code, mf.project_id, mf.file_id,
            )
            mf.download_url = (
                f"{CF_DOWNLOAD_BASE}/mods/{mf.project_id}/files/{mf.file_id}/download"
            )
            return True

        try:
            data = resp.json().get("data") or {}
        except ValueError:
            mf.error = "API вернул некорректный JSON."
            return False

        if not isinstance(data, dict):
            mf.error = "API вернул неожиданную структуру data."
            return False

        file_name = data.get("fileName")
        if isinstance(file_name, str) and file_name.strip():
            mf.file_name = safe_filename(file_name)

        download_url = data.get("downloadUrl")
        if isinstance(download_url, str) and download_url.strip():
            mf.download_url = download_url.strip()
            return True

        mf.error = (
            "downloadUrl отсутствует (автор запретил стороннюю загрузку). "
            f"Скачать вручную: {CF_PROJECT_URL}/{mf.project_id}/files/{mf.file_id}"
        )
        return False

    def _download_with_requests(self, mf: ModFile, mods_dir: Path) -> None:
        headers = dict(DEFAULT_HEADERS)
        if self.api_key:
            headers["x-api-key"] = self.api_key

        with requests.get(
            mf.download_url,
            headers=headers,
            allow_redirects=True,
            stream=True,
            timeout=(30, 120),
        ) as resp:
            if resp.status_code in (403, 404, 429):
                raise RuntimeError(f"HTTP {resp.status_code}")
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise RuntimeError(
                    "Сервер вернул HTML (капча или 403). "
                    "Нужен API-ключ или ручная загрузка."
                )

            file_name = mf.file_name
            if not file_name:
                file_name = self._filename_from_response(resp)
            if not file_name:
                raise RuntimeError("Не удалось определить имя файла.")
            mf.file_name = file_name

            dest = mods_dir / file_name
            part = dest.with_suffix(dest.suffix + ".part")

            content_length = self._int_or_none(resp.headers.get("Content-Length"))
            if (
                dest.is_file()
                and content_length is not None
                and content_length > 0
                and dest.stat().st_size == content_length
            ):
                self._logger.debug("Файл уже скачан: %s", dest)
                return

            part.parent.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if self._cancel_event.is_set():
                        raise RuntimeError("Отменено пользователем")
                    if chunk:
                        f.write(chunk)

            os.replace(part, dest)
            size = dest.stat().st_size
            # В файл — debug, в UI — через сигнал с галочкой. Не дублируем.
            self._logger.debug(
                "Файл скачан: %s (%s)", file_name, format_size(size)
            )
            self.signals.log.emit(f"✔ {file_name} ({format_size(size)})")

    @staticmethod
    def _filename_from_response(resp: requests.Response) -> Optional[str]:
        cd = resp.headers.get("Content-Disposition", "")
        if cd:
            match = re.search(
                r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I
            )
            if match:
                return safe_filename(unquote(match.group(1)))

        path = urlparse(resp.url).path
        base = Path(unquote(path)).name
        if base and "." in base:
            return safe_filename(base)

        return None

    @staticmethod
    def _int_or_none(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _emit_progress(self, current: int, status: str) -> None:
        now = time.monotonic() * 1000
        if (
            now - self._last_progress_time < PROGRESS_THROTTLE_MS
            and current < self._total_tasks
        ):
            return
        self._last_progress_time = now
        self.signals.progress.emit(current, self._total_tasks, status)

    def _finalize(self, start_time: float, failed: list[ModFile]) -> None:
        elapsed = time.monotonic() - start_time
        mods_dir = self.output_dir / "mods"
        downloaded = 0
        if mods_dir.exists():
            downloaded = len(
                [
                    p
                    for p in mods_dir.iterdir()
                    if p.is_file() and not p.name.endswith(".part")
                ]
            )

        if failed:
            self.signals.log.emit("")
            self.signals.log.emit("❌ Проваленные моды:")
            for mf in failed:
                manual = (
                    f"{CF_PROJECT_URL}/{mf.project_id}/files/{mf.file_id}"
                )
                self.signals.log.emit(
                    f"   • projectID={mf.project_id}, fileID={mf.file_id} — "
                    f"{mf.error or 'неизвестная ошибка'}"
                )
                self.signals.log.emit(f"     Ручная ссылка: {manual}")
            self.signals.finished.emit(
                False, f"Завершено с ошибками: {len(failed)} модов не скачано."
            )
        else:
            self.signals.log.emit("")
            self.signals.log.emit(
                f"✅ Собрано {downloaded} модов в {mods_dir}, "
                f"папка готова к использованию. ({elapsed:.1f} с)"
            )
            self.signals.finished.emit(True, "Готово")

        self.signals.open_folder_requested.emit(str(self.output_dir))

    def _finish_cancelled(self) -> None:
        self.signals.log.emit(
            "⏹ Работа отменена. Уже скачанные файлы сохранены."
        )
        self.signals.finished.emit(False, "Отменено")

    def _error(self, message: str) -> None:
        self._logger.error(message)
        self.signals.log.emit(f"❌ {message}")
        self.signals.finished.emit(False, message)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """Главное окно утилиты."""

    _log_signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(QSize(720, 640))
        self.setAcceptDrops(True)

        self._log_signal.connect(self._append_log)

        self._worker: Optional[ModpackWorker] = None
        self._validator: Optional[KeyValidator] = None
        self.config = ConfigManager()
        self._last_auto_output = ""

        self._build_ui()
        self._setup_logging()
        self._load_saved_key()

        # Проверка ключа после отрисовки окна — чтобы диалог не появился
        # раньше, чем окно успело показаться пользователю.
        QTimer.singleShot(400, self._check_api_key_startup)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ZIP
        zip_row = QHBoxLayout()
        zip_row.addWidget(QLabel("ZIP модпака:"))
        self.zip_edit = QLineEdit()
        self.zip_edit.setPlaceholderText(
            "Перетащите ZIP сюда или нажмите «Обзор»"
        )
        self.zip_edit.textChanged.connect(self._on_zip_changed)
        zip_row.addWidget(self.zip_edit, 1)
        btn_zip = QPushButton("Обзор...")
        btn_zip.clicked.connect(self._choose_zip)
        zip_row.addWidget(btn_zip)
        root.addLayout(zip_row)

        # Выход
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Куда распаковать:"))
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText(
            "Заполняется автоматически при выборе ZIP"
        )
        out_row.addWidget(self.out_edit, 1)
        btn_out = QPushButton("Обзор...")
        btn_out.clicked.connect(self._choose_output)
        out_row.addWidget(btn_out)
        root.addLayout(out_row)

        # API-ключ
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API-ключ:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            "$2a$10$... (обязателен, сохраняется автоматически)"
        )
        self.api_key_edit.editingFinished.connect(self._save_key_from_edit)
        key_row.addWidget(self.api_key_edit, 1)

        link_label = QLabel(f'<a href="{CF_CONSOLE_URL}">Получить ключ</a>')
        link_label.setOpenExternalLinks(True)
        key_row.addWidget(link_label)
        root.addLayout(key_row)

        # Параметры
        params = QHBoxLayout()
        params.addWidget(QLabel("Потоков:"))
        self.thread_slider = QSlider(Qt.Orientation.Horizontal)
        self.thread_slider.setRange(1, 32)
        self.thread_slider.setValue(8)
        self.thread_slider.setTickInterval(1)
        self.thread_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.thread_slider.setFixedWidth(160)
        params.addWidget(self.thread_slider)
        self.thread_label = QLabel("8")
        self.thread_label.setFixedWidth(24)
        self.thread_slider.valueChanged.connect(
            lambda v: self.thread_label.setText(str(v))
        )
        params.addWidget(self.thread_label)
        params.addSpacing(16)
        self.skip_optional_cb = QCheckBox("Пропускать опциональные моды")
        params.addWidget(self.skip_optional_cb)
        params.addStretch()
        root.addLayout(params)

        # Кнопки
        btn_row = QHBoxLayout()
        self.btn_go = QPushButton("Поехали")
        self.btn_go.setMinimumHeight(40)
        self.btn_go.clicked.connect(self._start)
        btn_row.addWidget(self.btn_go, 1)

        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self.btn_cancel)
        root.addLayout(btn_row)

        # Прогресс
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.status_label = QLabel("Ожидание...")
        root.addWidget(self.status_label)

        # Лог
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setPlaceholderText("Лог выполнения...")
        root.addWidget(self.log_view, 1)

        # Открыть папку
        self.btn_open = QPushButton("Открыть папку")
        self.btn_open.setVisible(False)
        self.btn_open.clicked.connect(self._open_output_folder)
        root.addWidget(self.btn_open)

    # ------------------------------------------------------------------
    # Проверка ключа при старте
    # ------------------------------------------------------------------

    def _check_api_key_startup(self) -> None:
        key = self.config.get_api_key()
        if not key:
            if not self.config.get("dont_remind_key", False):
                self._show_get_key_dialog()
            return

        # Ключ есть — валидируем в фоне, UI не блокируем
        self._validator = KeyValidator(key, parent=self)
        self._validator.result.connect(self._on_startup_validation)
        self._validator.start()

    @Slot(bool, str)
    def _on_startup_validation(self, ok: bool, err: str) -> None:
        if ok:
            self._append_log("✅ Сохранённый API-ключ действителен.")
            return

        self._append_log(f"⚠️ Сохранённый API-ключ недействителен: {err}")
        if not self.config.get("dont_remind_key", False):
            self._show_key_invalid_dialog(err)

    def _show_get_key_dialog(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Нужен API-ключ CurseForge")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "Для стабильной загрузки модов нужен API-ключ CurseForge.<br><br>"
            "<b>Получить бесплатно за минуту:</b><br>"
            "1. Нажмите «Открыть консоль»<br>"
            "2. Войдите через аккаунт CurseForge<br>"
            "3. Создайте ключ (название любое)<br>"
            "4. Скопируйте ключ и вставьте в поле «API-ключ»<br><br>"
            "Ключ действует 90 дней, потом нужно получить новый."
        )
        open_btn = box.addButton(
            "Открыть консоль", QMessageBox.ButtonRole.ActionRole
        )
        box.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        dont_remind_cb = QCheckBox("Больше не напоминать при запуске")
        box.setCheckBox(dont_remind_cb)
        box.exec()

        if dont_remind_cb.isChecked():
            self.config.set("dont_remind_key", True)
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(CF_CONSOLE_URL))

    def _show_key_invalid_dialog(self, error_msg: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("API-ключ недействителен")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"{error_msg}<br><br>"
            "Получите новый ключ в консоли CurseForge и вставьте его "
            "в поле «API-ключ», затем нажмите «Поехали»."
        )
        open_btn = box.addButton(
            "Открыть консоль", QMessageBox.ButtonRole.ActionRole
        )
        box.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        dont_remind_cb = QCheckBox("Больше не напоминать при запуске")
        box.setCheckBox(dont_remind_cb)
        box.exec()

        if dont_remind_cb.isChecked():
            self.config.set("dont_remind_key", True)
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(CF_CONSOLE_URL))

    # ------------------------------------------------------------------
    # Авто-подстановка выходной папки
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_zip_changed(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        suggested = str(suggest_output_for_zip(Path(text)))
        current = self.out_edit.text().strip()
        if not current or current == self._last_auto_output:
            self.out_edit.setText(suggested)
            self._last_auto_output = suggested

    # ------------------------------------------------------------------
    # Ключ
    # ------------------------------------------------------------------

    def _load_saved_key(self) -> None:
        key = self.config.get_api_key()
        if key:
            self.api_key_edit.setText(key)
            self._append_log("🔑 Загружен сохранённый API-ключ.")

    def _save_key_from_edit(self) -> None:
        key = self.api_key_edit.text().strip()
        if key and key != self.config.get_api_key():
            self.config.set_api_key(key)
            logging.getLogger(__name__).debug("API-ключ сохранён в config.json")

    # ------------------------------------------------------------------
    # Логирование
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        log_path = get_data_dir() / LOG_FILENAME

        root_logger = logging.getLogger()
        if any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", "") == str(log_path)
            for h in root_logger.handlers
        ):
            return

        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root_logger.addHandler(file_handler)
        if root_logger.level > logging.DEBUG:
            root_logger.setLevel(logging.DEBUG)

        emit = self._log_signal.emit

        class _UiLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    emit(self.format(record))
                except (RuntimeError, ValueError):
                    return

        ui_handler = _UiLogHandler()
        ui_handler.setLevel(logging.INFO)
        ui_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(ui_handler)

    # ------------------------------------------------------------------
    # Слоты
    # ------------------------------------------------------------------

    @Slot()
    def _choose_zip(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите ZIP модпака", "", "ZIP-архивы (*.zip);;Все файлы (*)"
        )
        if path:
            self.zip_edit.setText(path)

    @Slot()
    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Выберите папку для распаковки"
        )
        if path:
            self.out_edit.setText(path)
            self._last_auto_output = ""

    @Slot()
    def _start(self) -> None:
        zip_path = Path(self.zip_edit.text().strip())
        out_text = self.out_edit.text().strip()

        if not zip_path.is_file():
            self._append_log("❌ Выберите существующий ZIP-файл.")
            return

        if not out_text:
            out_path = suggest_output_for_zip(zip_path)
        else:
            out_path = Path(out_text)

        out_path = unique_pack_path(out_path)
        self.out_edit.setText(str(out_path))

        if out_path.exists() and not out_path.is_dir():
            self._append_log("❌ Выходной путь существует и не является папкой.")
            return

        api_key = self.api_key_edit.text().strip() or None
        if api_key:
            self._save_key_from_edit()
        else:
            self._append_log(
                "⚠️ API-ключ не задан. Возможны проблемы с загрузкой модов."
            )

        out_path.mkdir(parents=True, exist_ok=True)

        self._set_running(True)
        self.log_view.clear()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status_label.setText("Запуск...")
        self.btn_open.setVisible(False)

        self._worker = ModpackWorker(
            zip_path=zip_path,
            output_dir=out_path,
            threads=self.thread_slider.value(),
            skip_optional=self.skip_optional_cb.isChecked(),
            api_key=api_key,
        )
        self._worker.signals.log.connect(self._append_log)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.signals.api_key_invalid.connect(self._on_api_key_invalid)
        self._worker.signals.open_folder_requested.connect(
            self._on_open_folder_requested
        )
        self._worker.start()

    @Slot(str)
    def _on_api_key_invalid(self, error_msg: str) -> None:
        """Ошибка ключа во время работы (не при старте)."""
        self._show_key_invalid_dialog(error_msg)

    @Slot()
    def _cancel(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    @Slot(int, int, str)
    def _on_progress(self, current: int, total: int, status: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        self.status_label.setText(status)

    @Slot(bool, str)
    def _on_finished(self, success: bool, message: str) -> None:
        self._set_running(False)
        self.status_label.setText("Готово" if success else "Ошибка / Отмена")
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if success else 0)
        self._append_log(f"— {message} —")

    @Slot(str)
    def _on_open_folder_requested(self, path: str) -> None:
        self.btn_open.setVisible(True)
        self.btn_open.setProperty("folder_path", path)

    @Slot()
    def _open_output_folder(self) -> None:
        path = self.btn_open.property("folder_path") or self.out_edit.text()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _set_running(self, running: bool) -> None:
        self.btn_go.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.zip_edit.setEnabled(not running)
        self.out_edit.setEnabled(not running)
        self.api_key_edit.setEnabled(not running)
        self.thread_slider.setEnabled(not running)
        self.skip_optional_cb.setEnabled(not running)

    # ------------------------------------------------------------------
    # Drag & Drop + корректное закрытие
    # ------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path.lower().endswith(".zip"):
                    self.zip_edit.setText(path)
                    break

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            if not self._worker.wait(5000):
                self._worker.terminate()
                self._worker.wait(1000)
        event.accept()


# ---------------------------------------------------------------------------
# Точка входа для лаунчера
# ---------------------------------------------------------------------------


def main(app: QApplication) -> int:
    window = MainWindow()
    window.show()
    return app.exec()