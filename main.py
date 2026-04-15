#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitMergeMods v1.1
Легковесное приложение для синхронизации файлов модов с GitHub.
Работает через GitHub REST API — не требует установки Git.
Batch-коммиты, мерж по умолчанию, конфликт-диалог.
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
APP_VERSION = "1.1"
SCAN_INTERVAL_SEC = 5
API_BASE = "https://api.github.com"

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "gitmergemods_config.json")


# ============================================================
# Утилиты
# ============================================================

def compute_git_blob_hash(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


# ============================================================
# Мерж текстовых файлов
# ============================================================

class MergeResult:
    def __init__(self, success, content=None, conflicts=None,
                 local_only_lines=0, remote_only_lines=0):
        self.success = success
        self.content = content
        self.conflicts = conflicts or []
        self.local_only_lines = local_only_lines
        self.remote_only_lines = remote_only_lines


def merge_text_contents(local_content: bytes, remote_content: bytes,
                        path: str = "") -> MergeResult:
    if not local_content:
        return MergeResult(success=True, content=remote_content)
    if not remote_content:
        return MergeResult(success=True, content=local_content)

    if is_binary(local_content) or is_binary(remote_content):
        return MergeResult(
            success=False,
            conflicts=["Бинарный файл — автоматический мерж невозможен"],
        )

    local_text = local_content.decode("utf-8", errors="replace")
    remote_text = remote_content.decode("utf-8", errors="replace")
    local_lines = local_text.splitlines()
    remote_lines = remote_text.splitlines()

    if local_lines == remote_lines:
        return MergeResult(success=True, content=local_content)

    merged = []
    conflicts = []
    local_added = 0
    remote_added = 0

    matcher = difflib.SequenceMatcher(None, remote_lines, local_lines)
    opcodes = list(matcher.get_opcodes())

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            merged.extend(remote_lines[i1:i2])
        elif tag == "insert":
            merged.extend(local_lines[j1:j2])
            local_added += (j2 - j1)
        elif tag == "delete":
            pass  # локально удалены — не берём
        elif tag == "replace":
            remote_block = remote_lines[i1:i2]
            local_block = local_lines[j1:j2]
            if remote_block == local_block:
                merged.extend(remote_block)
            else:
                conflicts.append(
                    f"Строки {len(merged) + 1}-{len(merged) + 1 + (j2 - j1) + 5 + (i2 - i1)}"
                )
                merged.append("<<<<<<< ЛОКАЛЬНАЯ ВЕРСИЯ")
                merged.extend(local_block)
                merged.append("=======")
                merged.extend(remote_block)
                merged.append(">>>>>>> ВЕРСИЯ РЕПОЗИТОРИЯ")
                local_added += (j2 - j1)
                remote_added += (i2 - i1)

    result_text = "\n".join(merged)
    if local_text.endswith("\n") or remote_text.endswith("\n"):
        result_text += "\n"
    result_bytes = result_text.encode("utf-8")

    if conflicts:
        return MergeResult(
            success=False, content=result_bytes, conflicts=conflicts,
            local_only_lines=local_added, remote_only_lines=remote_added,
        )
    return MergeResult(
        success=True, content=result_bytes,
        local_only_lines=local_added, remote_only_lines=remote_added,
    )


def save_conflict_files(local_folder, rel_path, local_content, remote_content):
    local_path = os.path.join(local_folder, rel_path.replace("/", os.sep))
    backup_path = local_path + ".local"
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    with open(backup_path, "wb") as f:
        f.write(local_content)
    with open(local_path, "wb") as f:
        f.write(remote_content)
    return backup_path


# ============================================================
# Конфигурация
# ============================================================

class Config:
    DEFAULTS = {
        "token": "",
        "repo_url": "",
        "local_folder": "",
        "branch": "main",
        "auto_push": False,
        "filter_extensions": False,
        "extensions": [".xml", ".txt"],
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
    """Клиент GitHub API v3 с поддержкой batch-коммитов через Git Data API."""

    def __init__(self, token, repo_url, branch="main"):
        self.token = token
        self.branch = branch
        self.owner, self.repo = self._parse_repo_url(repo_url)
        self._cached_tree = None
        self._cached_commit_sha = None

    @staticmethod
    def _parse_repo_url(url):
        url = url.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        match = re.match(
            r"(?:https?://github\.com/|git@github\.com:)([^/]+)/(.+)", url
        )
        if match:
            return match.group(1), match.group(2)
        raise ValueError(f"Не удалось распознать URL: {url}")

    def _api_request(self, method, endpoint, data=None, params=None):
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

    # ---- Базовые операции ----

    def test_connection(self):
        return self._api_request("GET", f"/repos/{self.owner}/{self.repo}")

    def get_latest_commit_sha(self):
        data = self._api_request(
            "GET", f"/repos/{self.owner}/{self.repo}/commits/{self.branch}"
        )
        if data and "sha" in data:
            return data["sha"]
        return None

    def get_tree(self):
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

    def get_file_content(self, path):
        data = self._api_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{path}",
            params={"ref": self.branch},
        )
        if data and "content" in data:
            raw = base64.b64decode(data["content"].replace("\n", ""))
            return raw, data.get("sha")

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

    # ---- Batch-коммит через Git Data API ----

    def create_blob(self, content):
        """Создаёт blob и возвращает его SHA."""
        content_b64 = base64.b64encode(content).decode("ascii")
        data = self._api_request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/git/blobs",
            data={"content": content_b64, "encoding": "base64"},
        )
        if data and "sha" in data:
            return data["sha"]
        raise Exception("Не удалось создать blob")

    def batch_commit(self, files_to_upload, files_to_delete_shas=None,
                     message=None, progress_callback=None):
        """
        Создаёт один коммит с несколькими файлами через Git Data API.

        files_to_upload: dict {path: bytes_content}
        files_to_delete_shas: dict {path: remote_sha}
        message: текст коммита
        progress_callback: callable(step_text, percent) или None

        Возвращает (success: bool, new_commit_sha: str | None)
        """
        if not files_to_upload and not files_to_delete_shas:
            return True, None

        def report(step_text, percent):
            if progress_callback:
                progress_callback(step_text, percent)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not message:
            n_up = len(files_to_upload)
            n_del = len(files_to_delete_shas or {})
            parts = []
            if n_up:
                parts.append(f"{n_up} файл(ов) обновлено")
            if n_del:
                parts.append(f"{n_del} файл(ов) удалено")
            message = f"Auto-sync: {', '.join(parts)} [{ts}]"

        # 1. Текущий коммит и его дерево
        report("Получение текущего состояния...", 5)
        current_sha = self.get_latest_commit_sha()
        if not current_sha:
            return False, None

        commit_data = self._api_request(
            "GET", f"/repos/{self.owner}/{self.repo}/git/commits/{current_sha}"
        )
        if not commit_data:
            return False, None
        base_tree_sha = commit_data["tree"]["sha"]

        # 2. Создаём blobs для файлов (самая долгая часть)
        tree_entries = []
        total = len(files_to_upload)
        for idx, (path, content) in enumerate(files_to_upload.items()):
            pct = int(10 + (idx / max(total, 1)) * 65)
            short_name = os.path.basename(path)
            report(f"Отправка: {short_name} ({idx + 1}/{total})", pct)
            blob_sha = self.create_blob(content)
            tree_entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

        # Удаления: SHA = null в tree
        if files_to_delete_shas:
            for path in files_to_delete_shas:
                tree_entries.append({
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": None,
                })

        # 3. Создаём новое дерево
        report("Создание дерева файлов...", 80)
        new_tree = self._api_request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/git/trees",
            data={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        if not new_tree:
            return False, None
        new_tree_sha = new_tree["sha"]

        # 4. Создаём коммит
        report("Создание коммита...", 90)
        new_commit = self._api_request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/git/commits",
            data={
                "message": message,
                "tree": new_tree_sha,
                "parents": [current_sha],
            },
        )
        if not new_commit:
            return False, None
        new_commit_sha = new_commit["sha"]

        # 5. Обновляем ссылку ветки
        report("Применение коммита...", 95)
        result = self._api_request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/git/refs/heads/{self.branch}",
            data={"sha": new_commit_sha, "force": False},
        )
        if not result:
            return False, None

        self.invalidate_cache()
        report("Готово", 100)
        return True, new_commit_sha

    def upload_file(self, path, content, existing_sha=None):
        """Загрузка одного файла (для ручного разрешения конфликтов)."""
        content_b64 = base64.b64encode(content).decode("ascii")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "message": f"Auto-sync {path} [{ts}]",
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
        self.invalidate_cache()
        return result

    def invalidate_cache(self):
        self._cached_commit_sha = None
        self._cached_tree = None


