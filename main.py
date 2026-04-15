#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitMergeMods v1.11
Легковесное приложение для синхронизации файлов модов с GitHub.
Работает через GitHub REST API — не требует установки Git.
Batch-коммиты, мерж по умолчанию, конфликт-диалог.
"""

import os
import sys
import io
import json
import hashlib
import base64
import shutil
import threading
import ssl
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from datetime import datetime
import difflib
import re
from dulwich import porcelain

try:
    import truststore
except ImportError:
    truststore = None

try:
    import certifi
except ImportError:
    certifi = None

# ============================================================
# Константы
# ============================================================

APP_NAME = "GitMergeMods"
APP_VERSION = "1.12"
SCAN_INTERVAL_SEC = 5
AUTO_CONNECT_RETRY_SEC = 10
API_BASE = "https://api.github.com"

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "gitmergemods_config.json")
LOG_FILE = os.path.join(APP_DIR, "gitmergemods_errors.log")
STATE_FILE = os.path.join(APP_DIR, "gitmergemods_state.json")
TRANSPORT_ROOT = os.path.join(APP_DIR, ".gitmergemods_transport")


# ============================================================
# Логирование
# ============================================================

def log_error(msg):
    """Записывает ошибку в лог-файл с таймстемпом."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


_SSL_CONTEXT = None
_SSL_CA_SOURCE = "default"
_SSL_CA_FILE = None


def get_ssl_context():
    """Возвращает SSL context с доверенными сертификатами.

    Приоритет:
    1. truststore — системное хранилище сертификатов Windows
    2. certifi — встроенный CA bundle внутри exe
    3. стандартный ssl context Python
    """
    global _SSL_CONTEXT, _SSL_CA_SOURCE, _SSL_CA_FILE

    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT

    if truststore is not None:
        try:
            truststore.inject_into_ssl()
            _SSL_CA_SOURCE = "truststore"
            _SSL_CONTEXT = ssl.create_default_context()
            return _SSL_CONTEXT
        except Exception as e:
            log_error(f"ssl truststore init: {e}")

    if certifi is not None:
        try:
            ca_file = certifi.where()
            if ca_file and os.path.isfile(ca_file):
                os.environ.setdefault("SSL_CERT_FILE", ca_file)
                os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_file)
                os.environ.setdefault("CURL_CA_BUNDLE", ca_file)
                _SSL_CA_SOURCE = "certifi"
                _SSL_CA_FILE = ca_file
                _SSL_CONTEXT = ssl.create_default_context(cafile=ca_file)
                return _SSL_CONTEXT
        except Exception as e:
            log_error(f"ssl certifi init: {e}")

    _SSL_CA_SOURCE = "default"
    _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def get_urllib3_pool_manager():
    """Создаёт urllib3 PoolManager с правильным SSL context для dulwich."""
    try:
        import urllib3
        return urllib3.PoolManager(ssl_context=get_ssl_context(), timeout=60)
    except Exception as e:
        log_error(f"urllib3 pool init: {e}")
        return None


# Инициализируем SSL до первых сетевых запросов.
get_ssl_context()


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


def encode_path_for_url(path: str) -> str:
    """Кодирует путь для GitHub API URL (кириллица и спецсимволы)."""
    parts = path.split("/")
    return "/".join(url_quote(p, safe="") for p in parts)


def _force_rmtree(path):
    """Удаляет дерево каталогов, обрабатывая read-only файлы (.git/objects).

    На Windows dulwich создаёт read-only файлы в .git/objects/pack/,
    из-за чего shutil.rmtree() молча не может их удалить (ignore_errors=True)
    и следующий clone падает с WinError 183.

    Стратегия: двухпроходная — сначала снимаем read-only со всех файлов,
    потом удаляем через shutil.rmtree.
    """
    import stat

    if not os.path.isdir(path):
        return

    # Проход 1: снимаем read-only со всех файлов и каталогов
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    os.chmod(fp, stat.S_IWRITE)
                except Exception:
                    pass
            for name in dirs:
                dp = os.path.join(root, name)
                try:
                    os.chmod(dp, stat.S_IWRITE)
                except Exception:
                    pass
        # Также снимаем с корня
        try:
            os.chmod(path, stat.S_IWRITE)
        except Exception:
            pass
    except Exception:
        pass

    # Проход 2: удаляем через rmtree с onerror-обработчиком
    def on_error(func, filepath, exc_info):
        try:
            os.chmod(filepath, stat.S_IWRITE)
            func(filepath)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=on_error)
    except Exception:
        pass

    # Проход 3 (страховочный): ручное удаление остатков
    if os.path.isdir(path):
        try:
            for root, dirs, files in os.walk(path, topdown=False):
                for f in files:
                    try:
                        fp = os.path.join(root, f)
                        os.chmod(fp, stat.S_IWRITE)
                        os.remove(fp)
                    except Exception:
                        pass
                for d in dirs:
                    try:
                        dp = os.path.join(root, d)
                        os.rmdir(dp)
                    except Exception:
                        pass
            try:
                os.rmdir(path)
            except Exception:
                pass
        except Exception:
            pass


# ============================================================
# Мерж текстовых файлов (3-way merge)
# ============================================================

class MergeResult:
    def __init__(self, success, content=None, conflicts=None,
                 local_only_lines=0, remote_only_lines=0):
        self.success = success
        self.content = content
        self.conflicts = conflicts or []
        self.local_only_lines = local_only_lines
        self.remote_only_lines = remote_only_lines


