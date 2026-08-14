"""End-to-end MIDI normalization pipeline."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from .midi_io import read_midi_files
from .models import ConversionReport, ConversionSettings, InputSpec
from .musescore import export_musicxml_to_mscz
from .musicxml import write_musicxml


Progress = Callable[[str], None]


def _announce(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def convert(
    specs: Sequence[InputSpec],
    output_path: str | Path,
    settings: ConversionSettings | None = None,
    progress: Progress | None = None,
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
