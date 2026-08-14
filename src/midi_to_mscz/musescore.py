"""MuseScore command-line integration.

MuseScore's ``.mscz`` format is intentionally treated as an output format here.
The converter writes standards-compliant MusicXML first and lets MuseScore do
the final import.  This is substantially less brittle than constructing MSCX
internals ourselves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable


class MuseScoreError(RuntimeError):
    """Raised when MuseScore cannot create or validate the requested score."""


_WINDOWS_CANDIDATES = (
    Path(r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe"),
    Path(r"C:\Program Files\MuseScore 4\bin\mscore.exe"),
    Path(r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe"),
    Path(r"C:\Program Files\MuseScore 3\bin\mscore.exe"),
)


def find_musescore(configured_path: str | Path | None = None) -> Path:
    """Return a usable MuseScore executable or raise :class:`MuseScoreError`."""

    checked: list[Path] = []
    if configured_path:
        candidate = Path(configured_path).expanduser()
        checked.append(candidate)
        if candidate.is_file():
            return candidate.resolve()

    env_path = os.environ.get("MUSESCORE_PATH")
    if env_path:
        candidate = Path(env_path).expanduser()
        checked.append(candidate)
        if candidate.is_file():
            return candidate.resolve()

    for command in ("MuseScore4", "mscore4", "MuseScore3", "mscore3", "mscore"):
        resolved = shutil.which(command)
        if resolved:
            return Path(resolved).resolve()

    if os.name == "nt":
        for candidate in _WINDOWS_CANDIDATES:
            checked.append(candidate)
            if candidate.is_file():
                return candidate.resolve()

    locations = "\n".join(f"  - {path}" for path in checked)
    raise MuseScoreError(
        "没有找到 MuseScore。请安装 MuseScore 4，或在设置中选择 "
        "MuseScore4.exe。\n已检查：\n" + (locations or "  - 系统 PATH")
    )


def validate_mscz(path: str | Path) -> Path:
    """Perform a cheap structural validation of an MSCZ container."""

    score_path = Path(path)
    if not score_path.is_file() or score_path.stat().st_size < 100:
        raise MuseScoreError(f"MuseScore 没有生成有效文件：{score_path}")
    try:
        with zipfile.ZipFile(score_path) as archive:
            names = archive.namelist()
            score_entries = [name for name in names if name.lower().endswith(".mscx")]
            if not score_entries:
                raise MuseScoreError("生成的 MSCZ 中没有主乐谱文件（.mscx）。")
            header = archive.read(score_entries[0], pwd=None)[:1024]
            if b"<museScore" not in header:
                raise MuseScoreError("生成的 MSCZ 主文件结构异常。")
    except zipfile.BadZipFile as exc:
        raise MuseScoreError("生成的文件不是有效的 MSCZ/ZIP 容器。") from exc
    return score_path


def export_musicxml_to_mscz(
    musicxml_path: str | Path,
    output_path: str | Path,
    *,
    musescore_path: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    timeout: float = 180.0,
) -> Path:
    """Convert ``musicxml_path`` to ``output_path`` atomically with MuseScore.

    MuseScore may print non-fatal audio/network warnings on stderr.  Therefore
    success is determined by both its return code and the generated container.
    The destination is replaced only after that validation succeeds.
    """

    source = Path(musicxml_path).resolve()
    destination = Path(output_path).resolve()
    if not source.is_file():
        raise MuseScoreError(f"找不到中间 MusicXML：{source}")
    if destination.suffix.lower() != ".mscz":
        destination = destination.with_suffix(".mscz")
    destination.parent.mkdir(parents=True, exist_ok=True)
    executable = find_musescore(musescore_path)

    if progress:
        progress(f"正在调用 MuseScore：{executable.name}")

    temporary_dir = Path(
        tempfile.mkdtemp(prefix="midi-to-mscz-", dir=str(destination.parent))
    )
    temporary_output = temporary_dir / destination.name
    command = [str(executable), "-o", str(temporary_output), str(source)]
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise MuseScoreError(
                f"MuseScore 转换超过 {timeout:g} 秒，已停止。中间文件保留在：{source}"
            ) from exc

        if completed.returncode != 0 or not temporary_output.exists():
            details = (completed.stderr or completed.stdout or "无错误详情").strip()
            # Keep the user-facing error useful without dumping megabytes of logs.
            if len(details) > 2000:
                details = details[-2000:]
            raise MuseScoreError(
                f"MuseScore 转换失败（退出码 {completed.returncode}）。\n{details}"
            )

        validate_mscz(temporary_output)
        os.replace(temporary_output, destination)
        validate_mscz(destination)
        if progress:
            progress(f"MSCZ 已生成：{destination}")
        return destination
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


__all__ = [
    "MuseScoreError",
    "export_musicxml_to_mscz",
    "find_musescore",
    "validate_mscz",
]