def three_way_merge(base_content, local_content, remote_content, path=""):
    """
    3-way merge: base — общая предковая версия.
    Сравниваем base→local и base→remote чтобы определить,
    какая сторона изменила каждый блок.
    
    Возвращает MergeResult:
    - success=True если нет конфликтов (auto-merge)
    - success=False если есть перекрывающиеся изменения
    """
    # Если local == base → только remote изменился → берём remote
    if local_content == base_content:
        return MergeResult(success=True, content=remote_content,
                           remote_only_lines=1)
    # Если remote == base → только local изменился → берём local
    if remote_content == base_content:
        return MergeResult(success=True, content=local_content,
                           local_only_lines=1)
    # Если local == remote → обе стороны одинаково изменили → берём любую
    if local_content == remote_content:
        return MergeResult(success=True, content=local_content)

    # Бинарные файлы
    if is_binary(local_content) or is_binary(remote_content) or is_binary(base_content):
        return MergeResult(
            success=False,
            conflicts=["Бинарный файл — автоматический мерж невозможен"])

    base_lines = base_content.decode("utf-8", errors="replace").splitlines()
    local_lines = local_content.decode("utf-8", errors="replace").splitlines()
    remote_lines = remote_content.decode("utf-8", errors="replace").splitlines()

    # Diff base→local и base→remote
    local_ops = list(difflib.SequenceMatcher(None, base_lines, local_lines).get_opcodes())
    remote_ops = list(difflib.SequenceMatcher(None, base_lines, remote_lines).get_opcodes())

    # Собираем changed regions: (base_start, base_end, new_lines, source)
    local_changes = []
    for tag, i1, i2, j1, j2 in local_ops:
        if tag != "equal":
            local_changes.append((i1, i2, local_lines[j1:j2], "local"))

    remote_changes = []
    for tag, i1, i2, j1, j2 in remote_ops:
        if tag != "equal":
            remote_changes.append((i1, i2, remote_lines[j1:j2], "remote"))

    # Проверяем перекрытия
    conflicts = []
    for ls, le, ll, _ in local_changes:
        for rs, re, rl, _ in remote_changes:
            # Диапазоны перекрываются?
            if ls < re and rs < le:
                # Проверяем: может быть обе стороны сделали ОДИНАКОВОЕ изменение
                if ll == rl:
                    continue  # Одинаковое изменение — не конфликт
                conflicts.append(
                    f"Строки {min(ls, rs) + 1}-{max(le, re)}: "
                    f"изменены с обеих сторон по-разному")

    if conflicts:
        # Конфликт — собираем с маркерами
        merged = _build_conflict_result(
            base_lines, local_changes, remote_changes)
        return MergeResult(
            success=False, content=merged, conflicts=conflicts,
            local_only_lines=sum(len(c[2]) for c in local_changes),
            remote_only_lines=sum(len(c[2]) for c in remote_changes))

    # Чистый мерж — объединяем все изменения
    merged = _build_clean_merge(base_lines, local_changes, remote_changes)

    result_text = "\n".join(merged)
    base_text = base_content.decode("utf-8", errors="replace")
    local_text = local_content.decode("utf-8", errors="replace")
    remote_text = remote_content.decode("utf-8", errors="replace")
    if base_text.endswith("\n") or local_text.endswith("\n") or remote_text.endswith("\n"):
        result_text += "\n"

    return MergeResult(
        success=True, content=result_text.encode("utf-8"),
        local_only_lines=sum(len(c[2]) for c in local_changes),
        remote_only_lines=sum(len(c[2]) for c in remote_changes))


def _build_clean_merge(base_lines, local_changes, remote_changes):
    """Собирает чистый мерж: все изменения с обеих сторон."""
    all_changes = sorted(
        local_changes + remote_changes,
        key=lambda c: (c[0], c[1]))

    merged = []
    pos = 0
    for start, end, new_lines, source in all_changes:
        # Добавляем неизменённые строки до этого изменения
        if start > pos:
            merged.extend(base_lines[pos:start])
        # Если уже обработали этот диапазон (дубликат от local и remote
        # с одинаковым результатом) — пропускаем
        if start < pos:
            continue
        merged.extend(new_lines)
        pos = end

    # Хвост
    if pos < len(base_lines):
        merged.extend(base_lines[pos:])

    return merged


def _build_conflict_result(base_lines, local_changes, remote_changes):
    """Собирает текст с маркерами конфликтов."""
    all_changes = sorted(
        local_changes + remote_changes,
        key=lambda c: (c[0], c[1]))

    merged = []
    pos = 0
    seen_ranges = set()

    for start, end, new_lines, source in all_changes:
        if start > pos:
            merged.extend(base_lines[pos:start])

        range_key = (start, end)
        if range_key in seen_ranges:
            # Вторая сторона того же диапазона — конфликт
            merged.append("=======")
            merged.extend(new_lines)
            merged.append(">>>>>>> ВЕРСИЯ РЕПОЗИТОРИЯ")
            pos = end
            continue

        seen_ranges.add(range_key)

        # Проверяем есть ли изменение с другой стороны в том же диапазоне
        has_other = any(
            s == start and e == end and src != source
            for s, e, _, src in (local_changes if source == "remote" else remote_changes)
        )

        if has_other:
            merged.append("<<<<<<< ЛОКАЛЬНАЯ ВЕРСИЯ")
            merged.extend(new_lines)
        else:
            merged.extend(new_lines)
            pos = end

    if pos < len(base_lines):
        merged.extend(base_lines[pos:])

    return "\n".join(merged).encode("utf-8")


def merge_text_contents(local_content, remote_content, path=""):
    """
    Простая 2-way merge (без базовой версии).
    Используется только когда базовая версия неизвестна.
    Для replace-блоков берёт LOCAL версию (приоритет пользователя).
    """
    if not local_content:
        return MergeResult(success=True, content=remote_content)
    if not remote_content:
        return MergeResult(success=True, content=local_content)

    if is_binary(local_content) or is_binary(remote_content):
        return MergeResult(
            success=False,
            conflicts=["Бинарный файл — автоматический мерж невозможен"])

    local_text = local_content.decode("utf-8", errors="replace")
    remote_text = remote_content.decode("utf-8", errors="replace")
    local_lines = local_text.splitlines()
    remote_lines = remote_text.splitlines()

    if local_lines == remote_lines:
        return MergeResult(success=True, content=local_content)

    merged = []
    local_added = 0
    remote_added = 0

    matcher = difflib.SequenceMatcher(None, remote_lines, local_lines)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            merged.extend(remote_lines[i1:i2])
        elif tag == "insert":
            # Строки добавлены локально
            merged.extend(local_lines[j1:j2])
            local_added += (j2 - j1)
        elif tag == "delete":
            # Строки есть в remote, нет в local — remote добавил
            merged.extend(remote_lines[i1:i2])
            remote_added += (i2 - i1)
        elif tag == "replace":
            # Разные версии блока — берём LOCAL (приоритет пользователя)
            merged.extend(local_lines[j1:j2])
            local_added += (j2 - j1)

    result_text = "\n".join(merged)
    if local_text.endswith("\n") or remote_text.endswith("\n"):
        result_text += "\n"
    result_bytes = result_text.encode("utf-8")

    return MergeResult(
        success=True, content=result_bytes,
        local_only_lines=local_added,
        remote_only_lines=remote_added)


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
        "auto_merge_update": True,
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
            with urlopen(req, timeout=30, context=get_ssl_context()) as resp:
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
            reason = getattr(e, "reason", e)
            reason_text = str(reason)
            if (isinstance(reason, ssl.SSLCertVerificationError)
                    or "CERTIFICATE_VERIFY_FAILED" in reason_text):
                details = "Сетевая ошибка SSL: не удалось проверить сертификат GitHub. "
                details += "Проверьте HTTPS-перехват антивирусом/прокси и корневые сертификаты Windows."
                if _SSL_CA_SOURCE == "certifi":
                    details += " Приложение использует встроенный CA bundle certifi."
                elif _SSL_CA_SOURCE == "truststore":
                    details += " Приложение использует системное хранилище сертификатов Windows."
                raise Exception(details)
            raise Exception(f"Сетевая ошибка: {reason}")
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
        encoded_path = encode_path_for_url(path)
        data = self._api_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}",
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

    def get_blob_content(self, blob_sha):
        """Получает содержимое blob по SHA."""
        blob = self._api_request(
            "GET", f"/repos/{self.owner}/{self.repo}/git/blobs/{blob_sha}"
        )
        if blob and "content" in blob:
            return base64.b64decode(blob["content"].replace("\n", ""))
        return None

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
        encoded_path = encode_path_for_url(path)
        result = self._api_request(
            "PUT",
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}",
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
    def compare(local_files, remote_tree, synced_state=None):
        synced_state = synced_state or {}
        all_paths = set(local_files.keys()) | set(remote_tree.keys())
        changes = []

        for path in sorted(all_paths):
            local = local_files.get(path)
            remote = remote_tree.get(path)
            synced_sha = synced_state.get(path)

            if local and not remote:
                changes.append({
                    "path": path,
                    "status": "remote_deleted" if synced_sha else "local_only",
                    "local_hash": local["hash"], "remote_sha": None,
                    "full_path": local["full_path"],
                    "synced_sha": synced_sha,
                })
            elif remote and not local:
                changes.append({
                    "path": path, "status": "remote_only",
                    "local_hash": None, "remote_sha": remote["sha"],
                    "full_path": path,
                })
            elif local["hash"] != remote["sha"]:
                if synced_sha:
                    if local["hash"] != synced_sha and remote["sha"] == synced_sha:
                        status = "local_changed"
                    elif local["hash"] == synced_sha and remote["sha"] != synced_sha:
                        status = "remote_changed"
                    elif local["hash"] != synced_sha and remote["sha"] != synced_sha:
                        status = "both_changed"
                    else:
                        status = "different_unknown"
                else:
                    status = "different_unknown"
                changes.append({
                    "path": path, "status": status,
                    "local_hash": local["hash"],
                    "remote_sha": remote["sha"],
                    "full_path": local["full_path"],
                    "synced_sha": synced_sha,
                })

        return changes


