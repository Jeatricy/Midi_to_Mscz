"""Conservative score-metadata inference for imperfect transcription MIDI.

Song Master commonly writes one nearly identical tempo event per bar and its
key-signature tag is not always the key heard in the notes.  This module keeps
useful structural metadata (including temporary meter changes), while treating
those tags as evidence rather than unquestionable truth.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from fractions import Fraction
from typing import Iterable, Sequence

from .models import (
    ConversionSettings,
    KeyChange,
    MeterChange,
    MidiFileData,
    ScoreMetadata,
    TempoChange,
)


# Krumhansl-Kessler pitch-class profiles, indexed from the candidate tonic.
_MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)

_MAJOR_NAMES = {
    -7: "降C大调", -6: "降G大调", -5: "降D大调", -4: "降A大调",
    -3: "降E大调", -2: "降B大调", -1: "F大调", 0: "C大调",
    1: "G大调", 2: "D大调", 3: "A大调", 4: "E大调",
    5: "B大调", 6: "升F大调", 7: "升C大调",
}
_MINOR_NAMES = {
    -7: "降A小调", -6: "降E小调", -5: "降B小调", -4: "F小调",
    -3: "C小调", -2: "G小调", -1: "D小调", 0: "A小调",
    1: "E小调", 2: "B小调", 3: "升F小调", 4: "升C小调",
    5: "升G小调", 6: "升D小调", 7: "升A小调",
}


def key_name(fifths: int, mode: str = "major") -> str:
    """Return a concise Chinese name for a circle-of-fifths value."""

    table = _MINOR_NAMES if mode == "minor" else _MAJOR_NAMES
    return table.get(int(fifths), f"五度圈 {int(fifths):+d}")


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return numerator / denominator if denominator else 0.0


def _pitch_class_histogram(files: Sequence[MidiFileData]) -> tuple[list[float], int]:
    """Combine source-normalised duration/velocity pitch-class evidence."""

    combined = [0.0] * 12
    note_count = 0
    usable_files = 0
    for file in files:
        local = [0.0] * 12
        for note in file.notes:
            duration = max(Fraction(1, 32), min(Fraction(4), note.duration_beats))
            velocity_weight = 0.35 + 0.65 * max(1, note.velocity) / 127.0
            local[note.pitch % 12] += float(duration) * velocity_weight
            note_count += 1
        total = sum(local)
        if not total:
            continue
        usable_files += 1
        for pitch_class, value in enumerate(local):
            combined[pitch_class] += value / total
    if usable_files:
        combined = [value / usable_files for value in combined]
    return combined, note_count


def _explicit_initial_keys(files: Sequence[MidiFileData]) -> Counter[tuple[int, str]]:
    result: Counter[tuple[int, str]] = Counter()
    for file in files:
        valid = [event for event in file.key_signature_events if event.fifths is not None]
        if valid:
            event = min(valid, key=lambda item: (item.beat, item.track_index, item.sequence))
            result[(int(event.fifths), event.mode)] += 1
    return result


def _infer_initial_key(
    files: Sequence[MidiFileData],
) -> tuple[KeyChange, list[str]]:
    histogram, note_count = _pitch_class_histogram(files)
    explicit = _explicit_initial_keys(files)
    warnings: list[str] = []
    if note_count < 8 or not sum(histogram):
        if explicit:
            (fifths, mode), count = explicit.most_common(1)[0]
            confidence = count / max(1, len(files))
            return KeyChange(0, fifths, mode, confidence, False), warnings
        warnings.append("MIDI 中没有足够音符用于判断调号，暂用 C 大调，请人工复核。")
        return KeyChange(0, 0, "major", 0.0, True), warnings

    candidates: list[tuple[float, int, str]] = []
    for mode, profile in (("major", _MAJOR_PROFILE), ("minor", _MINOR_PROFILE)):
        for fifths in range(-7, 8):
            tonic = ((7 * fifths) + (9 if mode == "minor" else 0)) % 12
            rotated = [profile[(pitch_class - tonic) % 12] for pitch_class in range(12)]
            score = _correlation(histogram, rotated)
            # Metadata is a weak spelling/key prior.  It can break a close tie,
            # but cannot override a clearly different harmony (the included
            # Song Master stems say Db while their notes strongly say Ab).
            if explicit:
                agreement = explicit[(fifths, mode)] / max(1, sum(explicit.values()))
                score += 0.035 * agreement
            score -= 0.001 * abs(fifths)
            candidates.append((score, fifths, mode))
    candidates.sort(reverse=True)
    best, runner_up = candidates[0], candidates[1]
    margin = max(0.0, best[0] - runner_up[0])
    confidence = max(0.0, min(1.0, 0.55 + margin * 1.8))
    chosen = KeyChange(0, best[1], best[2], confidence, True)

    if explicit:
        (tag_fifths, tag_mode), _count = explicit.most_common(1)[0]
        if (tag_fifths, tag_mode) != (chosen.fifths, chosen.mode):
            warnings.append(
                f"MIDI 调号标签是{key_name(tag_fifths, tag_mode)}，"
                f"但音符内容更符合{key_name(chosen.fifths, chosen.mode)}；"
                "已按音符内容输出，请人工复核。"
            )
    if confidence < 0.68:
        warnings.append(
            f"自动调号 {key_name(chosen.fifths, chosen.mode)} 的把握较低，请人工复核。"
        )
    return chosen, warnings


def _clean_consecutive(items: Iterable[tuple[Fraction, object]]) -> list[tuple[Fraction, object]]:
    result: list[tuple[Fraction, object]] = []
    for beat, value in sorted(items, key=lambda item: item[0]):
        if result and beat == result[-1][0]:
            result[-1] = (beat, value)
        elif result and value == result[-1][1]:
            continue
        else:
            result.append((beat, value))
    return result


def _best_meter_map(files: Sequence[MidiFileData]) -> list[tuple[Fraction, tuple[int, int]]]:
    maps: list[list[tuple[Fraction, tuple[int, int]]]] = []
    for file in files:
        values = _clean_consecutive(
            (Fraction(event.tick, file.ticks_per_beat), event.signature)
            for event in file.time_signature_events
        )
        if values:
            maps.append([(beat, value) for beat, value in values])
    if not maps:
        return []
    # Prefer the map that contains most real changes.  On a tie, prefer the
    # most common exact map across stems, then the earliest input.
    signatures = Counter(tuple(value) for value in maps)
    return max(maps, key=lambda value: (len(value), signatures[tuple(value)]))


def _infer_meter(
    files: Sequence[MidiFileData], settings: ConversionSettings
) -> tuple[list[MeterChange], Fraction, list[str]]:
    if settings.time_signature is not None:
        numerator, denominator = settings.time_signature
        pickup = settings.pickup_beats if settings.pickup else Fraction(0)
        return [MeterChange(0, numerator, denominator, 1.0, False)], pickup, []

    values = _best_meter_map(files)
    warnings: list[str] = []
    if not values:
        warnings.append("MIDI 没有拍号信息，暂按 4/4 拍输出，请人工复核。")
        return [MeterChange(0, 4, 4, 0.25, True)], Fraction(0), warnings

    pickup = Fraction(0)
    # Some notation programs encode an anacrusis as a short first meter, then
    # switch to the real meter exactly at its end.  Expose that as a pickup,
    # not as a visible 1/4 -> 4/4 meter change.
    if settings.auto_pickup and len(values) >= 2:
        first_beat, first_sig = values[0]
        second_beat, second_sig = values[1]
        first_length = Fraction(first_sig[0] * 4, first_sig[1])
        second_length = Fraction(second_sig[0] * 4, second_sig[1])
        if (
            first_beat == 0
            and second_beat == first_length
            and first_sig != second_sig
            and first_length < second_length
        ):
            pickup = first_length
            values = [(Fraction(0), second_sig), *values[2:]]

    if values[0][0] > 0:
        values.insert(0, (Fraction(0), values[0][1]))
    changes = [
        MeterChange(beat, signature[0], signature[1], 1.0, False)
        for beat, signature in values
    ]
    # Transcription tools often stamp only one initial 4/4 event even when the
    # source song later changes meter.  A long file with a dense conductor map
    # has enough evidence to identify that pattern, but not enough to invent
    # trustworthy change positions from note attacks alone.  Keep the explicit
    # meter and tell the user to review structural transitions.
    longest = max(
        (
            max((note.end_beat for note in file.notes), default=Fraction(0))
            for file in files
        ),
        default=Fraction(0),
    )
    dense_conductor = any(len(file.tempo_events) >= 8 for file in files)
    no_later_meter_events = all(len(file.time_signature_events) <= 1 for file in files)
    if len(changes) == 1 and longest >= 32 and dense_conductor and no_later_meter_events:
        signature = changes[0].signature
        changes[0] = MeterChange(0, signature[0], signature[1], 0.55, True)
        warnings.append(
            f"输入只记录了开头的 {signature[0]}/{signature[1]} 拍号，无法可靠定位曲中临时变拍；"
            "已保留该拍号，请在 MuseScore 中复核段落转折。"
        )
    return changes, pickup, warnings


def _best_tempo_map(files: Sequence[MidiFileData]) -> list[tuple[Fraction, float]]:
    maps: list[list[tuple[Fraction, float]]] = []
    for file in files:
        by_beat: dict[Fraction, list[float]] = {}
        for event in file.tempo_events:
            by_beat.setdefault(
                Fraction(event.tick, file.ticks_per_beat), []
            ).append(event.bpm)
        if by_beat:
            maps.append(
                [(beat, statistics.median(bpms)) for beat, bpms in sorted(by_beat.items())]
            )
    if not maps:
        return []
    return max(maps, key=len)


def _pretty_bpm(value: float) -> float:
    nearest = round(value)
    return float(nearest) if abs(value - nearest) <= 0.75 else round(value, 3)


def _smooth_tempos(values: list[tuple[Fraction, float]]) -> list[tuple[Fraction, float]]:
    if not values:
        return []
    bpms = [value for _beat, value in values]
    global_median = statistics.median(bpms)
    if max(bpms) - min(bpms) <= max(1.5, global_median * 0.02):
        return [(Fraction(0), _pretty_bpm(global_median))]

    intervals = [
        float(right[0] - left[0])
        for left, right in zip(values, values[1:])
        if right[0] > left[0]
    ]
    # A sparse conductor-style tempo map normally writes each deliberate
    # change only once.  Dense, near-bar-by-bar maps are the ones that require
    # persistence filtering for transcription-tool jitter.
    if intervals and statistics.median(intervals) >= 8.0:
        sparse: list[tuple[Fraction, float]] = []
        for beat, bpm in values:
            cleaned = _pretty_bpm(bpm)
            if not sparse or abs(cleaned - sparse[-1][1]) >= max(1.0, sparse[-1][1] * 0.01):
                sparse.append((Fraction(0) if not sparse else beat, cleaned))
        return sparse

    result: list[tuple[Fraction, float]] = [(Fraction(0), _pretty_bpm(bpms[0]))]
    current_values = [bpms[0]]
    index = 1
    while index < len(values):
        beat, bpm = values[index]
        current = statistics.median(current_values[-8:])
        change_threshold = max(2.0, current * 0.025)
        if abs(bpm - current) < change_threshold:
            current_values.append(bpm)
            index += 1
            continue

        following = values[index + 1][1] if index + 1 < len(values) else None
        return_tolerance = max(1.5, current * 0.02)
        isolated_spike = following is not None and abs(following - current) < return_tolerance
        if not isolated_spike:
            # Start the new section at the actual first instruction, not at a
            # look-ahead median.  This also retains legitimate one-bar tempo
            # sections when the following section has a third value.
            result.append((beat, _pretty_bpm(bpm)))
            current_values = [bpm]
        else:
            current_values.append(bpm)
        index += 1

    return _clean_consecutive(result)  # type: ignore[return-value]


def _infer_tempos(
    files: Sequence[MidiFileData], settings: ConversionSettings
) -> tuple[list[TempoChange], list[str]]:
    if settings.bpm is not None:
        return [TempoChange(0, float(settings.bpm), 1.0, False)], []
    values = _best_tempo_map(files)
    if not values:
        return [TempoChange(0, 120.0, 0.25, True)], [
            "MIDI 没有速度信息，暂用 120 BPM，请人工复核。"
        ]
    cleaned = (
        _smooth_tempos(values)
        if settings.tempo_smoothing
        else [(beat, _pretty_bpm(bpm)) for beat, bpm in values]
    )
    if cleaned[0][0] > 0:
        cleaned.insert(0, (Fraction(0), cleaned[0][1]))
    return [TempoChange(beat, bpm, 0.95, False) for beat, bpm in cleaned], []


def _infer_keys(
    files: Sequence[MidiFileData], settings: ConversionSettings
) -> tuple[list[KeyChange], list[str]]:
    if settings.key_fifths is not None:
        return [KeyChange(0, int(settings.key_fifths), "major", 1.0, False)], []
    initial, warnings = _infer_initial_key(files)
    changes = [initial]

    # Preserve later explicit changes only when the most detailed source says
    # they exist.  Initial spelling is still chosen from the actual pitches.
    richest = max(files, key=lambda file: len(file.key_signature_events), default=None)
    if richest is not None:
        for event in richest.key_signature_events:
            event_beat = Fraction(event.tick, richest.ticks_per_beat)
            if event_beat <= 0 or event.fifths is None:
                continue
            candidate = KeyChange(event_beat, event.fifths, event.mode, 0.75, False)
            if (candidate.fifths, candidate.mode) != (
                changes[-1].fifths,
                changes[-1].mode,
            ):
                changes.append(candidate)
    return changes, warnings


def infer_score_metadata(
    files: Sequence[MidiFileData], settings: ConversionSettings
) -> ScoreMetadata:
    """Infer a cleaned, notation-oriented metadata map for the combined score."""

    tempos, tempo_warnings = _infer_tempos(files, settings)
    meters, pickup, meter_warnings = _infer_meter(files, settings)
    keys, key_warnings = _infer_keys(files, settings)
    if settings.pickup:
        pickup = settings.pickup_beats
    return ScoreMetadata(
        tempo_changes=tempos,
        meter_changes=meters,
        key_changes=keys,
        pickup_beats=pickup,
        warnings=[*tempo_warnings, *meter_warnings, *key_warnings],
    )


def shift_metadata(metadata: ScoreMetadata, offset: Fraction) -> ScoreMetadata:
    """Move maps left after shared pre-roll trimming, retaining active values."""

    offset = Fraction(offset)

    def shift_map(items: Sequence[object]) -> list[object]:
        if not items:
            return []
        before = [item for item in items if getattr(item, "beat") <= offset]
        active = before[-1] if before else items[0]
        shifted: list[object] = []
        if isinstance(active, TempoChange):
            shifted.append(TempoChange(0, active.bpm, active.confidence, active.inferred))
        elif isinstance(active, MeterChange):
            shifted.append(MeterChange(0, active.numerator, active.denominator, active.confidence, active.inferred))
        elif isinstance(active, KeyChange):
            shifted.append(KeyChange(0, active.fifths, active.mode, active.confidence, active.inferred))
        for item in items:
            if getattr(item, "beat") <= offset:
                continue
            beat = getattr(item, "beat") - offset
            if isinstance(item, TempoChange):
                new = TempoChange(beat, item.bpm, item.confidence, item.inferred)
                same = isinstance(shifted[-1], TempoChange) and shifted[-1].bpm == new.bpm
            elif isinstance(item, MeterChange):
                new = MeterChange(beat, item.numerator, item.denominator, item.confidence, item.inferred)
                same = isinstance(shifted[-1], MeterChange) and shifted[-1].signature == new.signature
            else:
                new = KeyChange(beat, item.fifths, item.mode, item.confidence, item.inferred)
                same = isinstance(shifted[-1], KeyChange) and (shifted[-1].fifths, shifted[-1].mode) == (new.fifths, new.mode)
            if not same:
                shifted.append(new)
        return shifted

    pickup = metadata.pickup_beats if offset == 0 else Fraction(0)
    return ScoreMetadata(
        tempo_changes=shift_map(metadata.tempo_changes),  # type: ignore[arg-type]
        meter_changes=shift_map(metadata.meter_changes),  # type: ignore[arg-type]
        key_changes=shift_map(metadata.key_changes),  # type: ignore[arg-type]
        pickup_beats=pickup,
        warnings=list(metadata.warnings),
    )


__all__ = ["infer_score_metadata", "key_name", "shift_metadata"]