# ============================================================
# Сканер локальных файлов
# ============================================================

class FileScanner:
    def __init__(self, local_folder, extensions_filter=None):
        self.local_folder = local_folder
        self.extensions_filter = extensions_filter

    def scan_local(self):
        result = {}
        if not os.path.isdir(self.local_folder):
            return result

        for root, dirs, files in os.walk(self.local_folder):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d.lower() not in ("__pycache__", "node_modules")
            ]
            for fname in files:
                if fname.startswith(".") or fname.endswith(".local"):
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
        all_paths = set(local_files.keys()) | set(remote_tree.keys())
        changes = []

        for path in sorted(all_paths):
            local = local_files.get(path)
            remote = remote_tree.get(path)

            if local and not remote:
                changes.append({
                    "path": path, "status": "local_only",
                    "local_hash": local["hash"], "remote_sha": None,
                    "full_path": local["full_path"],
                })
            elif remote and not local:
                changes.append({
                    "path": path, "status": "remote_only",
                    "local_hash": None, "remote_sha": remote["sha"],
                    "full_path": path,
                })
            elif local["hash"] != remote["sha"]:
                changes.append({
                    "path": path, "status": "changed",
                    "local_hash": local["hash"],
                    "remote_sha": remote["sha"],
                    "full_path": local["full_path"],
                })

        return changes


# ============================================================
# Окно конфликта (большой красный диалог)
# ============================================================

