"""Open the private local Web UI without a console window."""

from __future__ import annotations

import sys
import subprocess
import traceback
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = PROJECT_DIR / "src"
VENV_PYTHONW = PROJECT_DIR / ".venv" / "Scripts" / "pythonw.exe"

# Prefer the project-local environment when it exists.  This keeps the audio
# model and its native dependencies isolated from the user's system Python.
if VENV_PYTHONW.is_file() and Path(sys.executable).resolve() != VENV_PYTHONW.resolve():
    try:
        subprocess.Popen(
            [str(VENV_PYTHONW), str(Path(__file__).resolve())],
            cwd=str(PROJECT_DIR),
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        raise SystemExit(0)
    except OSError:
        # The regular startup path below will provide a useful error dialog if
        # the system interpreter also lacks a required dependency.
        pass

sys.path.insert(0, str(SOURCE_DIR))


def _show_native_error(message: str) -> None:
    details = PROJECT_DIR / "startup_error.txt"
    try:
        details.write_text(traceback.format_exc(), encoding="utf-8")
        suffix = f"\n\n详细信息已写入：\n{details}"
    except OSError:
        suffix = ""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            f"启动失败：{message}{suffix}",
            "谱净 · MIDI 标准化",
            0x10,
        )
    except Exception:
        pass


try:
    from midi_to_mscz.webui import main

    raise SystemExit(main())
except SystemExit:
    raise
except Exception as exc:
    _show_native_error(str(exc))
    raise SystemExit(1)
