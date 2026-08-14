"""Private local Web UI for the MIDI-to-MuseScore converter.

The UI deliberately uses only the Python standard library.  It listens on a
random loopback port, requires a per-launch token, stores uploads in a private
temporary directory, and exposes completed scores only long enough for the
browser to write them into a user-approved local directory.
"""

from __future__ import annotations

import dataclasses
import email.policy
import hashlib
import importlib.util
import inspect
import json
import math
import mimetypes
import re
import secrets
import shutil
import tempfile
import threading
import time
import webbrowser
from email.parser import BytesParser
from fractions import Fraction
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .models import ConversionSettings, InputSpec
from .pipeline import convert


ASSET_DIR = Path(__file__).with_name("web")
MAX_REQUEST_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_AUDIO_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_AUDIO_BYTES = 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_FILES = 8
MIDI_SUFFIXES = {".mid", ".midi"}
AUDIO_SUFFIXES = {".flac", ".wav", ".mp3", ".ogg"}
SAFE_OUTPUT_RE = re.compile(r"[^\w\- .()\u3000-\u9fff]+", re.UNICODE)
Converter = Callable[..., Any]


def _json_safe(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字。") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间。")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    result = _number(value, name, minimum, maximum)
    if result != int(result):
        raise ValueError(f"{name} 必须是整数。")
    return int(result)


def _safe_output_name(value: Any) -> str:
    raw = Path(str(value or "标准化乐谱.mscz")).name
    stem = Path(raw).stem.strip().rstrip(".") or "标准化乐谱"
    stem = SAFE_OUTPUT_RE.sub("_", stem)[:100].strip() or "标准化乐谱"
    return f"{stem}.mscz"


def _audio_capabilities() -> dict[str, Any]:
    """Return lightweight local-backend status without loading a model."""

    demucs_available = importlib.util.find_spec("demucs") is not None
    result: dict[str, Any] = {
        "available": importlib.util.find_spec("midi_to_mscz.audio_verify") is not None,
        "dsp_available": True,
        "model_available": importlib.util.find_spec("basic_pitch") is not None,
        "model_name": "Spotify Basic Pitch",
        "demucs_available": demucs_available,
        "source_separation_available": demucs_available,
        "source_separation_mode": "auto",
    }
    try:
        from . import audio_verify

        provider = next(
            (
                getattr(audio_verify, name, None)
                for name in ("get_backend_status", "model_capabilities", "capabilities")
                if callable(getattr(audio_verify, name, None))
            ),
            None,
        )
        if provider is not None:
            supplied = provider()
            if dataclasses.is_dataclass(supplied) and not isinstance(supplied, type):
                supplied = dataclasses.asdict(supplied)
            if isinstance(supplied, Mapping):
                result.update(_json_safe(supplied))
    except Exception as exc:
        result["detail"] = str(exc)
    return result


def _valid_audio_header(suffix: str, header: bytes) -> bool:
    if suffix == ".flac":
        return header.startswith(b"fLaC")
    if suffix == ".wav":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WAVE"
    if suffix == ".ogg":
        return header.startswith(b"OggS")
    if suffix == ".mp3":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    return False


def _input_spec_with_audio_role(**values: Any) -> InputSpec:
    """Pass the optional per-stem audio role when supported by the model."""

    role = values.pop("audio_role", "auto")
    try:
        parameters = inspect.signature(InputSpec).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "audio_role" in parameters:
        values["audio_role"] = role
    return InputSpec(**values)