class ConflictDialog(tk.Toplevel):
    """Большой диалог для разрешения конфликта с красным предупреждением."""

    def __init__(self, parent, file_path, local_content, remote_content,
                 remote_sha, on_resolve_callback):
        super().__init__(parent)
        self.title(f"⚠ КОНФЛИКТ: {os.path.basename(file_path)}")
        self.geometry("700x420")
        self.minsize(550, 350)
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)

        self.on_resolve = on_resolve_callback
        self.file_path = file_path
        self.local_content = local_content
        self.remote_content = remote_content
        self.remote_sha = remote_sha
        self._resolved = False

        # === Красное предупреждение ===
        warn_frame = tk.Frame(self, bg="#cc0000", padx=12, pady=10)
        warn_frame.pack(fill="x", padx=0, pady=0)

        tk.Label(
            warn_frame,
            text="⚠  КОНФЛИКТ ИЗМЕНЕНИЙ!  ⚠",
            font=("Segoe UI", 16, "bold"),
            fg="white", bg="#cc0000",
        ).pack(anchor="center")

        tk.Label(
            warn_frame,
            text=f"Файл изменён и локально, и в репозитории.\n"
                 f"Выберите действие для: {file_path}",
            font=("Segoe UI", 11),
            fg="#ffeeee", bg="#cc0000",
            justify="center",
        ).pack(anchor="center", pady=(4, 0))

        # === Информация о файле ===
        info_frame = ttk.Frame(self, padding=10)
        info_frame.pack(fill="x", padx=8, pady=4)

        local_size = len(local_content) if local_content else 0
        remote_size = len(remote_content) if remote_content else 0

        local_lines = (
            local_content.decode("utf-8", errors="replace").count("\n") + 1
            if local_content else 0
        )
        remote_lines = (
            remote_content.decode("utf-8", errors="replace").count("\n") + 1
            if remote_content else 0
        )

        ttk.Label(
            info_frame,
            text=f"📁 {file_path}\n"
                 f"Локально: {local_size} байт, {local_lines} строк  |  "
                 f"Репозиторий: {remote_size} байт, {remote_lines} строк",
            wraplength=650,
        ).pack(anchor="w")

        # Мерж-информация
        mr = merge_text_contents(local_content, remote_content, file_path)
        if not mr.success:
            conflict_count = len(mr.conflicts)
            merge_label = tk.Label(
                info_frame,
                text=f"⚠ Конфликтующих блоков: {conflict_count} — "
                     f"автоматический мерж невозможен",
                fg="#cc0000", font=("Segoe UI", 10, "bold"),
            )
            merge_label.pack(anchor="w", pady=(4, 0))

        # === Кнопки действий ===
        btn_frame = ttk.Frame(self, padding=8)
        btn_frame.pack(fill="x", padx=8, pady=8)

        ttk.Button(
            btn_frame,
            text="📤 Отправить свой файл в репозиторий",
            command=lambda: self._resolve("push_local"),
        ).pack(fill="x", pady=3)

        ttk.Label(
            btn_frame,
            text="Ваша версия заменит файл в репозитории",
            foreground="#888",
        ).pack(anchor="w", padx=4)

        ttk.Separator(btn_frame).pack(fill="x", pady=6)

        ttk.Button(
            btn_frame,
            text="📥 Скачать из репозитория (вы потеряете свои изменения!)",
            command=lambda: self._resolve("pull_remote"),
        ).pack(fill="x", pady=3)

        tk.Label(
            btn_frame,
            text="ВНИМАНИЕ: Ваши локальные изменения будут заменены!",
            fg="#cc0000", font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", padx=4)

        ttk.Separator(btn_frame).pack(fill="x", pady=6)

        ttk.Button(
            btn_frame,
            text="💾 Сохранить обе версии (.local файл)",
            command=lambda: self._resolve("save_both"),
        ).pack(fill="x", pady=3)

        ttk.Label(
            btn_frame,
            text="Репо-версия → основной файл, ваша → файл.local",
            foreground="#888",
        ).pack(anchor="w", padx=4)

        ttk.Separator(btn_frame).pack(fill="x", pady=6)

        ttk.Button(
            btn_frame,
            text="❌ Отмена — пропустить файл",
            command=self._cancel,
        ).pack(fill="x", pady=3)

    def _resolve(self, action):
        self._resolved = True
        self.on_resolve(action)
        self.destroy()

    def _cancel(self):
        self._resolved = False
        self.destroy()

    def destroy(self):
        if not self._resolved:
            self.on_resolve("skip")
        super().destroy()


# ============================================================
# Окно просмотра Diff
# ============================================================

class DiffWindow(tk.Toplevel):
    TAG_ADDED = "added"
    TAG_REMOVED = "removed"

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

        # === Информация ===
        info_frame = ttk.Frame(self)
        info_frame.pack(fill="x", padx=8, pady=(8, 4))

        status_text = {
            "local_only": "📄 Только локально",
            "remote_only": "☁️ Только в репозитории",
            "changed": "⚡ Изменён с обеих сторон",
        }.get(local_status, local_status)

        ttk.Label(
            info_frame, text=f"📁 {file_path}",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(info_frame, text=status_text, foreground="#555").pack(anchor="w")

        # === Side-by-side Diff ===
        diff_container = ttk.PanedWindow(self, orient="horizontal")
        diff_container.pack(fill="both", expand=True, padx=8, pady=4)

        left_frame = ttk.LabelFrame(diff_container, text="Локальная версия")
        diff_container.add(left_frame, weight=1)
        self.local_text = tk.Text(
            left_frame, wrap="none", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", padx=4, pady=4,
        )
        lsy = ttk.Scrollbar(left_frame, orient="vertical",
                             command=self.local_text.yview)
        lsx = ttk.Scrollbar(left_frame, orient="horizontal",
                             command=self.local_text.xview)
        self.local_text.configure(yscrollcommand=lsy.set, xscrollcommand=lsx.set)
        lsy.pack(side="right", fill="y")
        lsx.pack(side="bottom", fill="x")
        self.local_text.pack(fill="both", expand=True)

        right_frame = ttk.LabelFrame(diff_container, text="Версия репозитория")
        diff_container.add(right_frame, weight=1)
        self.remote_text = tk.Text(
            right_frame, wrap="none", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", padx=4, pady=4,
        )
        rsy = ttk.Scrollbar(right_frame, orient="vertical",
                             command=self.remote_text.yview)
        rsx = ttk.Scrollbar(right_frame, orient="horizontal",
                             command=self.remote_text.xview)
        self.remote_text.configure(yscrollcommand=rsy.set, xscrollcommand=rsx.set)
        rsy.pack(side="right", fill="y")
        rsx.pack(side="bottom", fill="x")
        self.remote_text.pack(fill="both", expand=True)

        for widget in (self.local_text, self.remote_text):
            widget.tag_configure(self.TAG_ADDED, background="#264f78",
                                 foreground="#d4d4d4")
            widget.tag_configure(self.TAG_REMOVED, background="#5a1d1d",
                                 foreground="#d4d4d4")

        self._populate_texts(local_content, remote_content)
        self.local_text.configure(state="disabled")
        self.remote_text.configure(state="disabled")

        # === Мерж-инфо ===
        self.merge_info_label = ttk.Label(self, text="", foreground="#b06000",
                                           wraplength=860)
        self.merge_info_label.pack(fill="x", padx=8, pady=(0, 2))

        if local_status == "changed" and local_content and remote_content:
            mr = merge_text_contents(local_content, remote_content, file_path)
            if mr.success:
                self.merge_info_label.configure(
                    text=f"✅ Мерж возможен: +{mr.local_only_lines} локально, "
                         f"+{mr.remote_only_lines} из репо",
                    foreground="#006600",
                )
            else:
                self.merge_info_label.configure(
                    text=f"⚠ Конфликт: {'; '.join(mr.conflicts)}",
                    foreground="#cc0000",
                )

        # === Кнопки ===
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=8, pady=8)

        ttk.Button(
            btn_frame, text="← Отправить локальную в Репо",
            command=lambda: self._apply("local_to_remote"),
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_frame, text="Скачать из Репо → Локально",
            command=lambda: self._apply("remote_to_local"),
        ).pack(side="left", padx=4)

        if local_status == "changed":
            ttk.Button(
                btn_frame, text="🔀 Слить обе версии",
                command=lambda: self._apply("merge"),
            ).pack(side="left", padx=4)

        ttk.Button(
            btn_frame, text="Закрыть", command=self.destroy,
        ).pack(side="right", padx=4)

        self._syncing_scroll = False

    def _make_yscroll(self, source_widget, side):
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
        local_bin = local_content or b""
        remote_bin = remote_content or b""

        if is_binary(local_bin) or is_binary(remote_bin):
            self.local_text.insert("1.0",
                f"(Локально) Бинарный файл — {len(local_bin)} байт")
            self.remote_text.insert("1.0",
                f"(Репозиторий) Бинарный файл — {len(remote_bin)} байт")
            return

        local_lines = local_bin.decode("utf-8", errors="replace").splitlines()
        remote_lines = remote_bin.decode("utf-8", errors="replace").splitlines()

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

        matcher = difflib.SequenceMatcher(None, remote_lines, local_lines)
        local_out, remote_out = [], []
        local_tags, remote_tags = [], []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    line = remote_lines[i1 + k]
                    remote_out.append(line)
                    local_out.append(line)
            elif tag == "replace":
                for k in range(i1, i2):
                    remote_out.append(remote_lines[k])
                    remote_tags.append((len(remote_out), self.TAG_REMOVED))
                for k in range(j1, j2):
                    local_out.append(local_lines[k])
                    local_tags.append((len(local_out), self.TAG_ADDED))
                diff = (j2 - j1) - (i2 - i1)
                if diff > 0:
                    for _ in range(diff):
                        remote_out.append("")
                elif diff < 0:
                    for _ in range(-diff):
                        local_out.append("")
            elif tag == "delete":
                for k in range(i1, i2):
                    remote_out.append(remote_lines[k])
                    remote_tags.append((len(remote_out), self.TAG_REMOVED))
                    local_out.append("")
            elif tag == "insert":
                for k in range(j1, j2):
                    local_out.append(local_lines[k])
                    local_tags.append((len(local_out), self.TAG_ADDED))
                    remote_out.append("")

        self._fill_panel(self.local_text, local_out, local_tags)
        self._fill_panel(self.remote_text, remote_out, remote_tags)

    @staticmethod
    def _fill_panel(text_widget, lines, tags):
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

    def _setup_theme(self):
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

    def _create_ui(self):
        # === Настройки ===
        settings = ttk.LabelFrame(self, text=" Подключение ", padding=6)
        settings.pack(fill="x", padx=8, pady=(8, 4))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="🔑 Токен GitHub:").grid(
            row=0, column=0, sticky="w", padx=2, pady=2)
        self.token_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.token_var, width=65,
                   show="•").grid(row=0, column=1, columnspan=2,
                                  sticky="ew", padx=2, pady=2)

        ttk.Label(settings, text="📦 Репозиторий:").grid(
            row=1, column=0, sticky="w", padx=2, pady=2)
        self.repo_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.repo_var, width=65).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=2, pady=2)

        ttk.Label(settings, text="📁 Папка модов:").grid(
            row=2, column=0, sticky="w", padx=2, pady=2)
        self.folder_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.folder_var, width=55).grid(
            row=2, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(settings, text="📂 Обзор", width=8,
                    command=self._browse_folder).grid(
            row=2, column=2, padx=2, pady=2)

        ttk.Label(settings, text="🌿 Ветка:").grid(
            row=3, column=0, sticky="w", padx=2, pady=2)
        self.branch_var = tk.StringVar(value="main")
        ttk.Entry(settings, textvariable=self.branch_var, width=25).grid(
            row=3, column=1, sticky="w", padx=2, pady=2)

        # === Управление ===
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=8, pady=4)

        self.connect_btn = ttk.Button(ctrl, text="▶ Подключить", width=16,
                                       command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=4)

        self.sync_btn = ttk.Button(ctrl, text="🔄 Загрузить всё из репо",
                                    width=24, command=self._download_all,
                                    state="disabled")
        self.sync_btn.pack(side="left", padx=4)

        self.upload_all_btn = ttk.Button(
            ctrl, text="📤 Отправить всё в репо",
            width=24, command=self._upload_all, state="disabled")
        self.upload_all_btn.pack(side="left", padx=4)

        # Режимы
        modes = ttk.Frame(self)
        modes.pack(fill="x", padx=8, pady=2)

        self.auto_push_var = tk.BooleanVar()
        ttk.Checkbutton(
            modes, text="🔄 Авто-пуш (автоматическая синхронизация)",
            variable=self.auto_push_var,
            command=self._on_auto_push_toggle).pack(side="left", padx=8)

        self.filter_var = tk.BooleanVar()
        ttk.Checkbutton(
            modes, text="📋 Только .xml / .txt",
            variable=self.filter_var,
            command=self._on_filter_toggle).pack(side="left", padx=8)

        # === Список изменений ===
        list_frame = ttk.LabelFrame(
            self, text=" Изменённые файлы (двойной клик → diff) ", padding=4)
        list_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("path", "status", "direction")
        self.tree = ttk.Treeview(
            list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("path", text="Файл")
        self.tree.heading("status", text="Статус")
        self.tree.heading("direction", text="Направление")
        self.tree.column("path", width=360, minwidth=200)
        self.tree.column("status", width=130, minwidth=80)
        self.tree.column("direction", width=170, minwidth=100)

        vsb = ttk.Scrollbar(list_frame, orient="vertical",
                             command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)

        self.tree.tag_configure("local_only", background="#d4edda")
        self.tree.tag_configure("remote_only", background="#cce5ff")
        self.tree.tag_configure("changed", background="#f8d7da")
        self.tree.tag_configure("conflict", background="#ff4444",
                                 foreground="white")

        # === Прогресс-бар ===
        progress_frame = ttk.Frame(self)
        progress_frame.pack(fill="x", padx=8, pady=(0, 2))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var,
            maximum=100, mode="determinate")
        self.progress_bar.pack(fill="x", side="left", expand=True)

        self.progress_label = ttk.Label(progress_frame, text="", width=24,
                                         anchor="e")
        self.progress_label.pack(side="right", padx=(8, 0))

        # === Статус-бар ===
        self.status_var = tk.StringVar(
            value="Отключено. Укажите настройки и нажмите «Подключить».")
        ttk.Label(self, textvariable=self.status_var,
                   relief="sunken", anchor="w", padding=4).pack(
            fill="x", side="bottom", padx=8, pady=(0, 8))

    # ---- Прогресс ----

    def _update_progress(self, value, label=""):
        """Обновляет прогресс-бар и метку (thread-safe через after)."""
        self.after(0, lambda: self.progress_var.set(value))
        if label:
            self.after(0, lambda: self.progress_label.configure(text=label))

    def _set_status(self, text):
        """Обновляет статус-бар (thread-safe через after)."""
        self.after(0, lambda: self.status_var.set(text))

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
            messagebox.showerror("Ошибка", "Укажите токен GitHub.")
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
                        "Ошибка", "Не удалось подключиться."))
                    self.after(0, lambda: self.status_var.set(
                        "Ошибка подключения"))
                    return

                self.github = client
                if not os.path.isdir(folder):
                    os.makedirs(folder, exist_ok=True)

                exts = (self.config.extensions
                        if self.config.filter_extensions else None)
                self.scanner = FileScanner(folder, exts)
                self.connected = True
                repo_name = repo_info.get("full_name", "?")
                self.after(0, lambda: self._on_connected(repo_name, branch))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Ошибка подключения", str(e)))
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
                self._scan_after_id = self.after(
                    SCAN_INTERVAL_SEC * 1000, self._do_scan)
            return

        self.scanning = True

        def scan_worker():
            try:
                if self.scanner:
                    self.scanner.extensions_filter = (
                        self.config.extensions
                        if self.filter_var.get() else None)

                local_files = (self.scanner.scan_local()
                               if self.scanner else {})
                remote_tree, commit_sha = (
                    self.github.get_tree() if self.github else ({}, None))
                changes = FileScanner.compare(local_files, remote_tree)

                with self._lock:
                    self.local_files = local_files
                    self.remote_tree = remote_tree
                    self.changes = changes

                self.after(0, self._update_changes_list)

                if self.auto_push_var.get() and changes:
                    self.after(100, self._auto_push_changes)

            except Exception as e:
                self.after(0, lambda: self.status_var.set(
                    f"⚠ Ошибка сканирования: {e}"))
            finally:
                self.scanning = False
                if self.connected:
                    self._scan_after_id = self.after(
                        SCAN_INTERVAL_SEC * 1000, self._do_scan)

        threading.Thread(target=scan_worker, daemon=True).start()

    def _update_changes_list(self):
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
            "changed": "проверка мержа...",
        }

        for c in changes:
            path = c["path"]
            status = STATUS_TEXT.get(c["status"], c["status"])
            direction = DIR_TEXT.get(c["status"], "—")
            tag = c["status"]
            iid = self.tree.insert("", "end",
                                    values=(path, status, direction),
                                    tags=(tag,))
            if path == sel_path:
                self.tree.selection_set(iid)

        now = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            total_local = len(self.local_files)
            n_changes = len(self.changes)
        synced = max(0, total_local - n_changes)
        self.status_var.set(
            f"✅ Синхронизировано: {synced} | "
            f"Изменений: {n_changes} | Проверка: {now}")

    # ---- Двойной клик → Diff ----

    def _on_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not vals:
            return
        self._show_diff(str(vals[0]))

    def _show_diff(self, file_path):
        with self._lock:
            local_info = self.local_files.get(file_path)
            remote_info = self.remote_tree.get(file_path)

        if not local_info and not remote_info:
            return

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
                    remote_content, remote_sha = (
                        self.github.get_file_content(file_path))

                status = "changed"
                if local_info and not remote_info:
                    status = "local_only"
                elif remote_info and not local_info:
                    status = "remote_only"

                self.after(0, lambda: self._open_diff_window(
                    file_path, local_content, remote_content,
                    remote_sha, status))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Ошибка", str(e)))

        threading.Thread(target=fetch_and_show, daemon=True).start()

    def _open_diff_window(self, file_path, local_content, remote_content,
                           remote_sha, status):
        def on_apply(direction):
            self._apply_single_change(
                file_path, direction, local_content, remote_content,
                remote_sha)
        DiffWindow(self, file_path, local_content, remote_content,
                   status, on_apply)

    def _apply_single_change(self, file_path, direction, local_content,
                              remote_content, remote_sha):
        """Ручное применение одного изменения из DiffWindow."""
        try:
            local_path = os.path.join(
                self.config.local_folder,
                file_path.replace("/", os.sep))

            if direction == "local_to_remote":
                if local_content is None:
                    messagebox.showwarning("Внимание",
                                           "Нет локального содержимого.")
                    return
                self.status_var.set(f"Отправка {file_path} → репозиторий...")
                self.update_idletasks()

                # Проверяем свежий remote
                fresh_remote, fresh_sha = (
                    self.github.get_file_content(file_path))
                if fresh_remote and fresh_sha and fresh_sha != remote_sha:
                    mr = merge_text_contents(
                        local_content, fresh_remote, file_path)
                    if mr.success:
                        self.github.upload_file(
                            file_path, mr.content, existing_sha=fresh_sha)
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        with open(local_path, "wb") as f:
                            f.write(mr.content)
                        self.status_var.set(
                            f"✅ {file_path} — смержено и отправлено")
                    else:
                        messagebox.showwarning(
                            "Конфликт",
                            f"Remote изменился. Мерж невозможен:\n"
                            f"{'; '.join(mr.conflicts)}\n\n"
                            f"Откройте diff заново.")
                        self.status_var.set(f"⚠ Конфликт: {file_path}")
                        return
                else:
                    self.github.upload_file(
                        file_path, local_content, existing_sha=remote_sha)
                    self.status_var.set(
                        f"✅ {file_path} отправлен в репозиторий")

            elif direction == "remote_to_local":
                if remote_content is None:
                    messagebox.showwarning("Внимание",
                                           "Нет содержимого репозитория.")
                    return
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(remote_content)
                self.status_var.set(f"✅ {file_path} загружен локально")

            elif direction == "merge":
                if local_content is None or remote_content is None:
                    messagebox.showwarning("Внимание", "Нужны обе версии.")
                    return
                mr = merge_text_contents(
                    local_content, remote_content, file_path)
                if mr.success:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(mr.content)
                    self.github.upload_file(
                        file_path, mr.content, existing_sha=remote_sha)
                    self.status_var.set(
                        f"✅ {file_path} — смержено "
                        f"(+{mr.local_only_lines} локально, "
                        f"+{mr.remote_only_lines} из репо)")
                else:
                    messagebox.showwarning(
                        "Конфликт мержа",
                        f"Мерж невозможен:\n{'; '.join(mr.conflicts)}")
                    return

            self.after(1000, self._do_scan)

        except Exception as e:
            messagebox.showerror("Ошибка", str(e))
            self.status_var.set(f"⚠ Ошибка: {e}")

    # ================================================================
    # АВТО-ПУШ: batch-мерж + конфликт-диалог
    # ================================================================

    def _auto_push_changes(self):
        with self._lock:
            changes = list(self.changes)

        if not changes:
            return

        n_total = len(changes)
        self._update_progress(0, "Анализ...")
        self._set_status(
            f"🔄 Авто-синхронизация: {n_total} изменений...")

        def auto_worker():
            files_to_upload = {}
            files_to_download = {}
            conflict_list = []
            errors = []

            # === Фаза 1: анализ изменений ===
            for idx, change in enumerate(changes):
                if not self.connected:
                    break
                try:
                    path = change["path"]
                    status = change["status"]
                    short = os.path.basename(path)
                    pct = int((idx / max(n_total, 1)) * 40)
                    self._update_progress(pct, f"Анализ: {short}")
                    self._set_status(
                        f"🔄 Анализ: {short} ({idx + 1}/{n_total})")

                    if status == "local_only":
                        full_path = change.get("full_path") or os.path.join(
                            self.config.local_folder,
                            path.replace("/", os.sep))
                        if os.path.isfile(full_path):
                            with open(full_path, "rb") as f:
                                files_to_upload[path] = f.read()

                    elif status == "remote_only":
                        remote_content, remote_sha = (
                            self.github.get_file_content(path))
                        if remote_content:
                            files_to_download[path] = (
                                remote_content, remote_sha)

                    elif status == "changed":
                        local_path = os.path.join(
                            self.config.local_folder,
                            path.replace("/", os.sep))
                        local_bytes = b""
                        if os.path.isfile(local_path):
                            with open(local_path, "rb") as f:
                                local_bytes = f.read()

                        remote_content, remote_sha = (
                            self.github.get_file_content(path))
                        if remote_content is None:
                            continue

                        mr = merge_text_contents(
                            local_bytes, remote_content, path)

                        if mr.success:
                            files_to_upload[path] = mr.content
                        else:
                            conflict_list.append((
                                path, local_bytes,
                                remote_content, remote_sha))

                except Exception as e:
                    errors.append(f"{path}: {e}")

            # === Фаза 2: batch-коммит ===
            n_batch = len(files_to_upload)
            if files_to_upload:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = (f"Auto-sync: {n_batch} файл(ов) "
                           f"обновлено [{ts}]")

                def on_batch_progress(step_text, percent):
                    # Маппим 40-90% диапазон
                    mapped = 40 + int(percent * 0.5)
                    self._update_progress(mapped, step_text)
                    self._set_status(f"📤 {step_text} ({n_batch} файл(ов))")

                try:
                    ok, commit_sha = self.github.batch_commit(
                        files_to_upload, message=message,
                        progress_callback=on_batch_progress)
                    if ok:
                        for path, content in files_to_upload.items():
                            local_path = os.path.join(
                                self.config.local_folder,
                                path.replace("/", os.sep))
                            os.makedirs(os.path.dirname(local_path),
                                        exist_ok=True)
                            with open(local_path, "wb") as f:
                                f.write(content)
                except Exception as e:
                    errors.append(f"Batch commit: {e}")

            # === Фаза 3: скачиваем remote_only ===
            n_dl = len(files_to_download)
            if files_to_download:
                self._update_progress(92, f"Скачивание {n_dl} файл(ов)")
                self._set_status(
                    f"📥 Скачивание {n_dl} новых файлов...")
                dl_idx = 0
                for path, (content, sha) in files_to_download.items():
                    dl_idx += 1
                    short = os.path.basename(path)
                    self._update_progress(92, f"{short}")
                    self._set_status(
                        f"📥 {short} ({dl_idx}/{n_dl})")
                    try:
                        local_path = os.path.join(
                            self.config.local_folder,
                            path.replace("/", os.sep))
                        os.makedirs(os.path.dirname(local_path),
                                    exist_ok=True)
                        with open(local_path, "wb") as f:
                            f.write(content)
                    except Exception as e:
                        errors.append(f"Download {path}: {e}")

            # === Фаза 4: конфликты ===
            n_conflicts = len(conflict_list)
            if conflict_list:
                self.after(0, lambda: self._show_conflicts(conflict_list))
            else:
                msg = "✅ Синхронизация завершена"
                if n_batch:
                    msg += f" | Отправлено: {n_batch}"
                if n_dl:
                    msg += f" | Скачано: {n_dl}"
                if errors:
                    msg += f" | Ошибок: {len(errors)}"
                self._set_status(msg)
                self._update_progress(100, "✅ Готово")

            if not conflict_list:
                self.after(2000, self._do_scan)

        threading.Thread(target=auto_worker, daemon=True).start()

    def _show_conflicts(self, conflict_list):
        """Показывает конфликты по одному, каждый — отдельный коммит."""
        self._conflict_queue = list(conflict_list)
        self._process_next_conflict()

    def _process_next_conflict(self):
        if not self._conflict_queue:
            self.status_var.set(
                "✅ Все конфликты разрешены. Сканирование...")
            self.after(2000, self._do_scan)
            return

        item = self._conflict_queue.pop(0)
        path, local_content, remote_content, remote_sha = item

        def on_resolve(action):
            self._handle_conflict_action(
                path, action, local_content, remote_content, remote_sha)

        ConflictDialog(
            self, path, local_content, remote_content, remote_sha,
            on_resolve)

    def _handle_conflict_action(self, path, action, local_content,
                                 remote_content, remote_sha):
        """Обрабатывает решение пользователя по конфликту."""
        local_path = os.path.join(
            self.config.local_folder, path.replace("/", os.sep))

        try:
            if action == "push_local":
                # Пользователь хочет отправить свою версию в репо
                self.status_var.set(
                    f"Отправка {path} → репозиторий (отдельный коммит)...")
                self.update_idletasks()

                # Пробуем batch из одного файла для отдельного коммита
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ok, _ = self.github.batch_commit(
                    {path: local_content},
                    message=f"Conflict resolved (local): {path} [{ts}]")
                if ok:
                    # Обновляем локальный файл (repo = source of truth)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(local_content)
                    self.status_var.set(
                        f"✅ {path} — ваша версия отправлена в репо")
                else:
                    self.status_var.set(
                        f"⚠ Не удалось отправить {path}")

            elif action == "pull_remote":
                # Пользователь принимает remote версию, теряет локальные
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(remote_content)
                self.status_var.set(
                    f"✅ {path} — загружена версия из репозитория "
                    f"(ваши изменения потеряны)")

            elif action == "save_both":
                backup_path = save_conflict_files(
                    self.config.local_folder, path,
                    local_content, remote_content)
                self.status_var.set(
                    f"💾 {path}: репо → основной, ваш → "
                    f"{os.path.basename(backup_path)}")

            elif action == "skip":
                self.status_var.set(f"⏭ {path} — пропущен")

        except Exception as e:
            self.status_var.set(f"⚠ Ошибка для {path}: {e}")

        # Следующий конфликт
        self.after(500, self._process_next_conflict)

    # ---- Полная синхронизация ----

    def _download_all(self):
        if not self.connected or not self.github:
            return

        self._update_progress(0, "Подготовка...")
        self._set_status("📥 Загрузка всех файлов из репозитория...")

        def download_worker():
            try:
                remote_tree, _ = self.github.get_tree()
                total = len(remote_tree)
                count = 0
                processed = 0
                for path, info in remote_tree.items():
                    processed += 1
                    if self.filter_var.get():
                        ext = os.path.splitext(path)[1].lower()
                        if ext not in self.config.extensions:
                            continue

                    local_path = os.path.join(
                        self.config.local_folder,
                        path.replace("/", os.sep))

                    need_download = False
                    if not os.path.exists(local_path):
                        need_download = True
                    else:
                        with open(local_path, "rb") as f:
                            if compute_git_blob_hash(f.read()) != info["sha"]:
                                need_download = True

                    if need_download:
                        short = os.path.basename(path)
                        pct = int((processed / max(total, 1)) * 100)
                        self._update_progress(pct, f"{short}")
                        self._set_status(
                            f"📥 Скачивание: {short} "
                            f"({processed}/{total})")
                        content, _ = self.github.get_file_content(path)
                        if content:
                            os.makedirs(os.path.dirname(local_path),
                                        exist_ok=True)
                            with open(local_path, "wb") as f:
                                f.write(content)
                            count += 1

                self._set_status(f"✅ Загружено {count} файлов")
                self._update_progress(100, f"✅ {count} файл(ов)")
                self.after(1000, self._do_scan)

            except Exception as e:
                self._set_status(f"⚠ Ошибка загрузки: {e}")
                self._update_progress(0, "Ошибка")

        threading.Thread(target=download_worker, daemon=True).start()

    def _upload_all(self):
        if not self.connected or not self.github:
            return

        self._update_progress(0, "Подготовка...")
        self._set_status("📤 Отправка всех файлов в репозиторий...")

        def upload_worker():
            try:
                with self._lock:
                    local_files = dict(self.local_files)
                    remote_tree = dict(self.remote_tree)

                # Собираем файлы для отправки
                files_to_upload = {}
                total_all = len(local_files)
                idx = 0
                for path, info in local_files.items():
                    idx += 1
                    if self.filter_var.get():
                        ext = os.path.splitext(path)[1].lower()
                        if ext not in self.config.extensions:
                            continue

                    remote_sha = remote_tree.get(path, {}).get("sha")
                    if remote_sha and info["hash"] == remote_sha:
                        continue  # Уже синхронизирован

                    with open(info["full_path"], "rb") as f:
                        files_to_upload[path] = f.read()

                if not files_to_upload:
                    self._set_status("✅ Все файлы уже синхронизированы")
                    self._update_progress(100, "")
                    self.after(1000, self._do_scan)
                    return

                n_files = len(files_to_upload)
                self._set_status(
                    f"📤 Отправка {n_files} файл(ов) в репозиторий...")

                def on_progress(step_text, percent):
                    self._update_progress(percent, step_text)
                    self._set_status(
                        f"📤 {step_text}  ({n_files} файл(ов))")

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ok, _ = self.github.batch_commit(
                    files_to_upload,
                    message=(f"Upload all: {n_files} файл(ов) [{ts}]"),
                    progress_callback=on_progress)

                if ok:
                    self._set_status(
                        f"✅ Отправлено {n_files} файл(ов) — один коммит")
                    self._update_progress(100, f"✅ {n_files} файл(ов)")
                else:
                    self._set_status("⚠ Ошибка batch-коммита")
                    self._update_progress(0, "Ошибка")

                self.after(1000, self._do_scan)

            except Exception as e:
                self._set_status(f"⚠ Ошибка отправки: {e}")
                self._update_progress(0, "Ошибка")

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
        sys.exit(1)

    app = MainApp()
    try:
        icon_path = os.path.join(APP_DIR, "icon.ico")
        if os.path.exists(icon_path):
            app.iconbitmap(icon_path)
    except Exception:
        pass

    app.mainloop()


if __name__ == "__main__":
    main()
