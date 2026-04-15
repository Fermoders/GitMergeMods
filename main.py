#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitMergeMods v1.0
Легковесное приложение для синхронизации файлов модов с GitHub.
Работает через GitHub REST API — не требует установки Git.
Все функции в одном файле. Собирается в один .exe через PyInstaller.
"""

import os
import sys
import json
import hashlib
import base64
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime
import difflib
import re

# ============================================================
# Константы
# ============================================================

APP_NAME = "GitMergeMods"
APP_VERSION = "1.0"
SCAN_INTERVAL_SEC = 5
API_BASE = "https://api.github.com"

# Определяем папку приложения (рядом с .exe или .py)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "gitmergemods_config.json")


# ============================================================
# Утилиты
# ============================================================

def compute_git_blob_hash(data: bytes) -> str:
    """Вычисляет SHA-1 хеш blob-объекта Git (для сравнения с GitHub tree)."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def normalize_path(path: str) -> str:
    """Нормализует путь: \\ → /, нижний регистр для сравнения."""
    return path.replace("\\", "/")


def is_binary(data: bytes) -> bool:
    """Определяет, являются ли данные бинарными (простая эвристика)."""
    if b"\x00" in data[:8192]:
        return True
    return False


def get_app_dir():
    return APP_DIR


# ============================================================
# Конфигурация (сохраняется рядом с .exe)
# ============================================================