def _settings_from_payload(data: Mapping[str, Any]) -> ConversionSettings:
    bpm_mode = str(data.get("bpm_mode", "auto"))
    bpm = None if bpm_mode == "auto" else _number(data.get("bpm"), "BPM", 20, 400)

    meter_mode = str(data.get("meter_mode", "auto"))
    meter: str | None
    if meter_mode == "auto":
        meter = None
    else:
        meter = str(data.get("time_signature", "4/4")).strip()
        if not re.fullmatch(r"(?:[1-9]|[12]\d|3[0-2])/(?:1|2|4|8|16|32)", meter):
            raise ValueError("拍号格式应为 4/4、3/4、6/8 等。")

    key_mode = str(data.get("key_mode", "auto"))
    key_fifths = None if key_mode == "auto" else _integer(
        data.get("key_fifths", 0), "调号", -7, 7
    )
    smallest_note = _integer(data.get("smallest_note", 16), "最小音符", 8, 32)
    if smallest_note not in {8, 16, 32}:
        raise ValueError("最小音符只能选择八分、十六分或三十二分音符。")
    pickup_beats = _number(data.get("pickup_beats", 0), "弱起拍长度", 0, 32)

    return ConversionSettings(
        bpm=bpm,
        time_signature=meter,
        key_fifths=key_fifths,
        smallest_note=smallest_note,
        auto_latency=bool(data.get("auto_latency", True)),
        auto_trim=bool(data.get("auto_trim", True)),
        detect_arpeggios=bool(data.get("detect_arpeggios", True)),
        detect_triplets=bool(data.get("detect_tuplets", True)),
        detect_grace_notes=bool(data.get("detect_grace_notes", True)),
        detect_swing=bool(data.get("detect_swing", True)),
        detect_ornaments=bool(data.get("detect_ornaments", True)),
        pickup=pickup_beats > 0,
        pickup_beats=pickup_beats,
        title=str(data.get("title", "")).strip()[:160],
        composer=str(data.get("composer", "")).strip()[:160],
    )


def _specs_from_payload(
    rows: Any,
    uploaded: Mapping[str, tuple[str, bytes]],
    destination: Path,
) -> list[InputSpec]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise ValueError(f"请添加 1 到 {MAX_FILES} 个 MIDI 文件。")
    specs: list[InputSpec] = []
    staff_counts = {"treble": 0, "bass": 0}
    destination.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError("文件设置格式不正确。")
        upload_id = str(row.get("upload_id", ""))
        if upload_id not in uploaded:
            raise ValueError(f"第 {index + 1} 个 MIDI 没有成功上传。")
        original_name, content = uploaded[upload_id]
        suffix = Path(original_name).suffix.lower()
        if suffix not in MIDI_SUFFIXES:
            raise ValueError(f"“{original_name}”不是 .mid 或 .midi 文件。")
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"“{original_name}”超过 32 MB。")
        if len(content) < 14 or not content.startswith(b"MThd"):
            raise ValueError(f"“{original_name}”不是有效的标准 MIDI 文件。")
        staff = str(row.get("staff", "treble"))
        if staff not in staff_counts:
            raise ValueError("谱表只能选择高音谱表或低音谱表。")
        staff_counts[staff] += 1
        if staff_counts[staff] > 4:
            label = "高音" if staff == "treble" else "低音"
            raise ValueError(f"{label}谱表最多放 4 个独立 MIDI 声部。")
        audio_role = str(row.get("audio_role", "auto")).strip().lower()
        if audio_role not in {"auto", "vocals", "accompaniment", "mix"}:
            raise ValueError("复核声源只能选择自动、人声、伴奏或完整混音。")

        upload_path = destination / f"input-{index + 1}{suffix}"
        upload_path.write_bytes(content)
        specs.append(
            _input_spec_with_audio_role(
                path=upload_path,
                staff=staff,
                transpose_octaves=_integer(row.get("octaves", 0), "八度调整", -3, 3),
                transpose_semitones=_integer(row.get("semitones", 0), "半音调整", -11, 11),
                velocity_min=_integer(row.get("velocity_min", 1), "最低力度", 1, 127),
                offset_beats=_number(row.get("offset_beats", 0), "偏移拍", -128, 128),
                audio_role=audio_role,
            )
        )
    return specs