# ============================================================
# Окно конфликта (большой красный диалог)
# ============================================================

class ConflictDialog(tk.Toplevel):
    """Большой диалог для разрешения конфликта с красным предупреждением."""

    @staticmethod
    def _danger_button(parent, text, command):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#cc0000",
            fg="white",
            activebackground="#cc0000",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=8,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

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

        merge_label = tk.Label(
            info_frame,
            text="⚠ Конфликт изменений — автоматическое применение отключено",
            fg="#cc0000", font=("Segoe UI", 10, "bold"),
        )
        merge_label.pack(anchor="w", pady=(4, 0))

        # === Кнопки действий ===
        btn_frame = ttk.Frame(self, padding=8)
        btn_frame.pack(fill="x", padx=8, pady=8)

        self._danger_button(
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

        self._danger_button(
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

        self._danger_button(
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

        self._danger_button(
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
    TAG_LINE_ADDED = "line_added"
    TAG_LINE_REMOVED = "line_removed"
    TAG_CHAR_ADDED = "char_added"
    TAG_CHAR_REMOVED = "char_removed"

    @staticmethod
    def _make_action_button(parent, text, command, bg, fg="white"):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            padx=10,
            pady=6,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )

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
        self.local_content = local_content or b""
        self.remote_content = remote_content or b""
        self.local_status = local_status

        # === Информация ===
        info_frame = ttk.Frame(self)
        info_frame.pack(fill="x", padx=8, pady=(8, 4))

        status_text = {
            "local_only": "📄 Только локально",
            "remote_only": "☁️ Только в репозитории",
            "remote_deleted": "🗑 Удален из репозитория",
            "local_changed": "📝 Изменён локально",
            "remote_changed": "☁️ Изменён в репозитории",
            "both_changed": "⚠ Изменён локально и в репозитории",
            "different_unknown": "⚡ Версии различаются",
            "conflict": "⛔ Конфликт изменений",
        }.get(local_status, local_status)

        ttk.Label(
            info_frame, text=f"📁 {file_path}",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(info_frame, text=status_text, foreground="#555").pack(anchor="w")

        self.only_changes_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            info_frame,
            text="Показывать только изменения",
            variable=self.only_changes_var,
            command=self._rerender_texts,
        ).pack(anchor="w", pady=(4, 0))

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
            widget.tag_configure(self.TAG_LINE_ADDED, background="#1f5d2f",
                                 foreground="#ffffff")
            widget.tag_configure(self.TAG_LINE_REMOVED, background="#8b5a00",
                                 foreground="#ffffff")
            widget.tag_configure(self.TAG_CHAR_ADDED, background="#2e8b57",
                                 foreground="#ffffff")
            widget.tag_configure(self.TAG_CHAR_REMOVED, background="#cc8400",
                                 foreground="#ffffff")

        self._rerender_texts()
        self.local_text.configure(state="disabled")
        self.remote_text.configure(state="disabled")

        # === Мерж-инфо ===
        self.merge_info_label = ttk.Label(self, text="", foreground="#b06000",
                                           wraplength=860)
        self.merge_info_label.pack(fill="x", padx=8, pady=(0, 2))

        if local_status in ("local_changed", "remote_changed", "both_changed", "different_unknown") and local_content and remote_content:
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

        merge_possible = False
        if local_content is not None and remote_content is not None and local_status != "remote_deleted":
            mr = merge_text_contents(local_content, remote_content, file_path)
            merge_possible = mr.success

        GREEN = "#2e8b57"
        RED = "#cc0000"
        GREY = "#666666"

        local_color = GREY
        remote_color = GREY
        merge_color = GREY

        if local_status in ("local_only", "local_changed"):
            local_color = GREEN
            merge_color = GREEN
        elif local_status in ("remote_only", "remote_changed"):
            remote_color = GREEN
            merge_color = GREEN
        elif local_status == "remote_deleted":
            local_color = GREEN
            remote_color = RED
        elif local_status == "both_changed":
            if merge_possible:
                merge_color = GREEN
            else:
                local_color = RED
                remote_color = RED
                merge_color = RED
        elif local_status == "different_unknown":
            if merge_possible:
                merge_color = GREEN
            else:
                local_color = RED
                remote_color = RED
                merge_color = RED
        elif local_status == "conflict":
            local_color = RED
            remote_color = RED
            merge_color = RED

        left_button_text = "← Отправить локальную в Репо"
        right_button_text = "Скачать из Репо → Локально"
        right_button_action = "remote_to_local"
        show_merge_button = local_status in (
            "local_changed", "remote_changed", "both_changed",
            "different_unknown", "local_only", "remote_only"
        )

        if local_status == "remote_deleted":
            left_button_text = "📤 Отправить в репозиторий"
            right_button_text = "🗑 Удалить локальный"
            right_button_action = "delete_local"
            show_merge_button = False

        self._make_action_button(
            btn_frame,
            left_button_text,
            lambda: self._apply("local_to_remote"),
            local_color,
        ).pack(side="left", padx=4)

        self._make_action_button(
            btn_frame,
            right_button_text,
            lambda: self._apply(right_button_action),
            remote_color,
        ).pack(side="left", padx=4)

        if show_merge_button:
            self._make_action_button(
                btn_frame,
                "🔀 Слить обе версии",
                lambda: self._apply("merge"),
                merge_color,
            ).pack(side="left", padx=4)

        tk.Button(
            btn_frame,
            text="Закрыть",
            command=self.destroy,
            bg="#444444",
            fg="white",
            activebackground="#444444",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
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

    def _rerender_texts(self):
        self.local_text.configure(state="normal")
        self.remote_text.configure(state="normal")
        self.local_text.delete("1.0", "end")
        self.remote_text.delete("1.0", "end")
        self._populate_texts(self.local_content, self.remote_content)
        self.local_text.configure(state="disabled")
        self.remote_text.configure(state="disabled")

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
        local_char_tags, remote_char_tags = [], []
        show_only = self.only_changes_var.get()

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                if not show_only:
                    for k in range(i2 - i1):
                        line = remote_lines[i1 + k]
                        remote_out.append(line)
                        local_out.append(line)
            elif tag == "replace":
                max_len = max(i2 - i1, j2 - j1)
                for idx in range(max_len):
                    remote_line = remote_lines[i1 + idx] if i1 + idx < i2 else ""
                    local_line = local_lines[j1 + idx] if j1 + idx < j2 else ""
                    remote_out.append(remote_line)
                    local_out.append(local_line)
                    local_tags.append((len(local_out), self.TAG_LINE_ADDED))
                    remote_tags.append((len(remote_out), self.TAG_LINE_REMOVED))

                    char_ops = difflib.SequenceMatcher(None, remote_line, local_line).get_opcodes()
                    line_no = len(local_out)
                    for ctag, ci1, ci2, cj1, cj2 in char_ops:
                        if ctag in ("replace", "insert") and local_line:
                            local_char_tags.append((line_no, cj1, cj2, self.TAG_CHAR_ADDED))
                        if ctag in ("replace", "delete") and remote_line:
                            remote_char_tags.append((line_no, ci1, ci2, self.TAG_CHAR_REMOVED))
            elif tag == "delete":
                for k in range(i1, i2):
                    remote_out.append(remote_lines[k])
                    remote_tags.append((len(remote_out), self.TAG_LINE_REMOVED))
                    local_out.append("")
            elif tag == "insert":
                for k in range(j1, j2):
                    local_out.append(local_lines[k])
                    local_tags.append((len(local_out), self.TAG_LINE_ADDED))
                    remote_out.append("")

        self._fill_panel(self.local_text, local_out, local_tags, local_char_tags)
        self._fill_panel(self.remote_text, remote_out, remote_tags, remote_char_tags)

    @staticmethod
    def _fill_panel(text_widget, lines, tags, char_tags=None):
        content = "\n".join(lines)
        text_widget.insert("1.0", content)
        for line_no, tag_name in tags:
            text_widget.tag_add(tag_name, f"{line_no}.0", f"{line_no}.end")
        for line_no, start_col, end_col, tag_name in (char_tags or []):
            if end_col > start_col:
                text_widget.tag_add(tag_name, f"{line_no}.{start_col}", f"{line_no}.{end_col}")

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
        self._operation_active = False
        self.changes = []
        self.local_files = {}
        self.remote_tree = {}
        self._synced_state = {}  # path → SHA (последняя синхронизированная версия)
        self._scan_after_id = None
        self._connect_retry_after_id = None
        self._connect_in_progress = False
        self._auto_connect_enabled = True
        self._lock = threading.Lock()

        self._setup_theme()
        self._create_ui()
        self._load_config_to_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._startup_auto_connect)

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

        self.auto_merge_update_var = tk.BooleanVar()
        ttk.Checkbutton(
            modes, text="🧩 Авто мерж-обновление",
            variable=self.auto_merge_update_var,
            command=self._on_auto_merge_toggle).pack(side="left", padx=8)

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
        self.tree.tag_configure("remote_deleted", background="#ffe0b3")
        self.tree.tag_configure("local_changed", background="#d4edda")
        self.tree.tag_configure("remote_changed", background="#cce5ff")
        self.tree.tag_configure("both_changed", background="#f8d7da")
        self.tree.tag_configure("different_unknown", background="#fff3cd")
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

    def _get_state_key(self):
        folder = normalize_path(os.path.abspath(self.config.local_folder or ""))
        repo = self.config.repo_url or ""
        branch = self.config.branch or "main"
        return f"{repo}|{branch}|{folder}"

    def _load_synced_state_from_disk(self):
        try:
            if not os.path.exists(STATE_FILE):
                return {}
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(self._get_state_key(), {})
        except Exception as e:
            log_error(f"load_synced_state: {e}")
            return {}

    def _save_synced_state_to_disk(self):
        try:
            data = {}
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data[self._get_state_key()] = self._synced_state
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log_error(f"save_synced_state: {e}")

    def _get_transport_repo_path(self):
        key = hashlib.sha1(
            f"{self.config.repo_url}|{self.config.branch}".encode("utf-8")
        ).hexdigest()[:16]
        return os.path.join(TRANSPORT_ROOT, key)

    def _build_auth_repo_url(self):
        owner, repo = GitHubClient._parse_repo_url(self.config.repo_url)
        token = url_quote(self.config.token, safe="")
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    @staticmethod
    def _dulwich_status_has_changes(status_obj):
        staged = getattr(status_obj, "staged", {}) or {}
        staged_changed = any(bool(v) for v in staged.values())
        unstaged_changed = bool(getattr(status_obj, "unstaged", []))
        untracked_changed = bool(getattr(status_obj, "untracked", []))
        return staged_changed or unstaged_changed or untracked_changed

    def _ensure_transport_repo(self, progress_callback=None):
        def report(text, percent):
            if progress_callback:
                progress_callback(text, percent)

        repo_path = self._get_transport_repo_path()
        auth_url = self._build_auth_repo_url()
        os.makedirs(TRANSPORT_ROOT, exist_ok=True)

        def fresh_clone():
            if os.path.exists(repo_path):
                _force_rmtree(repo_path)
            report("Клонирование transport-репозитория...", 3)
            pool = get_urllib3_pool_manager()
            clone_kwargs = dict(
                checkout=True,
                branch=self.config.branch,
                outstream=io.BytesIO(),
                errstream=io.BytesIO(),
            )
            if pool is not None:
                clone_kwargs["pool_manager"] = pool
            porcelain.clone(auth_url, target=repo_path, **clone_kwargs)

        if not self.connected:
            raise Exception("Отключено — transport clone отменён")

        git_dir = os.path.join(repo_path, ".git")
        # Если репо отсутствует или shallow (от предыдущей версии) — полный clone
        shallow_marker = os.path.join(git_dir, "shallow")
        if not os.path.isdir(git_dir) or os.path.isfile(shallow_marker):
            fresh_clone()
            return repo_path

        try:
            status_obj = porcelain.status(repo_path)
            if self._dulwich_status_has_changes(status_obj):
                report("Очистка transport-репозитория...", 2)
                fresh_clone()
                return repo_path

            report("Обновление transport-репозитория...", 5)
            pool = get_urllib3_pool_manager()
            pull_kwargs = dict(
                remote_location=auth_url,
                outstream=io.BytesIO(),
                errstream=io.BytesIO(),
                fast_forward=True,
                force=True,
            )
            if pool is not None:
                pull_kwargs["pool_manager"] = pool
            porcelain.pull(repo_path, **pull_kwargs)
            return repo_path
        except Exception as e:
            if not self.connected:
                raise
            log_error(f"transport pull failed, re-cloning: {e}")
            fresh_clone()
            return repo_path

    def _transport_batch_push(self, files_to_upload, files_to_delete=None,
                              message=None, progress_callback=None):
        def report(text, percent):
            if progress_callback:
                progress_callback(text, percent)

        files_to_delete = files_to_delete or {}
        repo_path = self._ensure_transport_repo(progress_callback=progress_callback)
        auth_url = self._build_auth_repo_url()
        changed_paths = []
        delete_paths = list(files_to_delete.keys())

        total_upload = len(files_to_upload)
        for idx, (path, content) in enumerate(files_to_upload.items()):
            pct = 10 + int((idx / max(total_upload, 1)) * 35)
            report(f"Подготовка: {os.path.basename(path)} ({idx + 1}/{total_upload})", pct)
            abs_path = os.path.join(repo_path, path.replace("/", os.sep))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(content)
            changed_paths.append(path)

        if changed_paths:
            report("Индексирование файлов...", 50)
            porcelain.add(repo_path, paths=changed_paths)

        if delete_paths:
            report("Подготовка удалений...", 55)
            porcelain.remove(repo_path, paths=delete_paths)

        status_obj = porcelain.status(repo_path)
        if not self._dulwich_status_has_changes(status_obj):
            report("Изменений для push нет", 100)
            return True, None

        report("Создание локального коммита...", 70)
        commit_id = porcelain.commit(repo_path, message=message)

        report("Отправка pack в GitHub...", 85)
        pool = get_urllib3_pool_manager()
        push_kwargs = dict(
            remote_location=auth_url,
            refspecs=f"refs/heads/{self.config.branch}:refs/heads/{self.config.branch}",
            outstream=io.BytesIO(),
            errstream=io.BytesIO(),
        )
        if pool is not None:
            push_kwargs["pool_manager"] = pool

        try:
            result = porcelain.push(repo_path, **push_kwargs)
        except Exception as push_err:
            err_text = str(push_err)
            log_error(f"push attempt 1 failed: {err_text}")

            # Non-fast-forward — pull remote, rebase, и retry push
            if "main" in err_text or "fast" in err_text.lower() or "reject" in err_text.lower():
                report("Pull перед retry push...", 88)
                try:
                    pull_kwargs = dict(
                        remote_location=auth_url,
                        outstream=io.BytesIO(),
                        errstream=io.BytesIO(),
                        fast_forward=True,
                        force=True,
                    )
                    if pool is not None:
                        pull_kwargs["pool_manager"] = pool
                    porcelain.pull(repo_path, **pull_kwargs)
                    # Повторный commit после pull (merge)
                    report("Повторный commit...", 90)
                    commit_id = porcelain.commit(repo_path, message=message)
                    report("Retry push...", 92)
                    result = porcelain.push(repo_path, **push_kwargs)
                    log_error("push retry succeeded after pull")
                except Exception as retry_err:
                    log_error(f"push retry also failed: {retry_err}")
                    _force_rmtree(repo_path)
                    raise Exception(f"Push не удался после retry: {retry_err}")
            else:
                _force_rmtree(repo_path)
                raise

        # dulwich porcelain.push возвращает dict {ref: error_or_None}
        # или объект с ref_status
        if isinstance(result, dict):
            failures = {k: v for k, v in result.items() if v}
        else:
            ref_status = getattr(result, "ref_status", None) or {}
            failures = {k: v for k, v in ref_status.items() if v}

        if failures:
            log_error(f"push rejected: {failures}")
            _force_rmtree(repo_path)
            raise Exception(f"Push отклонен: {failures}")

        report("Готово", 100)
        return True, commit_id

    # ---- Конфигурация UI ----

    def _load_config_to_ui(self):
        self.token_var.set(self.config.token)
        self.repo_var.set(self.config.repo_url)
        self.folder_var.set(self.config.local_folder)
        self.branch_var.set(self.config.branch)
        self.auto_push_var.set(self.config.auto_push)
        self.auto_merge_update_var.set(self.config.auto_merge_update)
        self.filter_var.set(self.config.filter_extensions)

    def _save_config_from_ui(self):
        self.config.token = self.token_var.get().strip()
        self.config.repo_url = self.repo_var.get().strip()
        self.config.local_folder = self.folder_var.get().strip()
        self.config.branch = self.branch_var.get().strip() or "main"
        self.config.auto_push = self.auto_push_var.get()
        self.config.auto_merge_update = self.auto_merge_update_var.get()
        self.config.filter_extensions = self.filter_var.get()
        self.config.save()

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку модов")
        if folder:
            self.folder_var.set(folder)

    def _has_connection_data(self):
        token = self.token_var.get().strip()
        repo_url = self.repo_var.get().strip()
        folder = self.folder_var.get().strip()
        return bool(token and repo_url and folder)

    def _cancel_connect_retry(self):
        if self._connect_retry_after_id:
            self.after_cancel(self._connect_retry_after_id)
            self._connect_retry_after_id = None

    def _schedule_connect_retry(self):
        self._cancel_connect_retry()
        if not self._auto_connect_enabled or self.connected or self._connect_in_progress:
            return
        if not self._has_connection_data():
            return
        self._set_status(
            f"⚠ Подключение не удалось. Повтор через {AUTO_CONNECT_RETRY_SEC} сек..."
        )
        self._connect_retry_after_id = self.after(
            AUTO_CONNECT_RETRY_SEC * 1000,
            self._startup_auto_connect,
        )

    def _startup_auto_connect(self):
        if not self._auto_connect_enabled or self.connected or self._connect_in_progress:
            return
        if not self._has_connection_data():
            return
        self._connect(show_errors=False, schedule_retry=True)

    # ---- Подключение ----

    def _toggle_connection(self):
        if self.connected:
            self._disconnect()
        else:
            self._auto_connect_enabled = True
            self._connect(show_errors=True, schedule_retry=False)

    def _connect(self, show_errors=True, schedule_retry=False):
        if self.connected or self._connect_in_progress:
            return

        self._save_config_from_ui()
        token = self.config.token
        repo_url = self.config.repo_url
        folder = self.config.local_folder
        branch = self.config.branch

        if not token:
            if show_errors:
                messagebox.showerror("Ошибка", "Укажите токен GitHub.")
            return
        if not repo_url:
            if show_errors:
                messagebox.showerror("Ошибка", "Укажите URL репозитория.")
            return
        if not folder:
            if show_errors:
                messagebox.showerror("Ошибка", "Укажите локальную папку.")
            return

        self._cancel_connect_retry()
        self._connect_in_progress = True
        self.status_var.set("Подключение...")
        self.update_idletasks()

        def connect_worker():
            try:
                client = GitHubClient(token, repo_url, branch)
                repo_info = client.test_connection()
                if not repo_info:
                    if show_errors:
                        self.after(0, lambda: messagebox.showerror(
                            "Ошибка", "Не удалось подключиться."))
                    self.after(0, lambda: self.status_var.set(
                        "Ошибка подключения"))
                    log_error("connect: test_connection вернул пустой результат")
                    if schedule_retry:
                        self.after(0, self._schedule_connect_retry)
                    return

                self.github = client
                if not os.path.isdir(folder):
                    os.makedirs(folder, exist_ok=True)

                exts = (self.config.extensions
                        if self.config.filter_extensions else None)
                self.scanner = FileScanner(folder, exts)
                self.connected = True
                repo_name = repo_info.get("full_name", "?")
                self.after(0, self._cancel_connect_retry)
                self.after(0, lambda: self._on_connected(repo_name, branch))
            except Exception as e:
                log_error(f"connect: {e}")
                if show_errors:
                    self.after(0, lambda: messagebox.showerror(
                        "Ошибка подключения", str(e)))
                self.after(0, lambda: self.status_var.set(f"Ошибка: {e}"))
                if schedule_retry:
                    self.after(0, self._schedule_connect_retry)
            finally:
                self._connect_in_progress = False

        threading.Thread(target=connect_worker, daemon=True).start()

    def _on_connected(self, repo_name, branch):
        self.connect_btn.configure(text="⏹ Отключить")
        self.sync_btn.configure(state="normal")
        self.upload_all_btn.configure(state="normal")
        self.status_var.set(f"✅ Подключено к {repo_name} ({branch})")
        # Удаляем устаревшие transport-кэши от других репо/веток
        self._cleanup_stale_transport()
        # Загружаем baseline из локального manifest; если его нет —
        # инициализируем текущим состоянием репозитория.
        if self.github:
            tree, _ = self.github.get_tree()
            loaded_state = self._load_synced_state_from_disk()
            with self._lock:
                self._synced_state = loaded_state or {
                    p: info["sha"] for p, info in tree.items()
                }
            if not loaded_state:
                self._save_synced_state_to_disk()
        self._start_scanning()

    def _disconnect(self):
        self._auto_connect_enabled = False
        self._cancel_connect_retry()
        self.connected = False
        self._operation_active = False
        self.scanning = False
        if self._scan_after_id:
            self.after_cancel(self._scan_after_id)
            self._scan_after_id = None
        self.connect_btn.configure(text="▶ Подключить")
        self.sync_btn.configure(state="disabled")
        self.upload_all_btn.configure(state="disabled")
        self.progress_var.set(0)
        self.progress_label.configure(text="")
        self.status_var.set("Отключено")
        self.tree.delete(*self.tree.get_children())
        self.changes = []
        self.local_files = {}
        self.remote_tree = {}
        with self._lock:
            self._synced_state = {}
        self.github = None
        self.scanner = None
        self._cleanup_transport()

    def _cleanup_stale_transport(self):
        """Удаляет transport-кэши от других репозиториев/веток,
        оставляя только текущий."""
        try:
            if not os.path.isdir(TRANSPORT_ROOT):
                return
            current_key = hashlib.sha1(
                f"{self.config.repo_url}|{self.config.branch}".encode("utf-8")
            ).hexdigest()[:16]
            for entry in os.listdir(TRANSPORT_ROOT):
                entry_path = os.path.join(TRANSPORT_ROOT, entry)
                if os.path.isdir(entry_path) and entry != current_key:
                    _force_rmtree(entry_path)
        except Exception as e:
            log_error(f"cleanup_stale_transport: {e}")

    def _cleanup_transport(self):
        """Удаляет transport-кэш (.gitmergemods_transport) если он существует."""
        try:
            if os.path.isdir(TRANSPORT_ROOT):
                _force_rmtree(TRANSPORT_ROOT)
        except Exception as e:
            log_error(f"cleanup_transport: {e}")

    def _on_close(self):
        """Обработчик закрытия окна — останавливает потоки и чистит кэш."""
        self._disconnect()
        self._cleanup_transport()
        self.destroy()

    # ---- Сканирование ----

    def _start_scanning(self):
        if self.connected:
            self._scan_after_id = self.after(0, self._do_scan)

    def _do_scan(self):
        if not self.connected or self.scanning or self._operation_active:
            if self.connected and not self._operation_active:
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
                if not self.connected:
                    return
                with self._lock:
                    synced_state = dict(self._synced_state)
                changes = FileScanner.compare(local_files, remote_tree, synced_state)

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
            "remote_deleted": "Удален из репозитория",
            "local_changed": "Изменён локально",
            "remote_changed": "Изменён в репозитории",
            "both_changed": "Изменён с обеих сторон",
            "different_unknown": "Версии различаются",
        }
        DIR_TEXT = {
            "local_only": "локальн. → репо",
            "remote_only": "репо → локальн.",
            "remote_deleted": "выбор: вернуть / удалить локально",
            "local_changed": "локальн. → репо",
            "remote_changed": "репо → локальн.",
            "both_changed": "нужен мерж / выбор",
            "different_unknown": "источник не определён",
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

    def _build_merge_result(self, file_path, file_status, local_content, remote_content):
        """Возвращает результат мержа с учётом baseline для both_changed."""
        if local_content is None or remote_content is None:
            return None

        if file_status == "both_changed":
            with self._lock:
                base_sha = self._synced_state.get(file_path)
            base_content = self.github.get_blob_content(base_sha) if (self.github and base_sha) else None
            if base_content is not None:
                return three_way_merge(base_content, local_content, remote_content, file_path)

        return merge_text_contents(local_content, remote_content, file_path)

    def _handle_non_conflict_direct_sync(self, file_path, file_status, local_content, remote_content, remote_sha):
        """Для неконфликтных файлов синхронизирует сразу без окна."""
        if file_status in ("local_only", "local_changed"):
            self._apply_single_change(file_path, "local_to_remote", local_content, remote_content, remote_sha)
            return True

        if file_status in ("remote_only", "remote_changed"):
            self._apply_single_change(file_path, "remote_to_local", local_content, remote_content, remote_sha)
            return True

        if file_status in ("both_changed", "different_unknown"):
            mr = self._build_merge_result(file_path, file_status, local_content, remote_content)
            if mr and mr.success:
                self._apply_single_change(file_path, "merge", local_content, remote_content, remote_sha)
                return True

        return False

    # ---- Двойной клик → Diff ----

    def _on_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not vals:
            return
        tags = self.tree.item(sel[0], "tags")
        status_tag = tags[0] if tags else "different_unknown"
        self._show_diff(str(vals[0]), status_tag)

    def _show_diff(self, file_path, file_status="different_unknown"):
        with self._lock:
            local_info = self.local_files.get(file_path)
            remote_info = self.remote_tree.get(file_path)

        # Если не нашли в кэше — пробуем построить путь напрямую
        if not local_info:
            direct_path = os.path.join(
                self.config.local_folder,
                file_path.replace("/", os.sep))
            if os.path.isfile(direct_path):
                try:
                    with open(direct_path, "rb") as f:
                        content = f.read()
                    local_info = {
                        "hash": compute_git_blob_hash(content),
                        "full_path": direct_path,
                        "size": len(content),
                    }
                except Exception:
                    pass

        if not remote_info and self.remote_tree:
            # Попробуем найти по частичному совпадению
            for rp, rv in self.remote_tree.items():
                if rp == file_path or rp.endswith(file_path):
                    remote_info = rv
                    break

        if not local_info and not remote_info:
            log_error(f"show_diff: файл не найден ни локально, ни в репо: {file_path}")
            self._set_status(f"⚠ Файл не найден: {file_path}")
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

                next_action = None
                if file_status in ("local_only", "local_changed"):
                    next_action = "local_to_remote"
                elif file_status in ("remote_only", "remote_changed"):
                    next_action = "remote_to_local"
                elif file_status in ("both_changed", "different_unknown"):
                    mr = self._build_merge_result(
                        file_path, file_status, local_content, remote_content)
                    next_action = "merge" if (mr and mr.success) else "conflict"
                elif file_status == "remote_deleted":
                    next_action = "remote_deleted"
                else:
                    next_action = "conflict"

                def decide_action():
                    if next_action == "local_to_remote":
                        self._apply_single_change(file_path, "local_to_remote", local_content, remote_content, remote_sha)
                    elif next_action == "remote_to_local":
                        self._apply_single_change(file_path, "remote_to_local", local_content, remote_content, remote_sha)
                    elif next_action == "merge":
                        self._apply_single_change(file_path, "merge", local_content, remote_content, remote_sha)
                    elif next_action == "remote_deleted":
                        self._open_diff_window(
                            file_path, local_content, remote_content,
                            remote_sha, "remote_deleted")
                    else:
                        # Окно вызывается только для конфликтов
                        self._open_diff_window(
                            file_path, local_content, remote_content,
                            remote_sha, "conflict")

                self.after(0, decide_action)
            except Exception as e:
                log_error(f"show_diff [{file_path}]: {e}")
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
        if self._operation_active:
            messagebox.showwarning("Подождите",
                "Другая операция ещё выполняется. Дождитесь завершения.")
            return

        self._operation_active = True
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
                    mr = self._build_merge_result(
                        file_path, "both_changed", local_content, fresh_remote)
                    if mr.success:
                        self.github.upload_file(
                            file_path, mr.content, existing_sha=fresh_sha)
                        tree, _ = self.github.get_tree()
                        with self._lock:
                            new_sha = tree.get(file_path, {}).get("sha")
                            if new_sha:
                                self._synced_state[file_path] = new_sha
                        self._save_synced_state_to_disk()
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
                    tree, _ = self.github.get_tree()
                    with self._lock:
                        new_sha = tree.get(file_path, {}).get("sha")
                        if new_sha:
                            self._synced_state[file_path] = new_sha
                    self._save_synced_state_to_disk()
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
                with self._lock:
                    if remote_sha:
                        self._synced_state[file_path] = remote_sha
                self._save_synced_state_to_disk()
                self.status_var.set(f"✅ {file_path} загружен локально")

            elif direction == "delete_local":
                if os.path.exists(local_path):
                    os.remove(local_path)
                with self._lock:
                    self._synced_state.pop(file_path, None)
                self._save_synced_state_to_disk()
                self.status_var.set(f"✅ {file_path} удален локально")

            elif direction == "merge":
                if local_content is None or remote_content is None:
                    messagebox.showwarning("Внимание", "Нужны обе версии.")
                    return
                with self._lock:
                    base_sha = self._synced_state.get(file_path)
                mr = self._build_merge_result(
                    file_path,
                    "both_changed" if base_sha else "different_unknown",
                    local_content,
                    remote_content,
                )
                if mr.success:
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(mr.content)
                    self.github.upload_file(
                        file_path, mr.content, existing_sha=remote_sha)
                    tree, _ = self.github.get_tree()
                    with self._lock:
                        new_sha = tree.get(file_path, {}).get("sha")
                        if new_sha:
                            self._synced_state[file_path] = new_sha
                    self._save_synced_state_to_disk()
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
            log_error(f"apply_single [{file_path}] dir={direction}: {e}")
            messagebox.showerror("Ошибка", str(e))
            self.status_var.set(f"⚠ Ошибка: {e}")
        finally:
            self._operation_active = False

    # ================================================================
    # АВТО-ПУШ: batch-мерж + конфликт-диалог
    # ================================================================

    def _auto_push_changes(self):
        with self._lock:
            changes = list(self.changes)

        if not changes:
            return

        if self._operation_active:
            return  # Другая операция ещё идёт

        self._operation_active = True
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

                    if status in ("local_only", "local_changed"):
                        full_path = change.get("full_path") or os.path.join(
                            self.config.local_folder,
                            path.replace("/", os.sep))
                        if os.path.isfile(full_path):
                            with open(full_path, "rb") as f:
                                files_to_upload[path] = f.read()

                    elif status in ("remote_only", "remote_changed"):
                        remote_content, remote_sha = (
                            self.github.get_file_content(path))
                        if remote_content:
                            files_to_download[path] = (
                                remote_content, remote_sha)

                    elif status in ("both_changed", "different_unknown"):
                        if not self.auto_merge_update_var.get():
                            continue

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

                        mr = self._build_merge_result(
                            path, status, local_bytes, remote_content)

                        if mr and mr.success:
                            files_to_upload[path] = mr.content
                        else:
                            conflict_list.append((
                                path, local_bytes,
                                remote_content, remote_sha))

                except Exception as e:
                    errors.append(f"{path}: {e}")
                    log_error(f"auto_push analyze [{path}]: {e}")

            # === Фаза 2: batch-коммит ===
            n_batch = len(files_to_upload)
            if not self.connected:
                return
            if files_to_upload:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                message = (f"Auto-sync: {n_batch} файл(ов) "
                           f"обновлено [{ts}]")

                def on_batch_progress(step_text, percent):
                    mapped = 40 + int(percent * 0.5)
                    self._update_progress(mapped, step_text)
                    self._set_status(f"📤 {step_text} ({n_batch} файл(ов))")

                try:
                    ok, commit_sha = self._transport_batch_push(
                        files_to_upload, message=message,
                        progress_callback=on_batch_progress)
                    if ok:
                        # Обновляем synced_state для отправленных файлов
                        new_tree, _ = self.github.get_tree()
                        with self._lock:
                            for path, content in files_to_upload.items():
                                new_sha = new_tree.get(path, {}).get("sha")
                                if new_sha:
                                    self._synced_state[path] = new_sha
                                # Обновляем локальные файлы
                                local_path = os.path.join(
                                    self.config.local_folder,
                                    path.replace("/", os.sep))
                                os.makedirs(os.path.dirname(local_path),
                                            exist_ok=True)
                                with open(local_path, "wb") as f:
                                    f.write(content)
                        self._save_synced_state_to_disk()
                    else:
                        log_error(f"auto_push: transport push вернул False для {n_batch} файлов")
                except Exception as e:
                    errors.append(f"Transport push: {e}")
                    log_error(f"auto_push transport_push: {e}")

            # === Фаза 3: скачиваем remote_only ===
            if not self.connected:
                return
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
                        # Обновляем synced_state
                        with self._lock:
                            self._synced_state[path] = sha
                        self._save_synced_state_to_disk()
                    except Exception as e:
                        errors.append(f"Download {path}: {e}")
                        log_error(f"auto_push download [{path}]: {e}")

            # === Фаза 4: конфликты ===
            if conflict_list:
                self.after(0, lambda: self._show_conflicts(conflict_list))
                # Не снимаем _operation_active — конфликты разберёт пользователь
                # _operation_active снимется в _process_next_conflict когда очередь пуста
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
                self._operation_active = False
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
            self._operation_active = False
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
                ok, _ = self._transport_batch_push(
                    {path: local_content},
                    message=f"Conflict resolved (local): {path} [{ts}]")
                if ok:
                    tree, _ = self.github.get_tree()
                    with self._lock:
                        new_sha = tree.get(path, {}).get("sha")
                        if new_sha:
                            self._synced_state[path] = new_sha
                    self._save_synced_state_to_disk()
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
                with self._lock:
                    if remote_sha:
                        self._synced_state[path] = remote_sha
                self._save_synced_state_to_disk()
                self.status_var.set(
                    f"✅ {path} — загружена версия из репозитория "
                    f"(ваши изменения потеряны)")

            elif action == "save_both":
                backup_path = save_conflict_files(
                    self.config.local_folder, path,
                    local_content, remote_content)
                with self._lock:
                    if remote_sha:
                        self._synced_state[path] = remote_sha
                self._save_synced_state_to_disk()
                self.status_var.set(
                    f"💾 {path}: репо → основной, ваш → "
                    f"{os.path.basename(backup_path)}")

            elif action == "skip":
                self.status_var.set(f"⏭ {path} — пропущен")

        except Exception as e:
            log_error(f"conflict_action [{path}] action={action}: {e}")
            self.status_var.set(f"⚠ Ошибка для {path}: {e}")

        # Следующий конфликт
        self.after(500, self._process_next_conflict)

    # ---- Полная синхронизация ----

    def _download_all(self):
        if not self.connected or not self.github or self._operation_active:
            return

        self._operation_active = True
        self._update_progress(0, "Подготовка...")
        self._set_status("📥 Загрузка всех файлов из репозитория...")

        def download_worker():
            try:
                remote_tree, _ = self.github.get_tree()
                total = len(remote_tree)
                count = 0
                processed = 0
                for path, info in remote_tree.items():
                    if not self.connected:
                        return
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
                with self._lock:
                    self._synced_state = {p: info["sha"] for p, info in remote_tree.items()}
                self._save_synced_state_to_disk()
                self.after(1000, self._do_scan)

            except Exception as e:
                log_error(f"download_all: {e}")
                self._set_status(f"⚠ Ошибка загрузки: {e}")
                self._update_progress(0, "Ошибка")
            finally:
                self._operation_active = False

        threading.Thread(target=download_worker, daemon=True).start()

    def _upload_all(self):
        if not self.connected or not self.github or self._operation_active:
            return

        self._operation_active = True
        self._update_progress(0, "Подготовка...")
        self._set_status("📤 Отправка всех файлов в репозиторий...")

        def upload_worker():
            try:
                with self._lock:
                    local_files = dict(self.local_files)
                    remote_tree = dict(self.remote_tree)

                # Собираем файлы для отправки
                files_to_upload = {}
                for path, info in local_files.items():
                    if self.filter_var.get():
                        ext = os.path.splitext(path)[1].lower()
                        if ext not in self.config.extensions:
                            continue

                    remote_sha = remote_tree.get(path, {}).get("sha")
                    if remote_sha and info["hash"] == remote_sha:
                        continue  # Уже синхронизирован

                    with open(info["full_path"], "rb") as f:
                        files_to_upload[path] = f.read()

                if not self.connected:
                    self._set_status("Отключено")
                    return
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
                ok, _ = self._transport_batch_push(
                    files_to_upload,
                    message=(f"Upload all: {n_files} файл(ов) [{ts}]"),
                    progress_callback=on_progress)

                if ok:
                    tree, _ = self.github.get_tree()
                    with self._lock:
                        self._synced_state = {p: info["sha"] for p, info in tree.items()}
                    self._save_synced_state_to_disk()
                    self._set_status(
                        f"✅ Отправлено {n_files} файл(ов) — один коммит")
                    self._update_progress(100, f"✅ {n_files} файл(ов)")
                else:
                    self._set_status("⚠ Ошибка transport push")
                    self._update_progress(0, "Ошибка")
                    log_error(f"upload_all: transport push вернул False для {n_files} файлов")

                self.after(1000, self._do_scan)

            except Exception as e:
                log_error(f"upload_all: {e}")
                self._set_status(f"⚠ Ошибка отправки: {e}")
                self._update_progress(0, "Ошибка")
            finally:
                self._operation_active = False

        threading.Thread(target=upload_worker, daemon=True).start()

    # ---- Обработчики галочек ----

    def _on_auto_push_toggle(self):
        self.config.auto_push = self.auto_push_var.get()
        self.config.save()

    def _on_auto_merge_toggle(self):
        self.config.auto_merge_update = self.auto_merge_update_var.get()
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
