"""End-to-end MIDI normalization pipeline."""

from __future__ import annotations

import shutil
import tempfile
import inspect
from pathlib import Path
from typing import Any, Callable, Sequence

from .midi_io import read_midi_files
from .models import ConversionReport, ConversionSettings, InputSpec
from .musescore import export_musicxml_to_mscz
from .musicxml import write_musicxml


Progress = Callable[[str], None]


def _announce(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def _make_audio_verification_settings(
    model: type[Any],
    reference_audio: Path,
    mode: str,
    require_model: bool,
    dsp_only: bool,
) -> Any:
    """Build the optional audio model without coupling the main pipeline to field names."""

    values: dict[str, Any] = {
        "reference_audio": reference_audio,
        "mode": mode,
        "require_model": require_model,
        "dsp_only": dsp_only,
    }
    aliases = {
        "reference_audio": {"reference_audio", "audio_path", "reference_path", "path"},
        "mode": {"mode", "review_mode", "sensitivity"},
        "require_model": {"require_model", "model_required"},
        "dsp_only": {"dsp_only", "disable_model"},
    }
    try:
        signature = inspect.signature(model)
    except (TypeError, ValueError):
        return model(**values)
    kwargs: dict[str, Any] = {}
    accepts_extra = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    for logical_name, names in aliases.items():
        match = next((name for name in names if name in signature.parameters), None)
        if match is not None:
            kwargs[match] = values[logical_name]
        elif accepts_extra:
            kwargs[logical_name] = values[logical_name]
    return model(**kwargs)


def convert(
    specs: Sequence[InputSpec],
    output_path: str | Path,
    settings: ConversionSettings | None = None,
    progress: Progress | None = None,
    *,
    reference_audio: str | Path | None = None,
    audio_review_mode: str = "off",
    audio_require_model: bool = True,
    audio_dsp_only: bool = False,
) -> ConversionReport:
    """Normalize one or more MIDI stems and create a MuseScore ``.mscz``.

    All input-specific transformations are applied while reading.  Rhythm is
    then inferred on the combined absolute beat timeline, written as MusicXML,
    and imported by MuseScore.  The destination is not touched until MuseScore
    has produced a structurally valid score.
    """

    if not specs:
        raise ValueError("请至少添加一个 MIDI 文件。")
    for staff, display_name in (("treble", "高音"), ("bass", "低音")):
        count = sum(spec.staff == staff for spec in specs)
        if count > 4:
            raise ValueError(
                f"{display_name}谱表分配了 {count} 个 MIDI；"
                "MuseScore 每个谱表最多四个独立声部。请调整文件的谱表分配。"
            )
    settings = settings or ConversionSettings()
    destination = Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".mscz":
        destination = destination.with_suffix(".mscz")

    _announce(progress, f"正在读取 {len(specs)} 个 MIDI 文件…")
    files, report = read_midi_files(specs)
    if not any(item.notes for item in files):
        raise ValueError("过滤后没有可输出的音符；请降低最低力度阈值。")

    _announce(progress, f"读入 {report.notes_seen} 个音，保留 {report.notes_kept} 个。")

    configured_audio = getattr(settings, "audio_verification", None)
    review_mode = str(audio_review_mode or "off").strip().lower()
    audio_settings: Any | None = None
    if (
        review_mode == "off"
        and configured_audio is not None
        and bool(getattr(configured_audio, "enabled", True))
    ):
        audio_settings = configured_audio
        reference_audio = getattr(
            configured_audio,
            "audio_path",
            getattr(configured_audio, "reference_audio", None),
        )
        review_mode = str(getattr(configured_audio, "mode", "conservative")).strip().lower()
    if review_mode not in {"off", "conservative", "balanced", "strict"}:
        raise ValueError("音频复核模式必须是 conservative、balanced 或 strict。")
    if review_mode != "off":
        if reference_audio is None:
            raise ValueError("启用原始音频复核时必须提供音频文件。")
        audio_path = Path(reference_audio).expanduser().resolve()
        if not audio_path.is_file():
            raise ValueError(f"找不到原始音频：{audio_path}")
        if audio_settings is not None and hasattr(audio_settings, "audio_path"):
            audio_settings.audio_path = audio_path
        # The verifier and its heavier optional dependencies stay lazy so MIDI-only
        # conversion and WebUI startup remain lightweight.
        from .audio_verify import verify_audio
        from .models import AudioVerificationSettings

        if audio_settings is None:
            audio_settings = _make_audio_verification_settings(
                AudioVerificationSettings,
                audio_path,
                review_mode,
                bool(audio_require_model),
                bool(audio_dsp_only or not audio_require_model),
            )
        _announce(progress, "正在对齐原始音频，并复核疑似多余音与漏音…")
        audio_result = verify_audio(files, audio_settings, report=report, progress=progress)
        # These two lightweight attributes keep front ends compatible while the
        # detailed dataclass remains owned by the optional verifier.
        setattr(report, "audio_result", audio_result)
        setattr(report, "audio_review_mode", review_mode)
        if not any(item.notes for item in files):
            raise ValueError("音频复核后没有可输出的音符；请改用更保守的复核模式。")

    # Import lazily so a missing/invalid optional implementation yields a clear
    # conversion-time error rather than making inspection/UI startup fail.
    from .quantize import normalize

    _announce(
        progress,
        "正在校正演奏延迟，识别琶音、倚音、摇摆和通用连音并量化…",
    )
    score = normalize(files, settings, report)

    work_dir = Path(tempfile.mkdtemp(prefix="midi-to-mscz-work-", dir=str(destination.parent)))
    musicxml = work_dir / f"{destination.stem}.musicxml"
    try:
        _announce(progress, "正在生成钢琴大谱表 MusicXML…")
        write_musicxml(score, settings, musicxml)
        report.musicxml_path = musicxml
        export_musicxml_to_mscz(
            musicxml,
            destination,
            musescore_path=settings.musescore_path,
            progress=progress,
        )
        report.output_path = destination
        if settings.keep_intermediate:
            kept_xml = destination.with_suffix(".musicxml")
            shutil.copy2(musicxml, kept_xml)
            report.musicxml_path = kept_xml
            _announce(progress, f"已保留中间文件：{kept_xml}")
        else:
            report.musicxml_path = None
        return report
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


__all__ = ["convert"]
