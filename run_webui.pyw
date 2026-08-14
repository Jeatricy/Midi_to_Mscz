"""Open the private local Web UI without a console window."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = PROJECT_DIR / "src"
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
