"""Lossless-enough MIDI ingestion for the score-cleaning pipeline."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

import mido

from .models import (
    ConversionReport,
    FileReport,
    InputSpec,
    KeySignatureEvent,
    MidiFileData,
    NoteEvent,
    TempoEvent,
    TimeSignatureEvent,
)


class MidiReadError(ValueError):
    """Raised when an input cannot be read as a Standard MIDI File."""


_MAJOR_FIFTHS = {
    "Cb": -7,
    "Gb": -6,
    "Db": -5,
    "Ab": -4,
    "Eb": -3,
    "Bb": -2,
    "F": -1,
    "C": 0,
    "G": 1,
    "D": 2,
    "A": 3,
    "E": 4,
    "B": 5,
    "F#": 6,
    "C#": 7,
}
_MINOR_FIFTHS = {
    "Abm": -7,
    "Ebm": -6,
    "Bbm": -5,
    "Fm": -4,
    "Cm": -3,
    "Gm": -2,
    "Dm": -1,
    "Am": 0,
    "Em": 1,
    "Bm": 2,
    "F#m": 3,
    "C#m": 4,
    "G#m": 5,
    "D#m": 6,
    "A#m": 7,
}


@dataclass(frozen=True)
class _OpenNote:
    tick: int
    velocity: int
    sequence: int


def key_to_fifths(key: str) -> tuple[int | None, str]:
    """Return MusicXML fifths and mode for a mido key name."""

    if key in _MINOR_FIFTHS:
        return _MINOR_FIFTHS[key], "minor"
    return _MAJOR_FIFTHS.get(key), "major"


def _coerce_spec(value: InputSpec | str | Path) -> InputSpec:
    return value if isinstance(value, InputSpec) else InputSpec(path=value)


def _beat(tick: int, ticks_per_beat: int, offset: Fraction) -> Fraction:
    return Fraction(tick, ticks_per_beat) + offset


def _report_warnings(report: FileReport) -> None:
    if report.velocity_filtered:
        report.warnings.append(
            f"Filtered {report.velocity_filtered} note(s) below the velocity threshold."
        )
    if report.invalid_duration:
        report.warnings.append(
            f"Ignored {report.invalid_duration} note(s) with zero or negative duration."
        )
    if report.unmatched_note_offs:
        report.warnings.append(
            f"Ignored {report.unmatched_note_offs} unmatched note-off event(s)."
        )
    if report.unclosed_note_ons:
        report.warnings.append(
            f"Ignored {report.unclosed_note_ons} note-on event(s) without a note-off."
        )
    if report.out_of_midi_range:
        report.warnings.append(
            f"Dropped {report.out_of_midi_range} transposed note(s) outside MIDI 0-127."
        )
    if report.out_of_piano_range:
        report.warnings.append(
            f"Kept {report.out_of_piano_range} note(s) outside the piano range A0-C8."
        )


def read_midi_file(
    spec: InputSpec | str | Path,
    source_index: int = 0,
) -> MidiFileData:
    """Read and pre-process every track of one MIDI file.

    Same-channel, same-pitch overlaps are paired first-in/first-out.  This is
    important for retriggered transcription notes: a stack (last-in/first-out)
    would incorrectly cross their durations.  Velocity filtering happens only
    after pairing, so a removed attack cannot steal another note's note-off.
    """

    spec = _coerce_spec(spec)
    path = spec.path
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise MidiReadError(f"MIDI input is not a file: {path}")

    try:
        midi = mido.MidiFile(str(path))
    except (OSError, EOFError, ValueError) as exc:
        raise MidiReadError(f"Could not read MIDI file {path}: {exc}") from exc

    if midi.ticks_per_beat <= 0:
        raise MidiReadError(f"MIDI file has invalid ticks_per_beat: {path}")

    report = FileReport(
        source_index=source_index,
        path=path,
        staff=spec.staff,
        track_count=len(midi.tracks),
    )
    notes: list[NoteEvent] = []
    tempos: list[TempoEvent] = []
    time_signatures: list[TimeSignatureEvent] = []
    key_signatures: list[KeySignatureEvent] = []
    track_names: list[str] = []
    duration_ticks = 0
    offset = spec.offset_beats
    pitch_shift = spec.total_transpose_semitones

    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0
        active: dict[tuple[int, int], deque[_OpenNote]] = defaultdict(deque)
        track_name = f"Track {track_index + 1}"

        for sequence, message in enumerate(track):
            absolute_tick += int(message.time)
            duration_ticks = max(duration_ticks, absolute_tick)

            if message.type == "track_name" and message.name:
                track_name = message.name
            elif message.type == "set_tempo":
                tempos.append(
                    TempoEvent(
                        tick=absolute_tick,
                        beat=_beat(absolute_tick, midi.ticks_per_beat, offset),
                        tempo=int(message.tempo),
                        track_index=track_index,
                        sequence=sequence,
                    )
                )
            elif message.type == "time_signature":
                time_signatures.append(
                    TimeSignatureEvent(
                        tick=absolute_tick,
                        beat=_beat(absolute_tick, midi.ticks_per_beat, offset),
                        numerator=int(message.numerator),
                        denominator=int(message.denominator),
                        clocks_per_click=int(message.clocks_per_click),
                        notated_32nd_notes_per_beat=int(
                            message.notated_32nd_notes_per_beat
                        ),
                        track_index=track_index,
                        sequence=sequence,
                    )
                )
            elif message.type == "key_signature":
                fifths, mode = key_to_fifths(message.key)
                key_signatures.append(
                    KeySignatureEvent(
                        tick=absolute_tick,
                        beat=_beat(absolute_tick, midi.ticks_per_beat, offset),
                        key=message.key,
                        fifths=fifths,
                        mode=mode,  # type: ignore[arg-type]
                        track_index=track_index,
                        sequence=sequence,
                    )
                )
                if fifths is None:
                    report.warnings.append(
                        f"Could not translate MIDI key signature {message.key!r}."
                    )

            is_note_on = message.type == "note_on" and message.velocity > 0
            is_note_off = message.type == "note_off" or (
                message.type == "note_on" and message.velocity == 0
            )
            if is_note_on:
                report.notes_seen += 1
                key = (int(message.channel), int(message.note))
                active[key].append(
                    _OpenNote(
                        tick=absolute_tick,
                        velocity=int(message.velocity),
                        sequence=sequence,
                    )
                )
            elif is_note_off:
                key = (int(message.channel), int(message.note))
                starts = active.get(key)
                if not starts:
                    report.unmatched_note_offs += 1
                    continue
                opened = starts.popleft()
                if not starts:
                    del active[key]
                report.paired_notes += 1

                if absolute_tick <= opened.tick:
                    report.invalid_duration += 1
                    continue
                if opened.velocity < spec.velocity_min:
                    report.velocity_filtered += 1
                    continue

                pitch = int(message.note) + pitch_shift
                if pitch_shift:
                    report.transposed_notes += 1
                if not 0 <= pitch <= 127:
                    report.out_of_midi_range += 1
                    continue
                if not 21 <= pitch <= 108:
                    report.out_of_piano_range += 1

                notes.append(
                    NoteEvent(
                        source_index=source_index,
                        source_path=path,
                        raw_start=opened.tick,
                        raw_end=absolute_tick,
                        start_beat=_beat(opened.tick, midi.ticks_per_beat, offset),
                        end_beat=_beat(absolute_tick, midi.ticks_per_beat, offset),
                        original_pitch=int(message.note),
                        pitch=pitch,
                        velocity=opened.velocity,
                        staff=spec.staff,
                        channel=int(message.channel),
                        track_index=track_index,
                        note_id=f"s{source_index}:t{track_index}:e{opened.sequence}",
                    )
                )
                report.notes_kept += 1

        report.unclosed_note_ons += sum(len(starts) for starts in active.values())
        track_names.append(track_name)

    notes.sort(
        key=lambda note: (
            note.start_beat,
            note.end_beat,
            note.pitch,
            note.track_index,
            str(note.note_id),
        )
    )
    event_sort_key = lambda event: (event.tick, event.track_index, event.sequence)
    tempos.sort(key=event_sort_key)
    time_signatures.sort(key=event_sort_key)
    key_signatures.sort(key=event_sort_key)
    _report_warnings(report)

    return MidiFileData(
        source_index=source_index,
        path=path,
        spec=spec,
        midi_type=int(midi.type),
        ticks_per_beat=int(midi.ticks_per_beat),
        notes=notes,
        report=report,
        tempo_events=tempos,
        time_signature_events=time_signatures,
        key_signature_events=key_signatures,
        track_names=track_names,
        duration_ticks=duration_ticks,
    )


def read_midi_files(
    specs: Iterable[InputSpec | str | Path],
) -> tuple[list[MidiFileData], ConversionReport]:
    """Read a batch in user-supplied order and return data plus totals."""

    data = [read_midi_file(spec, index) for index, spec in enumerate(specs)]
    report = ConversionReport(files=[item.report for item in data])
    for item in data:
        report.warnings.extend(
            f"{item.path.name}: {warning}" for warning in item.report.warnings
        )
    return data, report


# A concise alias for callers that think in terms of parsing rather than I/O.
parse_midi = read_midi_file


__all__ = [
    "MidiReadError",
    "key_to_fifths",
    "parse_midi",
    "read_midi_file",
    "read_midi_files",
]