class Config:
    """Загрузка/сохранение настроек в JSON."""

    DEFAULTS = {
        "token": "",
        "repo_url": "",
        "local_folder": "",
        "branch": "main",
        "auto_push": False,
        "filter_extensions": False,
        "extensions": [".xml", ".txt"],
        "conflict_resolution": "remote",  # "remote" | "local"
    }

    def __init__(self):
        for k, v in self.DEFAULTS.items():
            setattr(self, k, v if not isinstance(v, list) else list(v))
        self.load()

    def load(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(self, k):
                        setattr(self, k, v)
        except Exception:
            pass

    def save(self):
        try:
            data = {k: getattr(self, k) for k in self.DEFAULTS}
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


# ============================================================
# GitHub REST API клиент
# ============================================================

class GitHubClient:
    """Полноценный клиент GitHub API v3. Не требует установки Git."""

    def __init__(self, token: str, repo_url: str, branch: str = "main"):
        self.token = token
        self.branch = branch
        self.owner, self.repo = self._parse_repo_url(repo_url)
        self._cached_tree = None
        self._cached_commit_sha = None

    # ---- Парсинг URL ----

    @staticmethod
    def _parse_repo_url(url: str):
        """Извлекает owner и repo из URL репозитория."""
        url = url.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        match = re.match(
            r"(?:https?://github\.com/|git@github\.com:)([^/]+)/(.+)", url
        )
        if match:
            return match.group(1), match.group(2)
        raise ValueError(f"Не удалось распознать URL: {url}")

    # ---- HTTP запросы ----

    def _api_request(self, method: str, endpoint: str, data=None, params=None):
        url = f"{API_BASE}{endpoint}"
        if params:
            url += "&" if "?" in url else "?"
            url += "&".join(f"{k}={v}" for k, v in params.items())

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }

        body = json.dumps(data).encode("utf-8") if data else None
        req = Request(url, data=body, headers=headers, method=method)
        if body:
            req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=30) as resp:
                if resp.status in (200, 201):
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw else None
                if resp.status == 204:
                    return None
                return None
        except HTTPError as e:
            if e.code in (404, 409):
                return None
            error_body = e.read().decode("utf-8", errors="replace")
            raise Exception(f"GitHub API ошибка {e.code}: {error_body[:300]}")
        except URLError as e:
            raise Exception(f"Сетевая ошибка: {e.reason}")
        except Exception as e:
            raise Exception(f"Ошибка запроса: {e}")

    # ---- Основные операции ----

    def test_connection(self):
        """Проверяет доступность репозитория. Возвращает инфо о репо или None."""
        return self._api_request("GET", f"/repos/{self.owner}/{self.repo}")

    def get_default_branch(self):
        """Получает имя ветки по умолчанию."""
        info = self.test_connection()
        if info:
            return info.get("default_branch", "main")
        return "main"

    def get_latest_commit_sha(self):
        data = self._api_request(
            "GET", f"/repos/{self.owner}/{self.repo}/commits/{self.branch}"
        )
        if data and "sha" in data:
            return data["sha"]
        return None

    def get_tree(self):
        """
        Получает полное дерево файлов репозитория.
        Возвращает (dict: path → {sha, size}, commit_sha).
        """
        commit_sha = self.get_latest_commit_sha()
        if not commit_sha:
            return {}, None

        if commit_sha == self._cached_commit_sha and self._cached_tree is not None:
            return self._cached_tree, commit_sha

        data = self._api_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/git/trees/{commit_sha}",
            params={"recursive": "1"},
        )

        tree = {}
        if data and "tree" in data:
            for item in data["tree"]:
                if item["type"] == "blob":
                    tree[normalize_path(item["path"])] = {
                        "sha": item["sha"],
                        "size": item.get("size", 0),
                    }

        self._cached_tree = tree
        self._cached_commit_sha = commit_sha
        return tree, commit_sha

    def get_file_content(self, path: str):
        """
        Получает содержимое файла из репозитория.
        Возвращает (bytes, blob_sha) или (None, None).
        """
        # Пробуем через Contents API (до 1 МБ)
        data = self._api_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{path}",
            params={"ref": self.branch},
        )
        if data and "content" in data:
            raw = base64.b64decode(data["content"].replace("\n", ""))
            return raw, data.get("sha")

        # Файл слишком большой — через Blob API
        tree, _ = self.get_tree()
        if path in tree:
            blob_sha = tree[path]["sha"]
            blob = self._api_request(
                "GET", f"/repos/{self.owner}/{self.repo}/git/blobs/{blob_sha}"
            )
            if blob and "content" in blob:
                raw = base64.b64decode(blob["content"].replace("\n", ""))
                return raw, blob_sha
        return None, None

    def upload_file(self, path: str, content: bytes, existing_sha: str = None):
        """Создаёт или обновляет файл в репозитории."""
        content_b64 = base64.b64encode(content).decode("ascii")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"Auto-sync {path} [{ts}]"

        payload = {
            "message": message,
            "content": content_b64,
            "branch": self.branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        result = self._api_request(
            "PUT",
            f"/repos/{self.owner}/{self.repo}/contents/{path}",
            data=payload,
        )
        # Сбрасываем кэш
        self._cached_commit_sha = None
        self._cached_tree = None
        return result

    def delete_file(self, path: str, sha: str):
        """Удаляет файл из репозитория."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "message": f"Auto-sync delete {path} [{ts}]",
            "sha": sha,
            "branch": self.branch,
        }
        result = self._api_request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/contents/{path}",
            data=payload,
        )
        self._cached_commit_sha = None
        self._cached_tree = None
        return result

    def invalidate_cache(self):
        self._cached_commit_sha = None
        self._cached_tree = None


# ============================================================
# Сканер локальных файлов
# ============================================================

class FileScanner:
    """Сканирует локальную папку и сравнивает с удалённым деревом."""

    def __init__(self, local_folder: str, extensions_filter=None):
        self.local_folder = local_folder
        self.extensions_filter = extensions_filter  # None = все файлы

    def scan_local(self):
        """
        Сканирует локальную папку.
        Возвращает dict: normalize_path → {hash, full_path, size}.
        """
        result = {}
        if not os.path.isdir(self.local_folder):
            return result

        for root, dirs, files in os.walk(self.local_folder):
            # Пропускаем скрытые и системные папки
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".")
                and d.lower() not in ("__pycache__", "node_modules")
            ]
            for fname in files:
                if fname.startswith("."):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, self.local_folder)
                norm_path = normalize_path(rel_path)

                if self.extensions_filter:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in self.extensions_filter:
                        continue

                try:
                    with open(full_path, "rb") as f:
                        content = f.read()
                    blob_hash = compute_git_blob_hash(content)
                    result[norm_path] = {
                        "hash": blob_hash,
                        "full_path": full_path,
                        "size": len(content),
                    }
                except (IOError, OSError):
                    pass
        return result

    @staticmethod
    def compare(local_files, remote_tree):
        """
        Сравнивает локальные и удалённые файлы.
        Возвращает список изменений:
        [
            {
                "path": str,
                "status": "local_only" | "remote_only" | "changed" | "synced",
                "local_hash": str | None,
                "remote_sha": str | None,
                "full_path": str,
            },
            ...
        ]
        """
        all_paths = set(local_files.keys()) | set(remote_tree.keys())
        changes = []

        for path in sorted(all_paths):
            local = local_files.get(path)
            remote = remote_tree.get(path)

            if local and not remote:
                changes.append(
                    {
                        "path": path,
                        "status": "local_only",
                        "local_hash": local["hash"],
                        "remote_sha": None,
                        "full_path": local["full_path"],
                    }
                )
            elif remote and not local:
                changes.append(
                    {
                        "path": path,
                        "status": "remote_only",
                        "local_hash": None,
                        "remote_sha": remote["sha"],
                        "full_path": path,
                    }
                )
            elif local["hash"] != remote["sha"]:
                changes.append(
                    {
                        "path": path,
                        "status": "changed",
                        "local_hash": local["hash"],
                        "remote_sha": remote["sha"],
                        "full_path": local["full_path"],
                    }
                )
            # synced — пропускаем

        return changes


# ============================================================
# Окно просмотра Diff (Toplevel)
# ============================================================

class DiffWindow(tk.Toplevel):
    """Окно с side-by-side сравнением локальной и удалённой версий файла."""

    TAG_ADDED = "added"
    TAG_REMOVED = "removed"
    TAG_HEADER = "header"

    def __init__(self, parent, file_path, local_content, remote_content,
                 local_status, on_apply_callback):
        super().__init__(parent)
        self.title(f"Различия: {os.path.basename(file_path)}")
        self.geometry("900x550")
        self.minsize(600, 400)
        self.transient(parent)
        self.grab_set()

        self.on_apply = on_apply_callback
        self.file_path = file_path
        self.local_content = local_content
        self.remote_content = remote_content

        # === Информационная панель ===
        info_frame = ttk.Frame(self)
        info_frame.pack(fill="x", padx=8, pady=(8, 4))

        status_text = {
            "local_only": "📄 Только локально (нет в репозитории)",
            "remote_only": "☁️ Только в репозитории (нет локально)",
            "changed": "⚡ Изменён с обеих сторон",
        }.get(local_status, local_status)

        ttk.Label(
            info_frame, text=f"📁 {file_path}", font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")
        ttk.Label(info_frame, text=status_text, foreground="#555").pack(anchor="w")

        # === Side-by-side Diff ===
        diff_container = ttk.PanedWindow(self, orient="horizontal")
        diff_container.pack(fill="both", expand=True, padx=8, pady=4)

        # Левая панель — локальная версия
        left_frame = ttk.LabelFrame(diff_container, text="Локальная версия")
        diff_container.add(left_frame, weight=1)

        self.local_text = tk.Text(
            left_frame, wrap="none", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            padx=4, pady=4,
        )
        left_scroll_y = ttk.Scrollbar(left_frame, orient="vertical",
                                       command=self.local_text.yview)
        left_scroll_x = ttk.Scrollbar(left_frame, orient="horizontal",
                                       command=self.local_text.xview)
        self.local_text.configure(
            yscrollcommand=self._make_yscroll(self.local_text, "left"),
            xscrollcommand=left_scroll_x.set,
        )
        left_scroll_y.pack(side="right", fill="y")
        left_scroll_x.pack(side="bottom", fill="x")
        self.local_text.pack(fill="both", expand=True)

        # Правая панель — версия репозитория
        right_frame = ttk.LabelFrame(diff_container, text="Версия репозитория")
        diff_container.add(right_frame, weight=1)

        self.remote_text = tk.Text(
            right_frame, wrap="none", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            padx=4, pady=4,
        )
        right_scroll_y = ttk.Scrollbar(right_frame, orient="vertical",
                                        command=self.remote_text.yview)
        right_scroll_x = ttk.Scrollbar(right_frame, orient="horizontal",
                                        command=self.remote_text.xview)
        self.remote_text.configure(
            yscrollcommand=self._make_yscroll(self.remote_text, "right"),
            xscrollcommand=right_scroll_x.set,
        )
        right_scroll_y.pack(side="right", fill="y")
        right_scroll_x.pack(side="bottom", fill="x")
        self.remote_text.pack(fill="both", expand=True)

        # Теги подсветки
        for widget in (self.local_text, self.remote_text):
            widget.tag_configure(self.TAG_ADDED, background="#264f78", foreground="#d4d4d4")
            widget.tag_configure(self.TAG_REMOVED, background="#5a1d1d", foreground="#d4d4d4")
            widget.tag_configure(self.TAG_HEADER, background="#333", foreground="#569cd6")

        # Заполняем тексты
        self._populate_texts(local_content, remote_content)

        self.local_text.configure(state="disabled")
        self.remote_text.configure(state="disabled")

        # === Кнопки ===
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=8, pady=8)

        ttk.Button(
            btn_frame, text="← Применить локальную версию в Репо",
            command=lambda: self._apply("local_to_remote"),
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_frame, text="Скачать версию Репо → Локально",
            command=lambda: self._apply("remote_to_local"),
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_frame, text="Закрыть", command=self.destroy,
        ).pack(side="right", padx=4)

        # Синхронизация горизонтальной прокрутки
        self._syncing_scroll = False

    def _make_yscroll(self, source_widget, side):
        """Создаёт функцию прокрутки с синхронизацией."""
        def on_scroll(*args):
            if self._syncing_scroll:
                return
            self._syncing_scroll = True
            target = self.remote_text if side == "left" else self.local_text
            target.yview_moveto(args[0])
            source_widget.yview_moveto(args[0])
            self._syncing_scroll = False
        return on_scroll

    def _populate_texts(self, local_content, remote_content):
        """Заполняет текстовые панели и подсвечивает различия."""
        local_bin = local_content or b""
        remote_bin = remote_content or b""

        # Проверяем на бинарность
        if is_binary(local_bin) or is_binary(remote_bin):
            self.local_text.insert("1.0",
                "(Локально) Бинарный файл — текстовое сравнение недоступно\n"
                f"Размер: {len(local_bin)} байт")
            self.remote_text.insert("1.0",
                "(Репозиторий) Бинарный файл — текстовое сравнение недоступно\n"
                f"Размер: {len(remote_bin)} байт")
            return

        local_lines = local_bin.decode("utf-8", errors="replace").splitlines()
        remote_lines = remote_bin.decode("utf-8", errors="replace").splitlines()

        # Пустые файлы
        if not local_lines and not remote_lines:
            self.local_text.insert("1.0", "(пустой файл)")
            self.remote_text.insert("1.0", "(пустой файл)")
            return

        if not local_lines:
            self.local_text.insert("1.0", "(файл отсутствует локально)")
            self._fill_panel(self.remote_text, remote_lines, [])
            return

        if not remote_lines:
            self._fill_panel(self.local_text, local_lines, [])
            self.remote_text.insert("1.0", "(файл отсутствует в репозитории)")
            return

        # Вычисляем diff через SequenceMatcher
        matcher = difflib.SequenceMatcher(None, remote_lines, local_lines)

        local_out = []
        remote_out = []
        local_tags = []  # (line_no_1based, tag_name)
        remote_tags = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    line = remote_lines[i1 + k]
                    remote_out.append(line)
                    local_out.append(line)
            elif tag == "replace":
                # Удалённые строки (были в remote, заменены в local)
                for k in range(i1, i2):
                    remote_out.append(remote_lines[k])
                    remote_tags.append((len(remote_out), self.TAG_REMOVED))
                # Новые строки (в local)
                for k in range(j1, j2):
                    local_out.append(local_lines[k])
                    local_tags.append((len(local_out), self.TAG_ADDED))
                # Выравниваем панели пустыми строками
                remote_missing = (j2 - j1) - (i2 - i1)
                if remote_missing > 0:
                    for _ in range(remote_missing):
                        remote_out.append("")
                elif remote_missing < 0:
                    for _ in range(-remote_missing):
                        local_out.append("")
            elif tag == "delete":
                # Строки есть в remote, но нет в local
                for k in range(i1, i2):
                    remote_out.append(remote_lines[k])
                    remote_tags.append((len(remote_out), self.TAG_REMOVED))
                    local_out.append("")
            elif tag == "insert":
                # Строки есть в local, но нет в remote
                for k in range(j1, j2):
                    local_out.append(local_lines[k])
                    local_tags.append((len(local_out), self.TAG_ADDED))
                    remote_out.append("")

        self._fill_panel(self.local_text, local_out, local_tags)
        self._fill_panel(self.remote_text, remote_out, remote_tags)

    @staticmethod
    def _fill_panel(text_widget, lines, tags):
        """Заполняет текстовый виджет строками и применяет теги."""
        content = "\n".join(lines)
        text_widget.insert("1.0", content)
        for line_no, tag_name in tags:
            text_widget.tag_add(tag_name, f"{line_no}.0", f"{line_no}.end")

    def _apply(self, direction):
        self.on_apply(direction)
        self.destroy()


# ============================================================
# Главное приложение
# ============================================================

class MainApp(tk.Tk):
    """Главное окно приложения GitMergeMods."""

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("780x560")
        self.minsize(650, 420)

        self.config = Config()
        self.github = None
        self.scanner = None
        self.connected = False
        self.scanning = False
        self.changes = []
        self.local_files = {}
        self.remote_tree = {}
        self._scan_after_id = None
        self._lock = threading.Lock()

        self._setup_theme()
        self._create_ui()
        self._load_config_to_ui()

    # ---- Тема ----

    def _setup_theme(self):
        style = ttk.Style(self)
        available = style.theme_names()
        for theme in ("vista", "winnative", "clam"):
            if theme in available:
                style.theme_use(theme)
                break

    # ---- Создание UI ----

    def _create_ui(self):
        # === Панель настроек ===
        settings = ttk.LabelFrame(self, text=" Подключение ", padding=6)
        settings.pack(fill="x", padx=8, pady=(8, 4))
        settings.columnconfigure(1, weight=1)

        # Токен
        ttk.Label(settings, text="🔑 Токен GitHub:").grid(
            row=0, column=0, sticky="w", padx=2, pady=2
        )
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(settings, textvariable=self.token_var, width=65, show="•")
        self.token_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=2, pady=2)

        # Репозиторий
        ttk.Label(settings, text="📦 Репозиторий:").grid(
            row=1, column=0, sticky="w", padx=2, pady=2
        )
        self.repo_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.repo_var, width=65).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=2, pady=2
        )

        # Локальная папка
        ttk.Label(settings, text="📁 Папка модов:").grid(
            row=2, column=0, sticky="w", padx=2, pady=2
        )
        self.folder_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.folder_var, width=55).grid(
            row=2, column=1, sticky="ew", padx=2, pady=2
        )
        ttk.Button(settings, text="📂 Обзор", width=8, command=self._browse_folder).grid(
            row=2, column=2, padx=2, pady=2
        )

        # Ветка
        ttk.Label(settings, text="🌿 Ветка:").grid(
            row=3, column=0, sticky="w", padx=2, pady=2
        )
        self.branch_var = tk.StringVar(value="main")
        ttk.Entry(settings, textvariable=self.branch_var, width=25).grid(
            row=3, column=1, sticky="w", padx=2, pady=2
        )

        # === Панель управления ===
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8, pady=4)

        self.connect_btn = ttk.Button(ctrl, text="▶ Подключить", width=16,
                                       command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=4)

        self.sync_btn = ttk.Button(ctrl, text="🔄 Загрузить всё из репо",
                                    width=24, command=self._download_all, state="disabled")
        self.sync_btn.pack(side="left", padx=4)

        self.upload_all_btn = ttk.Button(ctrl, text="📤 Отправить всё в репо",
                                          width=24, command=self._upload_all, state="disabled")
        self.upload_all_btn.pack(side="left", padx=4)

        # Режимы
        modes = ttk.Frame(self)
        modes.pack(fill="x", padx=8, pady=2)

        self.auto_push_var = tk.BooleanVar()
        ttk.Checkbutton(modes, text="🔄 Авто-пуш (автоматическая синхронизация)",
                         variable=self.auto_push_var,
                         command=self._on_auto_push_toggle).pack(side="left", padx=8)

        self.filter_var = tk.BooleanVar()
        ttk.Checkbutton(modes, text="📋 Только .xml / .txt",
                         variable=self.filter_var,
                         command=self._on_filter_toggle).pack(side="left", padx=8)

        # === Список изменений ===
        list_frame = ttk.LabelFrame(self, text=" Изменённые файлы (двойной клик → diff) ", padding=4)
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("path", "status", "direction")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse"
        )
        self.tree.heading("path", text="Файл")
        self.tree.heading("status", text="Статус")
        self.tree.heading("direction", text="Направление")
        self.tree.column("path", width=360, minwidth=200)
        self.tree.column("status", width=130, minwidth=80)
        self.tree.column("direction", width=170, minwidth=100)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._on_double_click)

        # Теги раскраски
        self.tree.tag_configure("local_only", background="#d4edda")
        self.tree.tag_configure("remote_only", background="#cce5ff")
        self.tree.tag_configure("changed", background="#f8d7da")

        # === Статус-бар ===
        self.status_var = tk.StringVar(
            value="Отключено. Укажите настройки и нажмите «Подключить»."
        )
        status_bar = ttk.Label(self, textvariable=self.status_var,
                                relief="sunken", anchor="w", padding=4)
        status_bar.pack(fill="x", side="bottom", padx=8, pady=(0, 8))

    # ---- Конфигурация UI ----

    def _load_config_to_ui(self):
        self.token_var.set(self.config.token)
        self.repo_var.set(self.config.repo_url)
        self.folder_var.set(self.config.local_folder)
        self.branch_var.set(self.config.branch)
        self.auto_push_var.set(self.config.auto_push)
        self.filter_var.set(self.config.filter_extensions)

    def _save_config_from_ui(self):
        self.config.token = self.token_var.get().strip()
        self.config.repo_url = self.repo_var.get().strip()
        self.config.local_folder = self.folder_var.get().strip()
        self.config.branch = self.branch_var.get().strip() or "main"
        self.config.auto_push = self.auto_push_var.get()
        self.config.filter_extensions = self.filter_var.get()
        self.config.save()

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку модов")
        if folder:
            self.folder_var.set(folder)

    # ---- Подключение ----

    def _toggle_connection(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        self._save_config_from_ui()
        token = self.config.token
        repo_url = self.config.repo_url
        folder = self.config.local_folder
        branch = self.config.branch

        if not token:
            messagebox.showerror("Ошибка", "Укажите токен GitHub (с правами repo).")
            return
        if not repo_url:
            messagebox.showerror("Ошибка", "Укажите URL репозитория.")
            return
        if not folder:
            messagebox.showerror("Ошибка", "Укажите локальную папку.")
            return

        self.status_var.set("Подключение...")
        self.update_idletasks()

        def connect_worker():
            try:
                client = GitHubClient(token, repo_url, branch)
                repo_info = client.test_connection()

                if not repo_info:
                    self.after(0, lambda: messagebox.showerror(
                        "Ошибка", "Не удалось подключиться. Проверьте токен и URL."
                    ))
                    self.after(0, lambda: self.status_var.set("Ошибка подключения"))
                    return

                self.github = client
                if not os.path.isdir(folder):
                    os.makedirs(folder, exist_ok=True)

                exts = self.config.extensions if self.config.filter_extensions else None
                self.scanner = FileScanner(folder, exts)

                self.connected = True
                repo_name = repo_info.get("full_name", "?")
                self.after(0, lambda: self._on_connected(repo_name, branch))

            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка подключения", str(e)))
                self.after(0, lambda: self.status_var.set(f"Ошибка: {e}"))

        threading.Thread(target=connect_worker, daemon=True).start()

    def _on_connected(self, repo_name, branch):
        self.connect_btn.configure(text="⏹ Отключить")
        self.sync_btn.configure(state="normal")
        self.upload_all_btn.configure(state="normal")
        self.status_var.set(f"✅ Подключено к {repo_name} ({branch})")
        self._start_scanning()

    def _disconnect(self):
        self.connected = False
        if self._scan_after_id:
            self.after_cancel(self._scan_after_id)
            self._scan_after_id = None
        self.connect_btn.configure(text="▶ Подключить")
        self.sync_btn.configure(state="disabled")
        self.upload_all_btn.configure(state="disabled")
        self.status_var.set("Отключено")
        self.tree.delete(*self.tree.get_children())
        self.changes = []

    # ---- Сканирование ----

    def _start_scanning(self):
        if self.connected:
            self._scan_after_id = self.after(0, self._do_scan)

    def _do_scan(self):
        if not self.connected or self.scanning:
            if self.connected:
                self._scan_after_id = self.after(SCAN_INTERVAL_SEC * 1000, self._do_scan)
            return

        self.scanning = True

        def scan_worker():
            try:
                # Обновляем фильтр расширений
                if self.scanner:
                    self.scanner.extensions_filter = (
                        self.config.extensions if self.filter_var.get() else None
                    )

                local_files = self.scanner.scan_local() if self.scanner else {}
                remote_tree, commit_sha = self.github.get_tree() if self.github else ({}, None)
                changes = FileScanner.compare(local_files, remote_tree)

                with self._lock:
                    self.local_files = local_files
                    self.remote_tree = remote_tree
                    self.changes = changes

                self.after(0, self._update_changes_list)

                # Авто-пуш
                if self.auto_push_var.get() and changes:
                    self.after(100, self._auto_push_changes)

            except Exception as e:
                self.after(0, lambda: self.status_var.set(f"⚠ Ошибка сканирования: {e}"))
            finally:
                self.scanning = False
                if self.connected:
                    self._scan_after_id = self.after(
                        SCAN_INTERVAL_SEC * 1000, self._do_scan
                    )

        threading.Thread(target=scan_worker, daemon=True).start()

    def _update_changes_list(self):
        """Обновляет список файлов в UI."""
        # Запоминаем выделение
        sel = self.tree.selection()
        sel_path = None
        if sel:
            vals = self.tree.item(sel[0], "values")
            if vals:
                sel_path = vals[0]

        self.tree.delete(*self.tree.get_children())

        with self._lock:
            changes = list(self.changes)

        STATUS_TEXT = {
            "local_only": "Только локально",
            "remote_only": "Только в репо",
            "changed": "Отличается",
        }
        DIR_TEXT = {
            "local_only": "локальн. → репо",
            "remote_only": "репо → локальн.",
            "changed": "отличие (×2)",
        }

        for c in changes:
            path = c["path"]
            status = STATUS_TEXT.get(c["status"], c["status"])
            direction = DIR_TEXT.get(c["status"], "—")
            iid = self.tree.insert("", "end", values=(path, status, direction),
                                    tags=(c["status"],))
            if path == sel_path:
                self.tree.selection_set(iid)

        now = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            total_local = len(self.local_files)
            n_changes = len(self.changes)
        synced = total_local - n_changes
        if synced < 0:
            synced = 0
        self.status_var.set(
            f"✅ Синхронизировано: {synced} | Изменений: {n_changes} | Проверка: {now}"
        )

    # ---- Двойной клик → Diff ----

    def _on_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not vals:
            return
        file_path = vals[0]
        self._show_diff(str(file_path))

    def _show_diff(self, file_path):
        with self._lock:
            local_info = self.local_files.get(file_path)
            remote_info = self.remote_tree.get(file_path)

        if not local_info and not remote_info:
            return

        # Локальное содержимое
        local_content = None
        if local_info:
            try:
                with open(local_info["full_path"], "rb") as f:
                    local_content = f.read()
            except Exception:
                local_content = None

        self.status_var.set(f"Загрузка {file_path}...")
        self.update_idletasks()

        def fetch_and_show():
            try:
                remote_content = None
                remote_sha = None
                if remote_info:
                    remote_content, remote_sha = self.github.get_file_content(file_path)

                status = "changed"
                if local_info and not remote_info:
                    status = "local_only"
                elif remote_info and not local_info:
                    status = "remote_only"

                self.after(0, lambda: self._open_diff_window(
                    file_path, local_content, remote_content, remote_sha, status
                ))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка", str(e)))

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _open_diff_window(self, file_path, local_content, remote_content,
                           remote_sha, status):
        with self._lock:
            local_info = self.local_files.get(file_path)
            local_hash = local_info["hash"] if local_info else None

        def on_apply(direction):
            self._apply_single_change(
                file_path, direction, local_content, remote_content,
                local_hash, remote_sha
            )

        DiffWindow(self, file_path, local_content, remote_content, status, on_apply)

    def _apply_single_change(self, file_path, direction, local_content,
                              remote_content, local_hash, remote_sha):
        """Применяет одно изменение."""
        try:
            if direction == "local_to_remote":
                if local_content is None:
                    messagebox.showwarning("Внимание", "Нет локального содержимого.")
                    return
                self.status_var.set(f"Отправка {file_path} → репозиторий...")
                self.update_idletasks()
                self.github.upload_file(file_path, local_content,
                                         existing_sha=remote_sha)
                self.status_var.set(f"✅ {file_path} отправлен в репозиторий")

            elif direction == "remote_to_local":
                if remote_content is None:
                    messagebox.showwarning("Внимание", "Нет содержимого репозитория.")
                    return
                self.status_var.set(f"Загрузка {file_path} → локально...")
                self.update_idletasks()
                local_path = os.path.join(
                    self.config.local_folder, file_path.replace("/", os.sep)
                )
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(remote_content)
                self.status_var.set(f"✅ {file_path} загружен локально")

            self.after(1000, self._do_scan)

        except Exception as e:
            messagebox.showerror("Ошибка применения", str(e))
            self.status_var.set(f"⚠ Ошибка: {e}")

    # ---- Авто-пуш ----

    def _auto_push_changes(self):
        with self._lock:
            changes = list(self.changes)

        if not changes:
            return

        self.status_var.set(f"🔄 Авто-пуш: обработка {len(changes)} изменений...")

        def auto_worker():
            for change in changes:
                if not self.connected:
                    break
                try:
                    path = change["path"]
                    status = change["status"]

                    if status == "local_only":
                        # Локальный файл → отправить в репо
                        full_path = change.get("full_path") or os.path.join(
                            self.config.local_folder, path.replace("/", os.sep)
                        )
                        if os.path.isfile(full_path):
                            with open(full_path, "rb") as f:
                                content = f.read()
                            self.github.upload_file(path, content)
                    elif status == "remote_only":
                        # Удалённый файл → скачать локально
                        remote_content, _ = self.github.get_file_content(path)
                        if remote_content:
                            local_path = os.path.join(
                                self.config.local_folder, path.replace("/", os.sep)
                            )
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, "wb") as f:
                                f.write(remote_content)
                    elif status == "changed":
                        # Конфликт — приоритет репозитория (безопасно для многопользовательской работы)
                        remote_content, _ = self.github.get_file_content(path)
                        if remote_content:
                            local_path = os.path.join(
                                self.config.local_folder, path.replace("/", os.sep)
                            )
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, "wb") as f:
                                f.write(remote_content)

                except Exception as e:
                    print(f"Auto-push error [{path}]: {e}")

            self.after(0, lambda: self.status_var.set("✅ Авто-пуш завершён"))
            # Пересканируем через секунду
            self.after(2000, self._do_scan)

        threading.Thread(target=auto_worker, daemon=True).start()

    # ---- Полная синхронизация ----

    def _download_all(self):
        """Загружает все файлы из репозитория в локальную папку."""
        if not self.connected or not self.github:
            return

        self.status_var.set("📥 Загрузка всех файлов из репозитория...")
        self.update_idletasks()

        def download_worker():
            try:
                remote_tree, _ = self.github.get_tree()
                count = 0
                for path, info in remote_tree.items():
                    # Фильтр расширений
                    if self.filter_var.get():
                        ext = os.path.splitext(path)[1].lower()
                        if ext not in self.config.extensions:
                            continue

                    local_path = os.path.join(
                        self.config.local_folder, path.replace("/", os.sep)
                    )

                    # Проверяем, нужно ли скачивать
                    need_download = False
                    if not os.path.exists(local_path):
                        need_download = True
                    else:
                        with open(local_path, "rb") as f:
                            local_hash = compute_git_blob_hash(f.read())
                        if local_hash != info["sha"]:
                            need_download = True

                    if need_download:
                        content, _ = self.github.get_file_content(path)
                        if content:
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, "wb") as f:
                                f.write(content)
                            count += 1

                msg = f"✅ Загружено {count} файлов"
                self.after(0, lambda: self.status_var.set(msg))
                self.after(1000, self._do_scan)

            except Exception as e:
                self.after(0, lambda: self.status_var.set(f"⚠ Ошибка загрузки: {e}"))

        threading.Thread(target=download_worker, daemon=True).start()

    def _upload_all(self):
        """Отправляет все локальные файлы в репозиторий."""
        if not self.connected or not self.github:
            return

        self.status_var.set("📤 Отправка всех файлов в репозиторий...")
        self.update_idletasks()

        def upload_worker():
            try:
                with self._lock:
                    local_files = dict(self.local_files)
                    remote_tree = dict(self.remote_tree)

                count = 0
                for path, info in local_files.items():
                    # Фильтр расширений
                    if self.filter_var.get():
                        ext = os.path.splitext(path)[1].lower()
                        if ext not in self.config.extensions:
                            continue

                    with open(info["full_path"], "rb") as f:
                        content = f.read()

                    remote_sha = remote_tree.get(path, {}).get("sha")
                    if remote_sha and info["hash"] == remote_sha:
                        continue  # Уже синхронизирован

                    self.github.upload_file(path, content, existing_sha=remote_sha)
                    count += 1

                msg = f"✅ Отправлено {count} файлов"
                self.after(0, lambda: self.status_var.set(msg))
                self.after(1000, self._do_scan)

            except Exception as e:
                self.after(0, lambda: self.status_var.set(f"⚠ Ошибка отправки: {e}"))

        threading.Thread(target=upload_worker, daemon=True).start()

    # ---- Обработчики галочек ----

    def _on_auto_push_toggle(self):
        self.config.auto_push = self.auto_push_var.get()
        self.config.save()

    def _on_filter_toggle(self):
        self.config.filter_extensions = self.filter_var.get()
        self.config.save()
        if self.connected:
            self._do_scan()


# ============================================================
# Точка входа
# ============================================================

def main():
    try:
        import tkinter as _tk
    except ImportError:
        print("Ошибка: tkinter не установлен.")
        print("Установите Python с поддержкой tkinter: https://www.python.org/downloads/")
        input("Нажмите Enter для выхода...")
        sys.exit(1)

    app = MainApp()

    # Иконка (пробуем загрузить, не критично)
    try:
        icon_path = os.path.join(APP_DIR, "icon.ico")
        if os.path.exists(icon_path):
            app.iconbitmap(icon_path)
    except Exception:
        pass

    app.mainloop()


if __name__ == "__main__":
    main()