def _multipart_fields(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("上传格式不正确。")
    envelope = (
        b"MIME-Version: 1.0\r\nContent-Type: "
        + content_type.encode("ascii", "strict")
        + b"\r\n\r\n"
        + body
    )
    message = BytesParser(policy=email.policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("无法读取上传内容。")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            fields[str(name)] = payload.decode(part.get_content_charset() or "utf-8")
        else:
            files[str(name)] = (Path(filename).name, payload)
    return fields, files


def _midi_pitch_name(pitch: Any) -> str | None:
    try:
        midi_pitch = int(pitch)
    except (TypeError, ValueError):
        return None
    if not 0 <= midi_pitch <= 127:
        return None
    names = ("C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B")
    return f"{names[midi_pitch % 12]}{midi_pitch // 12 - 1}"


def _report_payload(
    report: Any,
    output_name: str,
    source_names: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    def attr(name: str, default: Any = None) -> Any:
        return getattr(report, name, default)

    audio_result = attr("audio_verification", attr("audio_result", None))

    def audio_attr(*names: str, default: Any = None) -> Any:
        for source in (report, audio_result):
            if source is None:
                continue
            for name in names:
                if isinstance(source, Mapping) and name in source:
                    return source[name]
                if hasattr(source, name):
                    return getattr(source, name)
        return default

    notation_review = list(attr("low_confidence_measures", []) or [])
    audio_review = list(
        audio_attr("audio_review_measures", "review_measures", "flagged_measures", default=[])
        or []
    )
    if not audio_review:
        review_items = audio_attr("audio_review_items", "review_items", default=[]) or []
        audio_review = [
            item.get("measure") if isinstance(item, Mapping) else getattr(item, "measure", None)
            for item in review_items
        ]
        audio_review = [measure for measure in audio_review if measure is not None]
    review_measures = list(dict.fromkeys(notation_review + audio_review))

    raw_review_items = list(
        audio_attr("audio_review_items", "review_items", default=[]) or []
    )

    def item_attr(item: Any, name: str, default: Any = None) -> Any:
        if isinstance(item, Mapping):
            return item.get(name, default)
        return getattr(item, name, default)

    def review_confidence(item: Any) -> float:
        try:
            confidence = float(item_attr(item, "confidence", 0.0))
        except (TypeError, ValueError):
            return 0.0
        return confidence if math.isfinite(confidence) else 0.0

    serialized_review_items: list[dict[str, Any]] = []
    for item in sorted(raw_review_items, key=review_confidence, reverse=True)[:100]:
        kind = str(item_attr(item, "kind", "review"))
        evidence = item_attr(item, "evidence", None)
        evidence_decision = item_attr(
            evidence,
            "decision",
            item_attr(item, "decision", None),
        )
        if kind == "possible_missing":
            decision = "missing"
        elif evidence_decision == "remove":
            decision = "remove"
        else:
            decision = "review"
        source_index = item_attr(item, "source_index", None)
        try:
            source_key = int(source_index) if source_index is not None else None
        except (TypeError, ValueError):
            source_key = None
        source_path = item_attr(item, "source_path", None)
        source_name = (
            (source_names or {}).get(source_key)
            if source_key is not None
            else None
        ) or (Path(source_path).name if source_path else "未知 MIDI")
        pitch = item_attr(item, "pitch", None)
        original_pitch = item_attr(item, "original_pitch", None)
        serialized_review_items.append(
            {
                "kind": kind,
                "measure": item_attr(item, "measure", None),
                "source": source_name,
                "source_index": source_key,
                "pitch": pitch,
                "pitch_name": _midi_pitch_name(pitch),
                "original_pitch": original_pitch,
                "original_pitch_name": _midi_pitch_name(original_pitch),
                "start_beat": item_attr(item, "start_beat", None),
                "audio_start": item_attr(item, "audio_start_seconds", None),
                "confidence": review_confidence(item),
                "message": str(item_attr(item, "message", "请对照原始音频复核。"))[:1000],
                "decision": decision,
            }
        )

    file_rows = []
    for item in attr("files", []) or []:
        file_rows.append(
            {
                "name": Path(getattr(item, "path", "MIDI")).name,
                "staff": getattr(item, "staff", ""),
                "notes_seen": getattr(item, "notes_seen", 0),
                "notes_kept": getattr(item, "notes_kept", 0),
                "velocity_filtered": getattr(item, "velocity_filtered", 0),
                "audio_notes_checked": getattr(item, "audio_notes_checked", 0),
                "audio_notes_removed": getattr(item, "audio_notes_removed", 0),
                "audio_possible_extra": getattr(item, "audio_possible_extra", 0),
                "audio_possible_missing": getattr(item, "audio_possible_missing", 0),
            }
        )
    return _json_safe(
        {
            "output_name": output_name,
            "notes_seen": attr("notes_seen", 0),
            "notes_kept": attr("notes_kept", attr("final_notes", 0)),
            "final_notes": attr("final_notes", 0),
            "velocity_filtered": attr("velocity_filtered", 0),
            "arpeggio_count": attr("arpeggio_count", 0),
            "tuplet_count": attr("tuplet_count", attr("triplet_count", 0)),
            "grace_count": attr("grace_count", 0),
            "ornament_count": attr("ornament_count", 0),
            "pedal_tails_trimmed": attr("pedal_tails_trimmed", 0),
            "review_measures": review_measures,
            "audio_review_measures": audio_review,
            "audio_review_items": serialized_review_items,
            "audio_review_item_count": len(raw_review_items),
            "audio_review_items_truncated": len(raw_review_items) > 100,
            "audio_review_enabled": bool(
                audio_attr(
                    "audio_verification_enabled",
                    "audio_review_enabled",
                    "enabled",
                    "performed",
                    default=False,
                )
            ),
            "audio_review_mode": audio_attr("audio_review_mode", "mode", default=None),
            "audio_auto_removed": audio_attr(
                "audio_notes_removed", "audio_auto_removed", "notes_removed", "removed_count", default=0
            ),
            "audio_suspected_extra": audio_attr(
                "audio_possible_extra", "audio_suspected_extra", "suspected_extra", "suspected_extra_count", default=0
            ),
            "audio_suspected_missing": audio_attr(
                "audio_possible_missing", "audio_suspected_missing", "suspected_missing", "suspected_missing_count", default=0
            ),
            "audio_model_used": bool(
                audio_attr("audio_model_used", "model_used", default=False)
                or (
                    audio_attr("audio_model_name", "model_name", default=None)
                    and not audio_attr("audio_dsp_only", "dsp_only", default=False)
                )
            ),
            "audio_model_name": audio_attr("audio_model_name", "model_name", default=None),
            "audio_backend": audio_attr("audio_backend", "backend", default=None),
            "audio_alignment_seconds": audio_attr(
                "audio_alignment_offset_seconds",
                "audio_alignment_seconds",
                "alignment_seconds",
                "offset_seconds",
                default=None,
            ),
            "audio_alignment_beats": audio_attr(
                "audio_alignment_beats", "alignment_beats", "offset_beats", default=None
            ),
            "audio_alignment_confidence": audio_attr(
                "audio_alignment_confidence", "alignment_confidence", default=None
            ),
            "audio_summary": audio_attr("audio_summary", "summary", default=None),
            "warnings": attr("warnings", []) or [],
            "tempo_bpm": attr("tempo_bpm", None),
            "time_signature": attr("time_signature", None),
            "key_fifths": attr("key_fifths", None),
            "key_name": attr("key_name", None),
            "key_mode": attr("key_mode", None),
            "tempo_change_count": attr("tempo_change_count", 0),
            "time_signature_change_count": attr("time_signature_change_count", 0),
            "key_change_count": attr("key_change_count", 0),
            "pickup_beats": attr("pickup_beats", 0),
            "files": file_rows,
        }
    )


class WebApp:
    """State owned by one loopback HTTP server."""

    def __init__(self, token: str, temp_root: Path, converter: Converter = convert) -> None:
        self.token = token
        self.temp_root = temp_root
        self.converter = converter
        self.jobs: dict[str, dict[str, Any]] = {}
        self.pending_audio: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.server: ThreadingHTTPServer | None = None

    def store_audio_upload(self, original_name: str, stream: BinaryIO, length: int) -> str:
        """Stream one reference audio file to disk without buffering it in RAM."""

        safe_name = Path(unquote(str(original_name))).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            raise ValueError("原始音频仅支持 FLAC、WAV、MP3 或 OGG。")
        if not 0 < length <= MAX_AUDIO_FILE_BYTES:
            raise ValueError("原始音频为空或超过 512 MB。")
        with self.lock:
            expired = [
                upload_id
                for upload_id, item in self.pending_audio.items()
                if time.time() - float(item["created"]) > 3600
            ]
        for upload_id in expired:
            self.discard_audio_upload(upload_id)
        upload_id = secrets.token_urlsafe(18)
        upload_dir = self.temp_root / "pending-audio"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{upload_id}{suffix}"
        with self.lock:
            if len(self.pending_audio) >= 2:
                raise ValueError("已有两个待处理的原始音频，请先完成或重新启动应用。")
            retained_bytes = sum(int(item["size"]) for item in self.pending_audio.values())
            for job in self.jobs.values():
                retained = job.get("reference_audio")
                if retained:
                    try:
                        retained_bytes += Path(retained).stat().st_size
                    except OSError:
                        pass
            if retained_bytes + length > MAX_TOTAL_AUDIO_BYTES:
                raise ValueError("本机临时音频合计将超过 1 GB；请先保存现有结果并完成清理。")
            # Reserve the slot before reading so parallel local requests cannot
            # bypass the pending-upload disk limit.
            self.pending_audio[upload_id] = {
                "path": upload_path,
                "name": safe_name,
                "size": length,
                "created": time.time(),
            }
        header = bytearray()
        remaining = length
        try:
            with upload_path.open("xb") as target:
                while remaining:
                    chunk = stream.read(min(UPLOAD_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("原始音频上传不完整，请重试。")
                    if len(header) < 16:
                        header.extend(chunk[: 16 - len(header)])
                    target.write(chunk)
                    remaining -= len(chunk)
            if not _valid_audio_header(suffix, bytes(header)):
                raise ValueError(f"“{safe_name}”的内容与音频格式不符。")
            return upload_id
        except Exception:
            self.discard_audio_upload(upload_id)
            raise

    def discard_audio_upload(self, upload_id: str) -> bool:
        with self.lock:
            item = self.pending_audio.pop(str(upload_id), None)
        if item is None:
            return False
        path = Path(item["path"])
        path.unlink(missing_ok=True)
        return True

    def _claim_audio_upload(self, upload_id: str, destination: Path) -> Path:
        with self.lock:
            item = self.pending_audio.get(upload_id)
            if item is None:
                raise ValueError("原始音频上传已失效，请重新选择音频。")
            source = Path(item["path"])
            suffix = source.suffix.lower()
            target = destination / f"reference-audio{suffix}"
            shutil.move(str(source), target)
            self.pending_audio.pop(upload_id, None)
        return target

    def create_job(self, configuration: Mapping[str, Any], files: Mapping[str, tuple[str, bytes]]) -> str:
        audio_upload_id = ""
        with self.lock:
            active = sum(job["status"] in {"queued", "running"} for job in self.jobs.values())
            if active >= 2:
                raise ValueError("已有两个转换正在进行，请稍候再提交。")
            job_id = secrets.token_urlsafe(16)
            job_dir = self.temp_root / job_id
            try:
                configured_files = configuration.get("files")
                specs = _specs_from_payload(configured_files, files, job_dir)
                source_names = {
                    index: Path(files[str(row.get("upload_id", ""))][0]).name
                    for index, row in enumerate(configured_files)
                }
                settings = _settings_from_payload(configuration.get("settings", {}))
                output_name = _safe_output_name(configuration.get("output_name"))
                audio_data = configuration.get("audio_review", {})
                if not isinstance(audio_data, Mapping):
                    raise ValueError("原始音频复核设置格式不正确。")
                audio_enabled = bool(audio_data.get("enabled", False))
                audio_mode = str(audio_data.get("mode", "conservative")).strip().lower()
                if audio_mode not in {"conservative", "balanced", "strict"}:
                    raise ValueError("音频复核模式只能选择保守、普通或严格。")
                reference_audio: Path | None = None
                if audio_enabled:
                    audio_upload_id = str(audio_data.get("upload_id", ""))
                    if not audio_upload_id:
                        raise ValueError("请先选择并上传原始音频。")
                    reference_audio = self._claim_audio_upload(audio_upload_id, job_dir)
                audio_require_model = bool(audio_data.get("require_model", True))
                audio_dsp_only = bool(audio_data.get("dsp_only", False))
            except Exception:
                shutil.rmtree(job_dir, ignore_errors=True)
                if audio_upload_id:
                    self.discard_audio_upload(audio_upload_id)
                raise
            output_path = job_dir / "result.mscz"
            self.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 4,
                "stage": "文件已安全接收，准备转换",
                "messages": [],
                "created": time.time(),
                "output_name": output_name,
                "output_path": output_path,
                "reference_audio": reference_audio,
                "result": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run_job,
            args=(
                job_id,
                specs,
                settings,
                source_names,
                reference_audio,
                audio_mode if audio_enabled else "off",
                audio_require_model,
                audio_dsp_only,
            ),
            name=f"midi-web-job-{job_id[:6]}",
            daemon=True,
        )
        thread.start()
        return job_id

    def _run_job(
        self,
        job_id: str,
        specs: list[InputSpec],
        settings: ConversionSettings,
        source_names: Mapping[int, str],
        reference_audio: Path | None,
        audio_review_mode: str,
        audio_require_model: bool,
        audio_dsp_only: bool,
    ) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(status="running", progress=9, stage="正在分析 MIDI")

        def progress(message: str) -> None:
            clean = str(message).strip()
            with self.lock:
                job = self.jobs[job_id]
                job["messages"] = (job["messages"] + [clean])[-12:]
                job["stage"] = clean or "正在转换"
                job["progress"] = min(92, max(job["progress"] + 11, 18))

        try:
            output_path = Path(self.jobs[job_id]["output_path"])
            converter_options: dict[str, Any] = {}
            if reference_audio is not None:
                converter_options.update(
                    reference_audio=reference_audio,
                    audio_review_mode=audio_review_mode,
                    audio_require_model=audio_require_model,
                    audio_dsp_only=audio_dsp_only,
                )
            report = self.converter(specs, output_path, settings, progress, **converter_options)
            if not output_path.is_file() or output_path.stat().st_size == 0:
                raise RuntimeError("转换程序没有生成 MSCZ 文件。")
            result = _report_payload(
                report,
                str(self.jobs[job_id]["output_name"]),
                source_names,
            )
            digest = hashlib.sha256()
            with output_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result["output_size"] = output_path.stat().st_size
            result["output_sha256"] = digest.hexdigest()
            with self.lock:
                self.jobs[job_id].update(
                    status="done",
                    progress=100,
                    stage="转换完成",
                    result=result,
                )
        except Exception as exc:  # conversion errors are returned as friendly text
            message = str(exc).replace(str(self.temp_root), "临时目录")
            with self.lock:
                self.jobs[job_id].update(
                    status="error",
                    progress=100,
                    stage="转换失败",
                    error=message or "转换失败，请检查 MIDI 与 MuseScore 安装。",
                )
            shutil.rmtree(self.temp_root / job_id, ignore_errors=True)

    def public_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {
                key: _json_safe(job[key])
                for key in ("id", "status", "progress", "stage", "messages", "result", "error")
            }

    def mark_saved(self, job_id: str) -> bool:
        """Forget a completed result after the browser has safely written it."""

        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job["status"] == "saved":
                return True
            if job["status"] != "done":
                return False
            job_dir = self.temp_root / job_id
        shutil.rmtree(job_dir)
        if job_dir.exists():
            raise OSError("临时结果目录未能完全删除。")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job.update(
                    status="saved",
                    stage="已保存并清理临时文件",
                    output_path=None,
                )
        return True


class WebUIRequestHandler(BaseHTTPRequestHandler):
    """Small, explicitly routed HTTP surface; never serves arbitrary paths."""

    server_version = "MidiScoreLocal/1.0"

    @property
    def app(self) -> WebApp:
        return getattr(self.server, "app")

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost"}

    def _authorized(self, query: Mapping[str, list[str]]) -> bool:
        supplied = (query.get("token") or [""])[0]
        if not supplied:
            supplied = self.headers.get("X-App-Token", "")
        if not supplied:
            cookies = self.headers.get("Cookie", "")
            for chunk in cookies.split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == "m2m_token":
                    supplied = value
                    break
        return secrets.compare_digest(supplied, self.app.token)

    def _origin_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}

    def _security_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self._security_headers(content_type, len(payload))
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"m2m_token={self.app.token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(_json_safe(value), ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _route(self) -> tuple[str, Mapping[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path, query = self._route()
        if not self._host_is_local():
            self._error(HTTPStatus.FORBIDDEN, "仅允许本机访问。")
            return
        if not self._authorized(query):
            self._error(HTTPStatus.UNAUTHORIZED, "本次页面凭证已失效，请重新启动应用。")
            return

        if path in {"/", "/index.html"}:
            try:
                html = (ASSET_DIR / "index.html").read_text(encoding="utf-8")
            except OSError:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "界面文件缺失，请重新安装。")
                return
            html = html.replace("__APP_TOKEN__", self.app.token)
            self._send_bytes(
                HTTPStatus.OK,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                cookie=True,
            )
            return
        if path in {"/app.css", "/app.js"}:
            asset = ASSET_DIR / path.lstrip("/")
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            try:
                payload = asset.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "资源不存在。")
                return
            if path.endswith(".js"):
                content_type = "text/javascript; charset=utf-8"
            elif path.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            self._send_bytes(HTTPStatus.OK, payload, content_type)
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/capabilities":
            self._send_json(HTTPStatus.OK, {"audio_review": _audio_capabilities()})
            return
        match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)", path)
        if match:
            job = self.app.public_job(match.group(1))
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, "找不到这个转换任务。")
            else:
                self._send_json(HTTPStatus.OK, job)
            return
        match = re.fullmatch(r"/api/results/([A-Za-z0-9_-]+)", path)
        if match:
            with self.app.lock:
                job = self.app.jobs.get(match.group(1))
                if not job or job["status"] != "done":
                    self._error(HTTPStatus.NOT_FOUND, "结果尚未生成或已经失效。")
                    return
                output = Path(job["output_path"])
            try:
                payload = output.read_bytes()
            except OSError:
                self._error(HTTPStatus.GONE, "结果文件已经失效，请重新转换。")
                return
            self._send_bytes(
                HTTPStatus.OK,
                payload,
                "application/vnd.musescore.mscz",
            )
            return
        self._error(HTTPStatus.NOT_FOUND, "页面不存在。")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path, query = self._route()
        if not self._host_is_local() or not self._origin_is_local():
            self._error(HTTPStatus.FORBIDDEN, "仅允许本机页面提交。")
            return
        if not self._authorized(query):
            self._error(HTTPStatus.UNAUTHORIZED, "本次页面凭证已失效，请重新启动应用。")
            return
        if path == "/api/shutdown":
            with self.app.lock:
                active = any(
                    job["status"] in {"queued", "running"}
                    for job in self.app.jobs.values()
                )
            if active:
                self._error(HTTPStatus.CONFLICT, "转换仍在进行，请等待完成后再退出；这样才能完整清理临时文件。")
                return
            self._send_json(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        saved_match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)/saved", path)
        if saved_match:
            try:
                saved = self.app.mark_saved(saved_match.group(1))
            except OSError:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "文件已保存，但临时文件暂时无法清理；请稍后重试清理或退出应用。")
                return
            if saved:
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._error(HTTPStatus.CONFLICT, "结果尚未生成或已经失效。")
            return
        discard_match = re.fullmatch(r"/api/audio-uploads/([A-Za-z0-9_-]+)/discard", path)
        if discard_match:
            self.app.discard_audio_upload(discard_match.group(1))
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/audio-uploads":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_AUDIO_FILE_BYTES:
                self.close_connection = True
                self._error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "原始音频为空或超过 512 MB。",
                )
                return
            try:
                upload_id = self.app.store_audio_upload(
                    self.headers.get("X-Audio-Name", "reference-audio"),
                    self.rfile,
                    length,
                )
            except (ValueError, OSError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc) or "无法保存原始音频。")
                return
            self._send_json(HTTPStatus.CREATED, {"upload_id": upload_id})
            return
        if path != "/api/jobs":
            self._error(HTTPStatus.NOT_FOUND, "接口不存在。")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "MIDI 与设置的上传内容为空或超过 128 MB；原始音频请使用独立音频入口。",
            )
            return
        try:
            body = self.rfile.read(length)
            fields, files = _multipart_fields(self.headers.get("Content-Type", ""), body)
            configuration = json.loads(fields.get("configuration", ""))
            if not isinstance(configuration, Mapping):
                raise ValueError("转换设置格式不正确。")
            job_id = self.app.create_job(configuration, files)
        except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc) or "无法读取转换设置。")
            return
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"无法保存上传文件：{exc}")
            return
        self._send_json(HTTPStatus.ACCEPTED, {"job_id": job_id})


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def create_server(
    *, converter: Converter = convert, temp_root: Path | None = None
) -> LocalWebServer:
    """Create, but do not start, a token-protected loopback server."""

    root = temp_root or Path(tempfile.mkdtemp(prefix="midi-to-mscz-web-"))
    root.mkdir(parents=True, exist_ok=True)
    app = WebApp(secrets.token_urlsafe(32), root, converter)
    server = LocalWebServer(("127.0.0.1", 0), WebUIRequestHandler)
    setattr(server, "app", app)
    app.server = server
    return server


def _native_message(title: str, message: str, flags: int = 0x40) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
    except Exception:
        print(f"{title}: {message}")


def main() -> int:
    """Open the local Web UI and serve until the user chooses “退出应用”."""

    server = create_server()
    app: WebApp = getattr(server, "app")
    host, port = server.server_address
    url = f"http://{host}:{port}/?token={app.token}"
    try:
        if not webbrowser.open(url, new=1):
            _native_message("MIDI 标准化", f"请在浏览器中打开：\n\n{url}")
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        _native_message("MIDI 标准化启动失败", str(exc), 0x10)
        return 1
    finally:
        server.server_close()
        shutil.rmtree(app.temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
