"""Small helpers: HTTPS downloads with retry, checksums, safe extraction, atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import SetupError
from .logging_config import current_log_file, get_logger

USER_AGENT = "frappe-setup-cli (+https://github.com/frappe/frappe)"
DOWNLOAD_TIMEOUT = 60


def _open(url: str, timeout: float) -> Any:
    if not url.startswith("https://"):
        raise SetupError(f"Refusing non-HTTPS download: {url}", phase="download")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https enforced
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https enforced above


def fetch_bytes(url: str, *, phase: str, retries: int = 3, timeout: float = DOWNLOAD_TIMEOUT) -> bytes:
    log = get_logger(phase)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            log.info("GET %s", url)
            with _open(url, timeout) as response:
                return bytes(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            log.warning("download failed (%s/%s): %s", attempt + 1, retries + 1, exc)
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                break
            time.sleep(min(2 ** (attempt + 1), 20))
    raise _download_error(url, phase, last)


def fetch_json(url: str, *, phase: str) -> Any:
    try:
        return json.loads(fetch_bytes(url, phase=phase))
    except json.JSONDecodeError as exc:
        raise SetupError(f"Invalid JSON from {url}", phase=phase, retry_safe=True) from exc


def download(url: str, dest: Path, *, phase: str, retries: int = 3, timeout: float = DOWNLOAD_TIMEOUT) -> Path:
    """Stream to a temporary file next to dest, then rename (no partial files left behind)."""
    log = get_logger(phase)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries + 1):
        fd, tmp = tempfile.mkstemp(prefix=".part-", dir=dest.parent)
        try:
            log.info("GET %s -> %s", url, dest)
            with _open(url, timeout) as response, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(response, out, length=1024 * 1024)
            os.replace(tmp, dest)
            return dest
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            log.warning("download failed (%s/%s): %s", attempt + 1, retries + 1, exc)
            with _suppress():
                os.unlink(tmp)
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                break
            time.sleep(min(2 ** (attempt + 1), 20))
    raise _download_error(url, phase, last)


def _download_error(url: str, phase: str, exc: Exception | None) -> SetupError:
    log = current_log_file()
    return SetupError(
        f"Download failed: {url}",
        phase=phase,
        cause=f"Network error: {exc}" if exc else "Network error",
        remedy="Check the internet connection or HTTPS proxy (https_proxy) and rerun; retrying is safe.",
        retry_safe=True,
        log_file=str(log) if log else None,
    )


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_tar(archive: Path, dest: Path) -> None:
    """Extract without allowing absolute paths, '..', or links pointing outside dest."""
    dest = dest.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise SetupError(f"Unsafe path in archive {archive.name}: {member.name}", phase="extract")
            if member.issym() or member.islnk():
                link_target = (
                    (target.parent / member.linkname).resolve()
                    if member.issym()
                    else (dest / member.linkname).resolve()
                )
                if not str(link_target).startswith(str(dest) + os.sep):
                    raise SetupError(f"Unsafe link in archive {archive.name}: {member.name}", phase="extract")
            if member.isdev():
                raise SetupError(f"Device file in archive {archive.name}: {member.name}", phase="extract")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # pragma: no cover - Python < 3.12 without the backported filter
            tar.extractall(dest)  # noqa: S202 - members validated above


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with _suppress():
            os.unlink(tmp)
        raise


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"
