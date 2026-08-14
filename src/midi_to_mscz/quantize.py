"""Musically conservative adaptive quantization.

This module turns loosely timed transcription notes into notation events.  It
does not try to infer harmony.  Special gestures are protected before ordinary
quantization and complete, beat-anchored tuplet models compete with the binary
grid.  Ambiguous patterns are deliberately left on the straight grid.
"""

from __future__ import annotations

import copy
import math
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Sequence

from .models import (
    AttackGroup,
    ChordEvent,
    ConversionReport,
    ConversionSettings,
    MidiFileData,
    NoteEvent,
    RestEvent,
    ScoreMetadata,
    TupletGroup,
    VoiceLine,
)


@dataclass
class NormalizedScore:
    voices: dict[str, list[VoiceLine]] = field(
        default_factory=lambda: {"treble": [], "bass": []}
    )
    tuplets: list[TupletGroup] = field(default_factory=list)
    attack_groups: list[AttackGroup] = field(default_factory=list)
    tempo_bpm: float = 120.0
    time_signature: tuple[int, int] = (4, 4)
    key_fifths: int = 0
    metadata: ScoreMetadata = field(default_factory=ScoreMetadata)
    duration: Fraction = Fraction(0)
    pickup_beats: Fraction = Fraction(0)
    latency_by_source: dict[int, Fraction] = field(default_factory=dict)
    trim_offset: Fraction = Fraction(0)
    warnings: list[str] = field(default_factory=list)
    swing_ratio: tuple[int, int] | None = None
    swing_regions: list[tuple[Fraction, Fraction, tuple[int, int]]] = field(
        default_factory=list
    )

    @property
    def bpm(self) -> float:
        return self.tempo_bpm


def _nearest(value: Fraction, step: Fraction) -> Fraction:
    scaled = value / step
    # Fraction-aware half-up rounding avoids Python's banker's rounding.
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:
        quotient += 1
    return quotient * step


def _absolute_distance(value: Fraction, step: Fraction) -> Fraction:
    return abs(value - _nearest(value, step))


def _binary_sequence_error(
    positions: Sequence[Fraction],
    anchor: Fraction,
    smallest_step: Fraction,
) -> float:
    """Return the best fit to one equally spaced dyadic attack sequence.

    Comparing every attack only with the finest enabled grid is misleading:
    three loosely played eighth notes can have a larger 1/32-grid residual
    than a phase-fitted quarter-note triplet even though their *gaps* are
    unmistakably binary.  Compete against straight 1/32, 1/16, 1/8, quarter,
    half-note, ... pulses and allow one shared transcription latency.  Tuplet
    candidates must beat this melodic-pulse model as well as being internally
    regular.
    """

    if not positions:
        return 0.0
    best = math.inf
    step = smallest_step
    # Eight quarter-note beats is already wider than any automatic tuplet
    # template below; the cap merely prevents an accidental unbounded loop.
    while step <= 8:
        # First test a contiguous binary pulse.  Then allow genuine rhythmic
        # skips (for example attacks on 0, 1/2 and 3/2) while requiring every
        # attack to keep a distinct grid slot.  That last invariant is vital:
        # a real 5:4 run must not "fit" a 1/16 grid by mapping two attacks onto
        # the same position, which would destroy one of them in notation.
        target_sets: list[list[Fraction]] = [
            [anchor + index * step for index in range(len(positions))]
        ]
        indices: list[int] = []
        previous = -10**9
        for position in positions:
            index = int(_nearest((position - anchor) / step, Fraction(1)))
            index = max(index, previous + 1)
            indices.append(index)
            previous = index
        target_sets.append([anchor + index * step for index in indices])
        for targets in target_sets:
            phase = _median_fraction(
                [position - target for position, target in zip(positions, targets)]
            )
            error = statistics.mean(
                float(abs(position - target - phase) / step)
                for position, target in zip(positions, targets)
            )
            best = min(best, error)
        step *= 2
    return best


def _median_fraction(values: Sequence[Fraction]) -> Fraction:
    if not values:
        return Fraction(0)
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _estimate_latency(notes: Sequence[NoteEvent], smallest: Fraction) -> Fraction:
    """Estimate a consistent transcription attack delay.

    Song Master outputs tend to be shifted by about 40--80 ms.  We estimate the
    signed phase against a conservative grid (never finer than a sixteenth),
    reject dispersed phases, and cap correction at 0.15 quarter-note beats.
    """

    if len(notes) < 6:
        return Fraction(0)
    step = max(smallest, Fraction(1, 4))
    residuals: list[Fraction] = []
    for note in notes:
        residual = note.start_beat - _nearest(note.start_beat, step)
        # Wrap a phase just below the next cell back to a small negative value.
        half = step / 2
        if residual > half:
            residual -= step
        elif residual < -half:
            residual += step
        residuals.append(residual)
    bias = _median_fraction(residuals)
    deviations = [abs(value - bias) for value in residuals]
    mad = _median_fraction(deviations)
    concentrated = sum(abs(value - bias) <= Fraction(1, 16) for value in residuals)
    if concentrated < max(5, int(len(residuals) * 0.55)):
        return Fraction(0)
    if mad > Fraction(1, 20) or abs(bias) > Fraction(3, 20):
        return Fraction(0)
    return bias.limit_denominator(960)


def _choose_tempo(files: Sequence[MidiFileData], settings: ConversionSettings) -> float:
    if settings.bpm is not None:
        return float(settings.bpm)
    values = [event.bpm for file in files for event in file.tempo_events]
    if not values:
        return 120.0
    median = statistics.median(values)
    if settings.tempo_smoothing:
        spread = (max(values) - min(values)) / max(1.0, median)
        if spread <= 0.04:
            rounded = round(median)
            if abs(rounded - median) <= 1.0:
                return float(rounded)
    return float(round(median, 3))


def _choose_metadata(
    files: Sequence[MidiFileData], settings: ConversionSettings
) -> tuple[tuple[int, int], int]:
    signature = settings.time_signature or next(
        (file.initial_time_signature for file in files if file.time_signature_events),
        (4, 4),
    )
    key = settings.key_fifths
    if key is None:
        key = next(
            (file.initial_key_fifths for file in files if file.key_signature_events),
            0,
        )
    return signature, int(key)


def _measure_boundaries(
    metadata: ScoreMetadata,
    through: Fraction,
) -> list[Fraction]:
    """Build the exact score barline timeline used for rhythmic anchoring.

    A modulo operation on the initial meter is insufficient: a pickup shifts
    every later barline, and an explicit meter change may close the current
    measure early.  This mirrors the measure construction contract used by
    the MusicXML writer so quantisation and rendering agree about barlines.
    """

    changes = sorted(metadata.meter_changes, key=lambda change: change.beat)
    if changes:
        numerator, denominator = changes[0].signature
    else:
        numerator, denominator = metadata.time_signature
    cursor = Fraction(0)
    boundaries = [cursor]
    change_index = 0

    initial_capacity = Fraction(numerator * 4, denominator)
    pickup = Fraction(metadata.pickup_beats)
    # Keep this validation identical to ``musicxml._pickup_length``.  A
    # pickup that is as long as (or longer than) a complete opening measure
    # is not a pickup; treating it as one here would make quantisation and
    # MusicXML disagree about every later barline.
    if pickup < 0 or pickup >= initial_capacity:
        pickup = Fraction(0)
    if pickup > 0:
        cursor = pickup
        boundaries.append(cursor)

    target = max(Fraction(0), through) + Fraction(numerator * 4, denominator)
    while cursor <= target:
        while change_index < len(changes) and changes[change_index].beat <= cursor:
            numerator, denominator = changes[change_index].signature
            change_index += 1
        measure_end = cursor + Fraction(numerator * 4, denominator)
        if change_index < len(changes):
            next_change = Fraction(changes[change_index].beat)
            if cursor < next_change < measure_end:
                measure_end = next_change
        if measure_end <= cursor:
            raise ValueError("拍号变化产生了零长度小节。")
        boundaries.append(measure_end)
        cursor = measure_end
    return boundaries


def _anchor_barline_rolls(
    groups: Sequence[AttackGroup],
    boundaries: Sequence[Fraction],
) -> None:
    """Notate a roll performed just before a barline on the new downbeat.

    Song Master often places the audible lead-in of a rolled chord slightly
    before the intended attack.  Once a group has already passed the strict
    monotonic/overlap arpeggio detector, an ending within one eighth of a beat
    of a barline is stronger notation evidence than its attack median.  Raw
    timing remains untouched for reporting and audio verification.
    """

    tolerance = Fraction(1, 8)
    for group in groups:
        if group.kind != "arpeggio" or not group.notes:
            continue
        first_attack = min(note.start_beat for note in group.notes)
        last_attack = max(note.start_beat for note in group.notes)
        index = bisect_left(boundaries, last_attack)
        candidates = [
            boundaries[candidate]
            for candidate in (index - 1, index)
            if 0 <= candidate < len(boundaries)
        ]
        if not candidates:
            continue
        boundary = min(candidates, key=lambda value: (abs(value - last_attack), value))
        if (
            first_attack < boundary
            and abs(last_attack - boundary) <= tolerance
        ):
            group.quantized_onset = boundary


def _rolled_chord_clusters(
    notes: list[NoteEvent],
    bpm: float,
    sensitivity: str,
) -> list[tuple[list[NoteEvent], str]]:
    """Identify short monotonic, overlapping attacks that form rolled chords."""

    if len(notes) < 2:
        return []
    # 180 ms expressed in beats, with sensible musical caps.
    time_span = Fraction(str(0.18 * bpm / 60.0)).limit_denominator(960)
    maximum_span = min(Fraction(1, 3), max(Fraction(3, 20), time_span))
    if sensitivity == "strict":
        maximum_span = min(maximum_span, Fraction(1, 4))
    elif sensitivity == "aggressive":
        maximum_span = min(Fraction(2, 5), maximum_span + Fraction(1, 16))

    clusters: list[tuple[list[NoteEvent], str]] = []
    used: set[str | int] = set()
    ordered = sorted(notes, key=lambda note: (note.start_beat, note.pitch))
    for index, first in enumerate(ordered):
        if first.note_id in used:
            continue
        candidates = [first]
        for other in ordered[index + 1 :]:
            if other.start_beat - first.start_beat > maximum_span:
                break
            if other.note_id in used or other.pitch == candidates[-1].pitch:
                continue
            if other.start_beat - candidates[-1].start_beat > Fraction(1, 8):
                # A notated tuplet/scale step is not a rolled chord merely
                # because preceding tones overlap.  Real sample rolls cluster
                # well below this per-tone spacing.
                break
            # A block of tones sharing the same later onset is not a sequence
            # of rolled attacks.  Require each added tone to contribute an
            # audible step in time (about 6 ms at 100 BPM or more).
            if other.start_beat - candidates[-1].start_beat < Fraction(1, 100):
                continue
            # All tones must still be sounding through most of the roll.
            if min(note.end_beat for note in candidates) < other.start_beat + Fraction(1, 8):
                continue
            candidates.append(other)
            if len(candidates) >= 6:
                break
        if len(candidates) < 2:
            continue
        differences = [b.pitch - a.pitch for a, b in zip(candidates, candidates[1:])]
        monotonic_up = all(diff > 0 for diff in differences)
        monotonic_down = all(diff < 0 for diff in differences)
        if not (monotonic_up or monotonic_down):
            continue
        span = candidates[-1].start_beat - candidates[0].start_beat
        # Simultaneous notes (or a few ticks of detector jitter) are ordinary
        # block chords, not rolled chords.  A visible arpeggio needs a real
        # ordered spread of at least roughly 12 ms at 100 BPM.
        if span < Fraction(1, 48):
            continue
        # Two-tone rolls are ambiguous with melody/grace notes.  Only accept a
        # very tight, wide interval with long overlap.
        if len(candidates) == 2:
            if sensitivity == "strict":
                continue
            if span > Fraction(1, 12) or abs(differences[0]) < 7:
                continue
            if min(note.end_beat for note in candidates) - candidates[-1].start_beat < Fraction(1, 2):
                continue
        # Do not collapse an ordinary sequence that already sits on a stable
        # sixteenth grid.  Genuine rolls have short, non-grid internal gaps.
        gaps = [b.start_beat - a.start_beat for a, b in zip(candidates, candidates[1:])]
        if all(gap >= Fraction(1, 4) for gap in gaps):
            continue
        direction = "up" if monotonic_up else "down"
        clusters.append((candidates, direction))
        used.update(note.note_id for note in candidates)
    return clusters


def _build_attack_groups(
    notes: list[NoteEvent], settings: ConversionSettings, bpm: float
) -> list[AttackGroup]:
    groups: list[AttackGroup] = []
    consumed: set[str | int] = set()
    by_source_staff: dict[tuple[int, str], list[NoteEvent]] = defaultdict(list)
    for note in notes:
        by_source_staff[(note.source_index, str(note.staff))].append(note)

    arpeggio_serial = 0
    if settings.detect_arpeggios:
        for source_notes in by_source_staff.values():
            for cluster, direction in _rolled_chord_clusters(
                source_notes, bpm, settings.arpeggio_sensitivity
            ):
                cluster_ids = {note.note_id for note in cluster}
                if any(
                    note.note_id not in cluster_ids
                    and cluster[0].start_beat - Fraction(1, 20)
                    <= note.start_beat
                    <= cluster[-1].start_beat + Fraction(1, 20)
                    for note in source_notes
                ):
                    # A simultaneous extra chord tone means the monotonic
                    # subset is not a self-contained roll.  Reject the subset
                    # so the ordinary source-local chord grouper can retain
                    # every pitch in one attack instead of creating two
                    # overlapping events in this MIDI's sole voice.
                    continue
                group_id = f"arp:{arpeggio_serial}"
                arpeggio_serial += 1
                anchor = _median_fraction([note.start_beat for note in cluster])
                group = AttackGroup(
                    group_id=group_id,
                    staff=cluster[0].staff,
                    notes=cluster,
                    raw_anchor=anchor,
                    kind="arpeggio",
                    arpeggio_direction=direction,  # type: ignore[arg-type]
                    confidence=0.85,
                )
                for note in cluster:
                    note.arpeggio_id = group_id
                    note.quantization_kind = "arpeggio"
                    consumed.add(note.note_id)
                groups.append(group)

    # Near-simultaneous attacks become a chord *within one input MIDI only*.
    # The input file is the voice boundary: two stems that happen to attack on
    # the same tick must remain two independent MuseScore voices.
    remaining_by_source_staff: dict[tuple[int, str], list[NoteEvent]] = defaultdict(list)
    for note in notes:
        if note.note_id not in consumed:
            remaining_by_source_staff[(note.source_index, str(note.staff))].append(note)
    serial = 0
    chord_window = Fraction(1, 20)  # 0.05 beats after latency correction
    for (source, _staff), source_notes in sorted(remaining_by_source_staff.items()):
        ordered = sorted(source_notes, key=lambda note: (note.start_beat, note.pitch))
        index = 0
        while index < len(ordered):
            first = ordered[index]
            cluster = [first]
            cursor = index + 1
            while cursor < len(ordered):
                candidate = ordered[cursor]
                if candidate.start_beat - first.start_beat > chord_window:
                    break
                cluster.append(candidate)
                cursor += 1
            anchor = _median_fraction([note.start_beat for note in cluster])
            groups.append(
                AttackGroup(
                    group_id=f"atk:{source}:{serial}",
                    staff=first.staff,
                    notes=cluster,
                    raw_anchor=anchor,
                    kind="binary",
                )
            )
            serial += 1
            index = cursor
    groups.sort(
        key=lambda group: (
            str(group.staff),
            min(note.source_index for note in group.notes),
            group.raw_anchor,
            str(group.group_id),
        )
    )
    return groups


@dataclass(frozen=True)
class _TupletMatch:
    onset: Fraction
    tuplet_id: str
    actual_notes: int
    normal_notes: int
    slot: Fraction
    group_end: Fraction
    source_index: int


@dataclass(frozen=True)
class _OrnamentMatch:
    leader_id: str | int
    onset: Fraction
    end: Fraction
    kind: str


def _tuplet_candidates(
    groups: Sequence[AttackGroup],
    straight_step: Fraction,
    sensitivity: str,
    time_signature: tuple[int, int] = (4, 4),
    unresolved_regions: list[tuple[Fraction, Fraction, str]] | None = None,
    measure_boundaries: Sequence[Fraction] | None = None,
) -> dict[tuple[str | int, int], _TupletMatch]:
    """Return complete, anchored common tuplets that beat a straight grid.

    The detector operates on attack groups, so a block chord never becomes a
    tuplet merely because it contains several pitches.  Ratios are conservative
    common notation defaults; an incomplete or boundary-ambiguous group is not
    rewritten.
    """

    result: dict[tuple[str | int, int], _TupletMatch] = {}
    by_stream: dict[tuple[int, str], list[AttackGroup]] = defaultdict(list)
    for group in groups:
        if group.kind != "arpeggio":
            for source in {note.source_index for note in group.notes}:
                by_stream[(source, str(group.staff))].append(group)
    thresholds = {
        "strict": (0.11, 0.24, 0.12),
        "normal": (0.16, 0.19, 0.075),
        "aggressive": (0.22, 0.15, 0.05),
    }[sensitivity]
    serial = 0
    measure_beats = Fraction(time_signature[0] * 4, time_signature[1])
    for (source, staff), staff_groups in by_stream.items():
        staff_groups.sort(key=lambda group: group.raw_anchor)
        stream_onsets = [group.raw_anchor for group in staff_groups]
        # Larger or less common ratios are tested first.  A six-note pattern
        # still has to beat the straight grid strongly; ordinary pairs of
        # triplets are therefore not gratuitously relabelled as 6:4.
        # Each ratio is evaluated at several written base-note values.  For a
        # ratio A:N the occupied performed span is N * base_duration.  This
        # covers, for example, both eighth-note and quarter-note triplets and
        # half-beat as well as one/two-beat quintuplets without hard-coding one
        # duration per ratio.
        ratio_bases = {
            (9, 8): (Fraction(1, 8), Fraction(1, 4), Fraction(1, 2)),
            (13, 8): (Fraction(1, 8), Fraction(1, 4)),
            (12, 8): (Fraction(1, 8), Fraction(1, 4)),
            (11, 8): (Fraction(1, 8), Fraction(1, 4)),
            (7, 8): (Fraction(1, 8), Fraction(1, 4)),
            (7, 4): (Fraction(1, 8), Fraction(1, 4), Fraction(1, 2), Fraction(1)),
            (6, 4): (Fraction(1, 8), Fraction(1, 4), Fraction(1, 2), Fraction(1)),
            (5, 3): (Fraction(1, 4), Fraction(1, 2), Fraction(1)),
            (5, 4): (Fraction(1, 8), Fraction(1, 4), Fraction(1, 2), Fraction(1)),
            (4, 3): (Fraction(1, 4), Fraction(1, 2), Fraction(1)),
            (3, 2): (Fraction(1, 4), Fraction(1, 2), Fraction(1), Fraction(2)),
            (2, 3): (Fraction(1, 4), Fraction(1, 2), Fraction(1)),
        }
        templates = tuple(
            (actual, normal, normal * base)
            for (actual, normal), bases in ratio_bases.items()
            for base in bases
        )
        # When identical attacks admit the same span under more than one
        # notation ratio (e.g. 7:8 with a smaller base vs 7:4 with a larger
        # base), prefer the conventional smaller normal count.
        templates = tuple(
            sorted(templates, key=lambda item: (item[2], -item[0], item[1]))
        )
        compound_meter = time_signature[1] == 8 and time_signature[0] >= 6 and time_signature[0] % 3 == 0
        if not staff_groups:
            continue
        # Tuplets may begin on a quarter-beat or still finer written boundary;
        # restricting anchors to beats/half-beats destroys perfectly valid
        # mid-beat quintuplets.  Keep anchors on a conservative binary lattice
        # (one level finer than the requested ordinary grid).
        anchor_step = min(Fraction(1, 4), straight_step)
        phase_candidates = {Fraction(0)}
        minimum = math.floor(float(staff_groups[0].raw_anchor / anchor_step))
        maximum = math.ceil(float(staff_groups[-1].raw_anchor / anchor_step))
        anchors = sorted(
            {
                anchor_index * anchor_step + phase
                for phase in phase_candidates
                for anchor_index in range(minimum - 1, maximum + 2)
            }
        )
        for anchor in anchors:
            for actual, normal, span in templates:
                if actual in {2, 4} and normal == 3 and not compound_meter:
                    continue
                group_end = anchor + span
                end_probe = group_end - Fraction(1, 1000000)
                if measure_boundaries:
                    anchor_measure = bisect_right(measure_boundaries, anchor) - 1
                    end_measure = bisect_right(measure_boundaries, end_probe) - 1
                    crosses_barline = anchor_measure != end_measure
                else:
                    crosses_barline = (
                        anchor // measure_beats != end_probe // measure_beats
                    )
                if crosses_barline:
                    # A single MusicXML tuplet cannot safely straddle a barline;
                    # MuseScore rewrites it into unrelated fragments.  Leave
                    # such ambiguous input unfolded for manual review.
                    left = bisect_left(stream_onsets, anchor)
                    right = bisect_left(stream_onsets, group_end)
                    boundary_members = staff_groups[left:right]
                    if len(boundary_members) == actual and unresolved_regions is not None:
                        boundary_members.sort(key=lambda group: group.raw_anchor)
                        boundary_step = span / actual
                        boundary_targets = [
                            anchor + index * boundary_step for index in range(actual)
                        ]
                        boundary_phase = _median_fraction(
                            [
                                group.raw_anchor - target
                                for group, target in zip(
                                    boundary_members, boundary_targets
                                )
                            ]
                        )
                        if abs(boundary_phase) <= min(
                            Fraction(3, 20), boundary_step * Fraction(2, 5)
                        ) and max(
                            abs(
                                group.raw_anchor - target - boundary_phase
                            )
                            for group, target in zip(
                                boundary_members, boundary_targets
                            )
                        ) <= boundary_step * Fraction(1, 5):
                            unresolved_regions.append(
                                (anchor, group_end, "跨小节连音候选")
                            )
                    continue
                left = bisect_left(stream_onsets, anchor)
                right = bisect_left(stream_onsets, anchor + span)
                members = [
                    group for group in staff_groups[left:right]
                    if (group.group_id, source) not in result
                ]
                if len(members) != actual:
                    continue
                members.sort(key=lambda group: group.raw_anchor)
                step = span / actual
                targets = [anchor + index * step for index in range(actual)]
                positions = [group.raw_anchor for group in members]
                shared_phase = _median_fraction(
                    [position - target for position, target in zip(positions, targets)]
                )
                phase_limit = min(Fraction(3, 20), step * Fraction(2, 5))
                if abs(shared_phase) > phase_limit:
                    continue
                gaps = [right - left for left, right in zip(positions, positions[1:])]
                if gaps and (min(gaps) <= 0 or max(gaps) / min(gaps) > Fraction(3, 2)):
                    continue
                if right < len(stream_onsets):
                    following_gap = stream_onsets[right] - group_end
                    continuation_gap = stream_onsets[right] - positions[-1]
                    if gaps:
                        median_gap = _median_fraction(gaps)
                        next_starts_new_nominal_group = (
                            abs(following_gap - shared_phase)
                            <= max(Fraction(1, 960), step * Fraction(3, 20))
                        )
                        if (
                            not next_starts_new_nominal_group
                            and median_gap * Fraction(7, 10)
                            <= continuation_gap
                            <= median_gap * Fraction(13, 10)
                        ):
                            # This is a slice of one larger equal run, not a
                            # complete smaller tuplet.  A repeated next group
                            # with the same transcription phase is allowed.
                            continue
                tuplet_error = statistics.mean(
                    float(abs(position - target - shared_phase) / step)
                    for position, target in zip(positions, targets)
                )
                binary_error = _binary_sequence_error(
                    positions, anchor, straight_step
                )
                max_tuplet, min_binary, min_gain = thresholds
                # Complexity penalty prevents exotic ratios winning on tiny
                # numerical improvements.  Exact synthetic/performed groups
                # still pass comfortably.
                complexity = 0.006 * max(0, actual - 3)
                duration_evidence = all(
                    max(note.end_beat - note.start_beat for note in member.notes)
                    >= step * Fraction(3, 5)
                    for member in members
                )
                compound_exception = (
                    compound_meter
                    and normal == 3
                    and actual in {2, 4, 5}
                    and duration_evidence
                )
                if tuplet_error > max_tuplet or (
                    not compound_exception
                    and (
                        binary_error < min_binary
                        or binary_error - tuplet_error < min_gain + complexity
                    )
                ):
                    continue
                if actual == 6 and normal == 4:
                    velocities = [max(note.velocity for note in member.notes) for member in members]
                    # A clear re-accent halfway favours two 3:2 groups over
                    # one 6:4 group.  With no phrasing evidence we keep 6:4.
                    if velocities[3] >= velocities[0] * 0.95 and velocities[3] > statistics.mean(velocities[1:3]) * 1.08:
                        continue
                tuplet_id = f"tuplet:{actual}:{normal}:{source}:{staff}:{serial}"
                serial += 1
                for group, snapped in zip(members, targets):
                    result[(group.group_id, source)] = _TupletMatch(
                        snapped,
                        tuplet_id,
                        actual,
                        normal,
                        step,
                        anchor + span,
                        source,
                    )
                break
    return result


def _grace_candidates(
    groups: Sequence[AttackGroup],
    bpm: float,
    straight_step: Fraction,
    excluded: set[tuple[str | int, int]] | None = None,
) -> dict[tuple[str | int, int], tuple[Fraction, int, bool]]:
    """Find short lead-in notes resolving immediately to a stable main note.

    MIDI alone cannot prove a grace-note interpretation.  These intentionally
    strict rules require a short, off-grid, non-overlapping note followed by a
    substantially longer main attack in the same source/staff and nearby pitch.
    Returned onsets are the main-note onset so MusicXML can emit grace notes
    immediately before it without advancing score time.
    """

    result: dict[tuple[str | int, int], tuple[Fraction, int, bool]] = {}
    excluded = excluded or set()
    ordered = sorted(
        groups,
        key=lambda group: (str(group.staff), group.raw_anchor),
    )
    by_source_staff: dict[tuple[int, str], list[AttackGroup]] = defaultdict(list)
    for group in ordered:
        for source in {note.source_index for note in group.notes}:
            by_source_staff[(source, str(group.staff))].append(group)
    # About 150 ms, bounded musically so tempo extremes remain sane.
    maximum = min(Fraction(1, 4), max(Fraction(1, 10), Fraction(str(bpm * 0.15 / 60)).limit_denominator(960)))
    for (source, _staff), source_groups in by_source_staff.items():
        source_groups.sort(key=lambda group: group.raw_anchor)
        claimed: set[str | int] = set()
        order_by_target: dict[Fraction, int] = defaultdict(int)
        for main_index, main in enumerate(source_groups[1:], start=1):
            main_key = (main.group_id, source)
            if main_key in excluded or main.kind == "arpeggio":
                continue
            main_notes = [note for note in main.notes if note.source_index == source]
            if not main_notes:
                continue
            # With the one-MIDI/one-voice contract, a raw note-off after the
            # next attack is pedal resonance, not evidence that this is a
            # stable main note.  Use the next attack as its effective end;
            # only the final attack may use the recorded release.
            if main_index + 1 < len(source_groups):
                main_duration = (
                    source_groups[main_index + 1].raw_anchor - main.raw_anchor
                )
            else:
                main_duration = max(
                    note.end_beat - main.raw_anchor for note in main_notes
                )
            if (
                main_duration < Fraction(3, 8)
                or _absolute_distance(main.raw_anchor, straight_step) > straight_step / 4
            ):
                continue
            run: list[AttackGroup] = []
            next_group = main
            for candidate in reversed(source_groups[max(0, main_index - 4):main_index]):
                candidate_key = (candidate.group_id, source)
                notes = [note for note in candidate.notes if note.source_index == source]
                next_notes = [note for note in next_group.notes if note.source_index == source]
                if (
                    not notes
                    or not next_notes
                    or candidate.kind == "arpeggio"
                    or candidate_key in excluded
                    or candidate.group_id in claimed
                ):
                    break
                gap = next_group.raw_anchor - candidate.raw_anchor
                interval = min(
                    abs(note.pitch - other.pitch) for note in notes for other in next_notes
                )
                if not (
                    Fraction(1, 100) < gap <= maximum
                    and main_duration >= gap * 2
                    and interval <= 12
                ):
                    break
                run.append(candidate)
                next_group = candidate
            run.reverse()
            if not run:
                continue
            if main.raw_anchor - run[0].raw_anchor > Fraction(1, 2):
                continue
            # A grace note is a short lead-in, not a sustained pedal tone.
            # Song Master may keep a rolled chord's early pitches alive for
            # several beats; converting those long notes to zero-duration
            # grace notes is destructive.  Permit only a tiny detector tail
            # past the main attack and a genuinely short recorded duration.
            grace_notes = [
                note
                for member in run
                for note in member.notes
                if note.source_index == source
            ]
            if any(
                note.end_beat - note.start_beat > Fraction(3, 10)
                or note.end_beat - main.raw_anchor > Fraction(1, 16)
                for note in grace_notes
            ):
                continue
            approach = run + [main]
            approach_gaps = [
                right.raw_anchor - left.raw_anchor
                for left, right in zip(approach, approach[1:])
            ]
            # A long, evenly spaced fast passage is a written small-note run
            # (or an unsupported tuplet), not a chain of grace notes merely
            # because pedal kept every raw note-on alive.  Preserve all of
            # those attacks and let the low-confidence reporting path flag
            # grid collisions instead of silently collapsing them.
            if len(approach_gaps) >= 3:
                mean_gap = sum(approach_gaps, Fraction(0)) / len(approach_gaps)
                if mean_gap > 0 and max(
                    abs(gap - mean_gap) for gap in approach_gaps
                ) <= mean_gap * Fraction(3, 20):
                    continue
            # A metrically well-formed short note is more safely retained as
            # a real 16th/32nd.  At least one member must visibly fall between
            # the configured straight slots before the chain is collapsed.
            if not any(
                _absolute_distance(member.raw_anchor, straight_step) >= straight_step / 4
                for member in run
            ):
                continue
            target = _nearest(main.raw_anchor, straight_step)
            first_order = order_by_target[target]
            for local_order, member in enumerate(run):
                key = (member.group_id, source)
                following = (
                    run[local_order + 1]
                    if local_order + 1 < len(run)
                    else main
                )
                member_duration = following.raw_anchor - member.raw_anchor
                result[key] = (
                    main.raw_anchor,
                    first_order + local_order,
                    member_duration <= Fraction(1, 8),
                )
                claimed.add(member.group_id)
            order_by_target[target] += len(run)
    return result


def _detect_swing(
    groups: Sequence[AttackGroup],
    excluded: set[tuple[str | int, int]] | None = None,
) -> tuple[dict[tuple[str | int, int], Fraction], tuple[int, int] | None]:
    """Detect contiguous 2:1 eighth-note swing regions from repeated evidence."""

    excluded = excluded or set()
    by_stream: dict[tuple[int, str], list[AttackGroup]] = defaultdict(list)
    for group in groups:
        if group.kind != "arpeggio":
            for source in {note.source_index for note in group.notes}:
                if (group.group_id, source) in excluded:
                    continue
                by_stream[(source, str(group.staff))].append(group)
    mapping: dict[tuple[str | int, int], Fraction] = {}
    for (source, _staff), stream_groups in by_stream.items():
        early_ternary_beats: set[int] = set()
        for group in stream_groups:
            phase = group.raw_anchor - math.floor(float(group.raw_anchor))
            if Fraction(1, 4) <= phase <= Fraction(5, 12):
                early_ternary_beats.add(math.floor(float(group.raw_anchor)))
        candidates = []
        for group in stream_groups:
            phase = group.raw_anchor - math.floor(float(group.raw_anchor))
            beat_number = math.floor(float(group.raw_anchor))
            if (
                Fraction(3, 5) <= phase <= Fraction(11, 15)
                and beat_number not in early_ternary_beats
            ):
                candidates.append(group)
        onbeats = [
            group for group in stream_groups
            if abs(group.raw_anchor - _nearest(group.raw_anchor, Fraction(1)))
            <= Fraction(1, 12)
        ]
        if len(candidates) < 6 or len(onbeats) < 6:
            continue
        beat_numbers = {math.floor(float(group.raw_anchor)) for group in candidates}
        onbeat_numbers = {
            int(_nearest(group.raw_anchor, Fraction(1))) for group in onbeats
        }
        paired_beats = beat_numbers & onbeat_numbers
        if len(paired_beats) < 6:
            continue
        ordered_beats = sorted(paired_beats)
        runs: list[list[int]] = [[ordered_beats[0]]]
        for left, right in zip(ordered_beats, ordered_beats[1:]):
            if right == left + 1:
                runs[-1].append(right)
            else:
                runs.append([right])
        qualifying_beats = {
            beat for run in runs if len(run) >= 4 for beat in run
        }
        if len(qualifying_beats) < 6:
            continue
        for group in candidates:
            beat = math.floor(float(group.raw_anchor))
            if beat in qualifying_beats:
                mapping[(group.group_id, source)] = Fraction(beat) + Fraction(1, 2)
        for group in onbeats:
            beat = int(_nearest(group.raw_anchor, Fraction(1)))
            if beat in qualifying_beats:
                mapping[(group.group_id, source)] = Fraction(beat)
    return (mapping, (2, 1)) if mapping else ({}, None)


def _ornament_candidates(
    groups: Sequence[AttackGroup],
    excluded: set[tuple[str | int, int]],
    key_fifths: int = 0,
) -> dict[tuple[str | int, int], _OrnamentMatch]:
    """Collapse only sustained, very regular neighbour-note alternation.

    Short mordents and turns are intentionally not inferred: from MIDI alone
    they are too easily confused with written melodic notes.  A trill needs at
    least six attacks, exact ABAB alternation, a one/two-semitone neighbour and
    stable rapid gaps.  All original notes remain traceable in attack_groups.
    """

    result: dict[tuple[str | int, int], _OrnamentMatch] = {}
    tonic = (7 * int(key_fifths)) % 12
    scale = tuple((tonic + interval) % 12 for interval in (0, 2, 4, 5, 7, 9, 11))

    def is_diatonic_upper_neighbour(main_pitch: int, auxiliary_pitch: int) -> bool:
        if not 0 < auxiliary_pitch - main_pitch <= 2:
            return False
        try:
            degree = scale.index(main_pitch % 12)
        except ValueError:
            return False
        return auxiliary_pitch % 12 == scale[(degree + 1) % len(scale)]

    by_source_staff: dict[tuple[int, str], list[tuple[AttackGroup, int]]] = defaultdict(list)
    for group in groups:
        if group.kind == "arpeggio":
            continue
        for source in {note.source_index for note in group.notes}:
            if (group.group_id, source) in excluded:
                continue
            source_notes = [note for note in group.notes if note.source_index == source]
            if len(source_notes) == 1:
                by_source_staff[(source, str(group.staff))].append(
                    (group, source_notes[0].pitch)
                )
    for (source, _staff), source_groups in by_source_staff.items():
        source_groups.sort(key=lambda item: item[0].raw_anchor)
        index = 0
        while index + 5 < len(source_groups):
            run = source_groups[index:index + 2]
            # Only an upper neighbour implied by the current key signature is
            # collapsed.  A chromatic auxiliary would require an explicit
            # accidental-mark; without reliable MIDI spelling, keeping its
            # expanded pitches is safer than silently changing playback.
            if len(run) < 2 or not is_diatonic_upper_neighbour(
                run[0][1], run[1][1]
            ):
                # Do not slide one attack into a lower-neighbour ABAB run and
                # then reinterpret its second pitch as a new upper-neighbour
                # trill.  Skip the complete regular alternation as one
                # intentionally uncollapsed gesture.
                cursor = index + 2
                while cursor < len(source_groups):
                    group = source_groups[cursor][0]
                    previous = source_groups[cursor - 1][0]
                    if (
                        source_groups[cursor][1] != source_groups[cursor - 2][1]
                        or group.raw_anchor - previous.raw_anchor <= 0
                        or group.raw_anchor - previous.raw_anchor > Fraction(1, 5)
                    ):
                        break
                    cursor += 1
                index = cursor if cursor - index >= 6 else index + 1
                continue
            cursor = index + 2
            while cursor < len(source_groups):
                group, pitch = source_groups[cursor]
                previous = source_groups[cursor - 1][0]
                gap = group.raw_anchor - previous.raw_anchor
                if gap <= 0 or gap > Fraction(1, 5):
                    break
                if pitch != source_groups[cursor - 2][1]:
                    break
                run.append((group, pitch))
                cursor += 1
            if len(run) < 6:
                index += 1
                continue
            gaps = [
                right[0].raw_anchor - left[0].raw_anchor
                for left, right in zip(run, run[1:])
            ]
            mean_gap = sum(gaps, Fraction(0)) / len(gaps)
            if mean_gap <= 0:
                index += 1
                continue
            maximum_deviation = max(abs(gap - mean_gap) for gap in gaps) / mean_gap
            if maximum_deviation > Fraction(3, 10):
                index += 1
                continue
            first_group = run[0][0]
            last_group = run[-1][0]
            end = max(note.end_beat for note in last_group.notes)
            match = _OrnamentMatch(
                leader_id=first_group.group_id,
                onset=first_group.raw_anchor,
                end=max(end, last_group.raw_anchor + mean_gap),
                kind="trill",
            )
            for group, _pitch in run:
                result[(group.group_id, source)] = match
                group.kind = "ornament"
            index = cursor
    return result


def _quantize_duration(
    raw_duration: Fraction,
    onset: Fraction,
    next_onset: Fraction | None,
    step: Fraction,
    tuplet: _TupletMatch | None,
) -> Fraction:
    if tuplet:
        # The attack lattice is an actual equal subdivision of the normal
        # span.  Keep at least one slot and let MusicXML carry the ratio.
        duration_step = tuplet.slot
    else:
        duration_step = step
    # Audio-derived note-offs often include room/reverb tails.  Do not let a
    # note extend through the next attack in the same source line.
    available = next_onset - onset if next_onset is not None and next_onset > onset else None
    target = raw_duration
    if available is not None:
        target = min(target, available)
        if target >= available * Fraction(4, 5):
            target = available
    duration = _nearest(max(target, duration_step), duration_step)
    duration = max(duration_step, duration)
    if available is not None:
        duration = min(duration, available)
    return max(duration_step, duration)


def _deduplicate_chords(
    chords: list[ChordEvent], report: ConversionReport
) -> list[ChordEvent]:
    result: list[ChordEvent] = []
    merged = 0
    for chord in sorted(chords, key=lambda item: (str(item.staff), item.onset, item.voice or 0)):
        unique: dict[int, NoteEvent] = {}
        for note in chord.notes:
            existing = unique.get(note.pitch)
            if existing is None:
                unique[note.pitch] = note
            else:
                merged += 1
                if note.velocity > existing.velocity:
                    unique[note.pitch] = note
        chord.notes = sorted(unique.values(), key=lambda note: note.pitch)
        result.append(chord)
    report.merged_duplicates += merged
    return result


def _enforce_single_voice_timelines(
    chords: list[ChordEvent],
    tuplet_bounds: dict[str, tuple[Fraction, Fraction]] | None = None,
) -> list[ChordEvent]:
    """Make every ``(source, staff)`` event stream strictly monophonic.

    This final invariant pass intentionally runs after all special-rhythm
    classification.  Free/on-grid collision fallbacks, unfolded tuplets and
    collapsed ornaments can otherwise leave tiny overlaps even though their
    input source is declared to be one voice.  Grace notes do not advance the
    stream; every ordinary chord is held exactly to the next distinct attack.
    When a tuplet ends before the next attack, the last member is closed at
    the detected group boundary and an explicit ordinary continuation is tied
    to it.  Thus the model itself has no gap and the ratio remains valid for
    every output backend.
    """

    tuplet_bounds = tuplet_bounds or {}
    by_source: dict[int, list[ChordEvent]] = defaultdict(list)
    for chord in chords:
        sources = {note.source_index for note in chord.notes}
        if len(sources) != 1:
            raise ValueError("内部错误：单声部时间线包含多个 MIDI 来源。")
        by_source[next(iter(sources))].append(chord)

    result: list[ChordEvent] = []
    for source_chords in by_source.values():
        ordered = sorted(
            source_chords,
            key=lambda item: (
                item.onset,
                0 if item.grace else 1,
                item.grace_order,
                item.event_id,
            ),
        )
        ordinary_onsets = sorted({chord.onset for chord in ordered if not chord.grace})
        next_by_onset = {
            onset: following
            for onset, following in zip(ordinary_onsets, ordinary_onsets[1:])
        }
        for chord in ordered:
            continuation: ChordEvent | None = None
            if chord.grace:
                chord.duration = Fraction(0)
            else:
                following = next_by_onset.get(chord.onset)
                if following is not None:
                    bounds = tuplet_bounds.get(str(chord.tuplet_id))
                    if (
                        bounds is not None
                        and chord.onset < bounds[1] < following
                    ):
                        chord.duration = bounds[1] - chord.onset
                        tied_note_ids = {note.note_id for note in chord.notes}
                        chord.tie_starts.update(tied_note_ids)
                        continuation_notes = [copy.copy(note) for note in chord.notes]
                        continuation = ChordEvent(
                            event_id=f"{chord.event_id}:continuation",
                            staff=chord.staff,
                            onset=bounds[1],
                            duration=following - bounds[1],
                            notes=continuation_notes,
                            continuation=True,
                            confidence=chord.confidence,
                            tie_stops=set(tied_note_ids),
                        )
                    else:
                        chord.duration = following - chord.onset
                elif chord.duration <= 0:
                    chord.duration = Fraction(1, 32)
            for note in chord.notes:
                note.quantized_start = chord.onset
                note.quantized_end = chord.end
            result.append(chord)
            if continuation is not None:
                for note in continuation.notes:
                    note.quantized_start = continuation.onset
                    note.quantized_end = continuation.end
                    note.triplet_id = None
                    note.quantization_kind = "binary"
                    note.tie_stop = True
                for note in chord.notes:
                    note.tie_start = True
                result.append(continuation)
    return result


def _allocate_voices(
    chords: list[ChordEvent],
    staff: str,
) -> list[VoiceLine]:
    """Map every input MIDI to exactly one fixed voice on ``staff``.

    MIDI note-off overlap is treated as pedal/resonance, never as evidence for
    an extra notated voice.  MuseScore exposes four voices per staff, so more
    than four input files on one staff cannot satisfy the one-file/one-voice
    contract and is rejected explicitly instead of being silently merged.
    """

    by_source: dict[int, list[ChordEvent]] = defaultdict(list)
    source_labels: dict[int, str] = {}
    for chord in chords:
        sources = {note.source_index for note in chord.notes}
        if len(sources) != 1:
            raise ValueError(
                "内部错误：一个记谱事件混入了多个 MIDI 来源；"
                "每个输入文件必须保持独立声部。"
            )
        source = next(iter(sources))
        by_source[source].append(chord)
        source_labels[source] = chord.notes[0].source_path.name

    sources = sorted(by_source)
    if len(sources) > 4:
        names = "、".join(source_labels[source] for source in sources)
        staff_name = "高音" if staff == "treble" else "低音"
        raise ValueError(
            f"{staff_name}谱表分配了 {len(sources)} 个 MIDI（{names}），"
            "但 MuseScore 每个谱表最多四个声部。请把部分文件改分到另一谱表。"
        )

    base_voice = 1 if staff == "treble" else 5
    lines: list[VoiceLine] = []
    for voice_offset, source in enumerate(sources):
        voice_number = base_voice + voice_offset
        line = VoiceLine(
            staff=staff,
            voice_number=voice_number,
            source_affinity={source},
        )
        cursor = Fraction(-1)
        ordered = sorted(
            by_source[source],
            key=lambda item: (
                item.onset,
                0 if item.grace else 1,
                item.grace_order,
                item.event_id,
            ),
        )
        for chord in ordered:
            if not chord.grace and chord.onset < cursor:
                raise ValueError(
                    f"内部错误：MIDI {source_labels[source]} 的单声部事件仍然重叠："
                    f"第 {float(chord.onset):g} 拍早于上一事件结尾 {float(cursor):g}。"
                )
            chord.voice = voice_number
            for note in chord.notes:
                note.quantized_start = chord.onset
                note.quantized_end = chord.end
            line.events.append(chord)
            if not chord.grace:
                cursor = chord.end
        lines.append(line)
    return lines


def normalize(
    files: Sequence[MidiFileData],
    settings: ConversionSettings,
    report: ConversionReport | None = None,
) -> NormalizedScore:
    """Normalize parsed MIDI files into a two-staff score model."""

    if not files:
        raise ValueError("没有可量化的 MIDI 数据。")
    report = report or ConversionReport(files=[file.report for file in files])
    # ``MidiFileData`` can be previewed and then converted with the same
    # objects.  Derived counters must describe this normalization run rather
    # than accumulate every preview pass.
    for file in files:
        file.report.pedal_tails_trimmed = 0
    straight_step = settings.smallest_note_beats
    from .metadata import infer_score_metadata, key_name, shift_metadata

    metadata = infer_score_metadata(files, settings)
    bpm = metadata.bpm
    signature = metadata.time_signature
    key = metadata.key_fifths

    notes: list[NoteEvent] = []
    latency_by_source: dict[int, Fraction] = {}
    for file in files:
        source_notes = [copy.copy(note) for note in file.notes]
        bias = _estimate_latency(source_notes, straight_step) if settings.auto_latency else Fraction(0)
        latency_by_source[file.source_index] = bias
        file.report.latency_bias_beats = bias
        for note in source_notes:
            note.start_beat -= bias
            note.end_beat -= bias
            notes.append(note)

    trim_offset = Fraction(0)
    if settings.auto_trim and notes:
        # Remove only whole common bars.  This preserves pickups and manual
        # inter-stem alignment while discarding Song Master's shared pre-roll.
        measure_beats = Fraction(signature[0] * 4, signature[1])
        earliest = min(note.start_beat for note in notes)
        # Latency estimation is robust rather than exact, so an attack a few
        # milliseconds before a bar line still represents that common pre-roll.
        trim_tolerance = min(Fraction(1, 5), straight_step / 2)
        if earliest + trim_tolerance >= measure_beats:
            trim_offset = ((earliest + trim_tolerance) // measure_beats) * measure_beats
            for note in notes:
                note.start_beat -= trim_offset
                note.end_beat -= trim_offset
    metadata = shift_metadata(metadata, trim_offset)
    bpm = metadata.bpm
    signature = metadata.time_signature
    key = metadata.key_fifths

    attack_groups = _build_attack_groups(notes, settings, bpm)
    measure_boundaries = _measure_boundaries(
        metadata,
        max((note.start_beat for note in notes), default=Fraction(0)),
    )
    _anchor_barline_rolls(attack_groups, measure_boundaries)
    unresolved_regions: list[tuple[Fraction, Fraction, str]] = []
    initial_tuplet_map = (
        _tuplet_candidates(
            attack_groups,
            straight_step,
            settings.triplet_sensitivity,
            signature,
            unresolved_regions,
            measure_boundaries,
        )
        if settings.detect_triplets
        else {}
    )
    grace_map = (
        _grace_candidates(
            attack_groups,
            bpm,
            straight_step,
            set(initial_tuplet_map),
        )
        if getattr(settings, "detect_grace_notes", True)
        else {}
    )
    ornament_map = (
        _ornament_candidates(
            attack_groups,
            set(grace_map) | set(initial_tuplet_map),
            key,
        )
        if getattr(settings, "detect_ornaments", True)
        else {}
    )
    swing_map, swing_ratio = (
        _detect_swing(
            attack_groups,
            set(grace_map) | set(ornament_map) | set(initial_tuplet_map),
        )
        if getattr(settings, "detect_swing", True)
        else ({}, None)
    )
    tuplet_map = {
        key: match
        for key, match in initial_tuplet_map.items()
        if key not in grace_map and key not in ornament_map and key not in swing_map
    }

    # Next group onset per source/staff guides duration reconstruction.
    groups_by_source_staff: dict[tuple[int, str], list[AttackGroup]] = defaultdict(list)
    for group in attack_groups:
        sources = {note.source_index for note in group.notes}
        for source in sources:
            groups_by_source_staff[(source, str(group.staff))].append(group)
    next_by_group_source: dict[tuple[str | int, int], Fraction] = {}
    for (source, _staff), source_groups in groups_by_source_staff.items():
        ordered = sorted(source_groups, key=lambda group: group.raw_anchor)
        for left, right in zip(ordered, ordered[1:]):
            next_by_group_source[(left.group_id, source)] = right.raw_anchor

    chords: list[ChordEvent] = []
    tuplets_by_id: dict[str, list[str | int]] = defaultdict(list)
    tuplet_ratios: dict[str, tuple[int, int]] = {}
    tuplet_bounds: dict[str, tuple[Fraction, Fraction]] = {}
    clipped_tuplet_tails = 0
    review_onsets: set[Fraction] = set()

    def resolved_onset(candidate: AttackGroup, source: int) -> Fraction:
        if candidate.quantized_onset is not None:
            return candidate.quantized_onset
        key = (candidate.group_id, source)
        grace = grace_map.get(key)
        if grace:
            target = next(
                (
                    other for other in attack_groups
                    if other.staff == candidate.staff
                    and other.raw_anchor == grace[0]
                    and any(note.source_index == source for note in other.notes)
                ),
                None,
            )
            if target is not None and target.group_id != candidate.group_id:
                return resolved_onset(target, source)
            return _nearest(grace[0], straight_step)
        tuplet = tuplet_map.get(key)
        if tuplet:
            return tuplet.onset
        if key in swing_map:
            return swing_map[key]
        ornament = ornament_map.get(key)
        if ornament:
            return _nearest(ornament.onset, straight_step)
        return _nearest(candidate.raw_anchor, straight_step)

    for serial, group in enumerate(attack_groups):
        notes_by_source: dict[int, list[NoteEvent]] = defaultdict(list)
        for note in group.notes:
            notes_by_source[note.source_index].append(note)
        for batch_index, (source, batch) in enumerate(sorted(notes_by_source.items())):
            event_key = (group.group_id, source)
            grace_info = grace_map.get(event_key)
            tuplet_info = tuplet_map.get(event_key)
            ornament_info = ornament_map.get(event_key)
            swing_onset = swing_map.get(event_key)
            if ornament_info and group.group_id != ornament_info.leader_id:
                continue

            grace_order = grace_info[1] if grace_info else 0
            grace_slash = grace_info[2] if grace_info else True
            if grace_info:
                onset = resolved_onset(group, source)
                kind = "grace"
            elif tuplet_info:
                onset = tuplet_info.onset
                kind = (
                    "triplet"
                    if tuplet_info.actual_notes == 3
                    and tuplet_info.normal_notes == 2
                    else "tuplet"
                )
                tuplet_ratios[tuplet_info.tuplet_id] = (
                    tuplet_info.actual_notes,
                    tuplet_info.normal_notes,
                )
                tuplet_bounds[tuplet_info.tuplet_id] = (
                    tuplet_info.group_end
                    - tuplet_info.slot * tuplet_info.actual_notes,
                    tuplet_info.group_end,
                )
            elif ornament_info:
                onset = _nearest(ornament_info.onset, straight_step)
                kind = "ornament"
            elif swing_onset is not None:
                onset = swing_onset
                kind = "swing"
            else:
                onset = resolved_onset(group, source)
                kind = "arpeggio" if group.kind == "arpeggio" else "binary"
                if kind == "binary":
                    snapped_here = _nearest(group.raw_anchor, straight_step)
                    collision = any(
                        other is not group
                        and other.staff == group.staff
                        and _nearest(other.raw_anchor, straight_step) == snapped_here
                        and any(note.source_index == source for note in other.notes)
                        for other in attack_groups
                    )
                    if collision:
                        # Preserve both attacks on a notation-safe binary
                        # sub-grid.  Falling back to the arbitrary raw MIDI
                        # fraction creates durations whose MusicXML ``type``
                        # cannot describe them exactly; MuseScore then rounds
                        # those fragments and may lengthen the whole measure,
                        # inserting a compensating rest on the other staff.
                        #
                        # Separate attack groups are more than 1/20 beat
                        # apart, so a 1/32-beat (128th-note) lattice keeps
                        # their order and cannot merge them.
                        onset = _nearest(group.raw_anchor, Fraction(1, 32))
                        kind = "free"
                        review_onsets.add(onset)

            if group.quantized_onset is None:
                group.quantized_onset = onset
                group.kind = kind  # type: ignore[assignment]
            tuplet_id = tuplet_info.tuplet_id if tuplet_info else None
            next_onset = next_by_group_source.get(event_key)
            if next_onset is not None:
                following_group = next(
                    (
                        other for other in attack_groups
                        if other.raw_anchor == next_onset
                        and other.staff == group.staff
                        and any(note.source_index == source for note in other.notes)
                    ),
                    None,
                )
                next_onset = (
                    resolved_onset(following_group, source)
                    if following_group is not None
                    else _nearest(next_onset, straight_step)
                )
            raw_end = max(
                ornament_info.end if ornament_info else note.end_beat
                for note in batch
            )
            raw_duration = max(Fraction(1, 32), raw_end - group.raw_anchor)
            # One MIDI is one notated voice.  Every tone in this attack chord
            # receives one common duration, capped unconditionally at the
            # next logical attack from that MIDI.  Long note-offs beyond the
            # boundary are pedal/resonance tails, not additional voices.
            capped_by_next = (
                next_onset is not None
                and next_onset > onset
                and raw_end > next_onset + Fraction(1, 960)
            )
            if capped_by_next:
                next(
                    file.report for file in files if file.source_index == source
                ).pedal_tails_trimmed += sum(
                    note.end_beat > next_onset + Fraction(1, 960)
                    for note in batch
                )
            if grace_info:
                duration = Fraction(0)
            elif tuplet_info:
                if raw_end > tuplet_info.group_end + tuplet_info.slot / 3:
                    clipped_tuplet_tails += 1
                logical_end = min(
                    next_onset
                    if next_onset is not None and next_onset > onset
                    else tuplet_info.group_end,
                    tuplet_info.group_end,
                )
                duration = _quantize_duration(
                    raw_duration,
                    onset,
                    logical_end,
                    straight_step,
                    tuplet_info,
                )
            elif swing_onset is not None:
                duration = min(
                    Fraction(1, 2),
                    next_onset - onset
                    if next_onset is not None and next_onset > onset
                    else Fraction(1, 2),
                )
            else:
                duration = (
                    next_onset - onset
                    if next_onset is not None and next_onset > onset
                    else _quantize_duration(
                        raw_duration,
                        onset,
                        None,
                        straight_step,
                        None,
                    )
                )

            duration_batches = {duration: batch}
            for duration_index, (duration, duration_notes) in enumerate(
                duration_batches.items()
            ):
                event_id = f"chord:{serial}:{batch_index}:{duration_index}"
                for note in duration_notes:
                    note.quantized_start = onset
                    note.quantized_end = onset + duration
                    note.quantization_kind = (
                        "grace" if grace_info else kind
                    )  # type: ignore[assignment]
                    note.triplet_id = tuplet_id
                    note.grace = bool(grace_info)
                    note.ornament = ornament_info.kind if ornament_info else None
                    if grace_info:
                        note.grace_order = grace_order
                        note.grace_slash = grace_slash
                chord = ChordEvent(
                    event_id=event_id,
                    staff=group.staff,
                    onset=onset,
                    duration=duration,
                    notes=list(duration_notes),
                    arpeggio_direction=group.arpeggio_direction,
                    tuplet_id=tuplet_id,
                    grace=bool(grace_info),
                    grace_order=grace_order if grace_info else 0,
                    grace_slash=grace_slash if grace_info else True,
                    ornament=ornament_info.kind if ornament_info else None,
                    confidence=group.confidence,
                )
                chords.append(chord)
                if tuplet_id:
                    tuplets_by_id[tuplet_id].append(event_id)

    # Merge only events from the same input MIDI.  A source is the voice
    # boundary even when two files contain the same pitch at the same time.
    merged_by_attack: dict[
        tuple[
            str,
            int,
            Fraction,
            str | None,
            bool,
            int,
            str | None,
            str | int | None,
        ],
        ChordEvent,
    ] = {}
    for chord in chords:
        key_tuple = (
            str(chord.staff),
            chord.notes[0].source_index,
            chord.onset,
            str(chord.tuplet_id) if chord.tuplet_id else None,
            chord.grace,
            chord.grace_order,
            chord.ornament,
            # Separate grace attacks are an ordered gesture, never chord
            # tones merely because two detector chains reached one target
            # onset with the same local order.
            chord.event_id if chord.grace else None,
        )
        existing = merged_by_attack.get(key_tuple)
        if existing is None:
            merged_by_attack[key_tuple] = chord
        else:
            existing.notes.extend(chord.notes)
            # One MIDI is one voice: every non-grace event snapped to the same
            # attack is a single notated chord even if one subset supplied the
            # arpeggio evidence or carried a different raw note-off.  The
            # timeline pass below gives the merged chord one common release at
            # the next distinct attack.
            existing.duration = max(existing.duration, chord.duration)
            existing.arpeggio_direction = existing.arpeggio_direction or chord.arpeggio_direction
    chords = _deduplicate_chords(list(merged_by_attack.values()), report)

    voices = {
        staff: _allocate_voices(
            _enforce_single_voice_timelines(
                [chord for chord in chords if str(chord.staff) == staff],
                tuplet_bounds,
            ),
            staff,
        )
        for staff in ("treble", "bass")
    }
    rendered_chords = [
        event
        for staff_lines in voices.values()
        for line in staff_lines
        for event in line.events
        if isinstance(event, ChordEvent)
    ]
    event_by_id = {chord.event_id: chord for chord in rendered_chords}
    tuplets: list[TupletGroup] = []
    for tuplet_id, original_members in tuplets_by_id.items():
        members = [member for member in original_members if member in event_by_id]
        expected_actual = tuplet_ratios[tuplet_id][0]
        if len(members) != expected_actual:
            raise ValueError(
                f"连音组 {tuplet_id} 应有 {expected_actual} 个记谱事件，实际为 {len(members)}；"
                "已拒绝生成可能损坏时间轴的 MusicXML。"
            )
        events = [event_by_id[member] for member in members]
        member_voices = {event.voice for event in events}
        if len(member_voices) != 1:
            raise ValueError(
                f"连音组 {tuplet_id} 被分配到多个声部 {sorted(member_voices)}；"
                "已拒绝生成 MuseScore 会静默改写的跨声部连音。"
            )
        if len(members) < 2:
            continue
        tuplets.append(
            TupletGroup(
                tuplet_id=tuplet_id,
                staff=events[0].staff,
                voice=events[0].voice or (1 if events[0].staff == "treble" else 5),
                start=tuplet_bounds[tuplet_id][0],
                end=tuplet_bounds[tuplet_id][1],
                member_event_ids=members,
                actual_notes=tuplet_ratios[tuplet_id][0],
                normal_notes=tuplet_ratios[tuplet_id][1],
            )
        )

    swing_regions: list[tuple[Fraction, Fraction, tuple[int, int]]] = []
    if swing_ratio and swing_map:
        beat_numbers = sorted(
            {math.floor(float(onset)) for onset in swing_map.values()}
        )
        beat_runs: list[list[int]] = []
        for beat in beat_numbers:
            if beat_runs and beat == beat_runs[-1][-1] + 1:
                beat_runs[-1].append(beat)
            else:
                beat_runs.append([beat])
        swing_regions = [
            (Fraction(run[0]), Fraction(run[-1] + 1), swing_ratio)
            for run in beat_runs
        ]

    score_duration = max((chord.end for chord in rendered_chords), default=Fraction(0))
    score = NormalizedScore(
        voices=voices,
        tuplets=tuplets,
        attack_groups=attack_groups,
        tempo_bpm=bpm,
        time_signature=signature,
        key_fifths=key,
        metadata=metadata,
        duration=score_duration,
        pickup_beats=metadata.pickup_beats,
        latency_by_source=latency_by_source,
        trim_offset=trim_offset,
        swing_ratio=swing_ratio,
        swing_regions=swing_regions,
    )
    report.final_notes = sum(
        len(chord.notes) for chord in rendered_chords if not chord.continuation
    )
    report.quantized_chords = sum(
        not chord.continuation for chord in rendered_chords
    )
    report.arpeggio_count = sum(
        chord.arpeggio_direction is not None for chord in rendered_chords
    )
    report.tuplet_count = len(tuplets)
    report.triplet_count = sum(
        group.actual_notes == 3 and group.normal_notes == 2 for group in tuplets
    )
    report.grace_count = sum(chord.grace for chord in rendered_chords)
    report.ornament_count = sum(
        chord.ornament is not None for chord in rendered_chords
    )
    report.swing_detected = swing_ratio is not None
    report.tempo_bpm = bpm
    report.time_signature = signature
    report.key_fifths = key
    report.key_mode = metadata.key_mode
    report.key_name = key_name(key, metadata.key_mode)
    report.tempo_change_count = max(0, len(metadata.tempo_changes) - 1)
    report.time_signature_change_count = max(0, len(metadata.meter_changes) - 1)
    report.key_change_count = max(0, len(metadata.key_changes) - 1)
    report.pickup_beats = metadata.pickup_beats
    for metadata_warning in metadata.warnings:
        if metadata_warning not in report.warnings:
            report.warnings.append(metadata_warning)

    def review_measure(onset: Fraction) -> int:
        index = bisect_right(measure_boundaries, onset) - 1
        return max(1, index + 1)

    if unresolved_regions:
        measures = sorted(
            {
                review_measure(start)
                for start, _end, _reason in unresolved_regions
            }
        )
        report.low_confidence_measures = sorted(
            set(report.low_confidence_measures) | set(measures)
        )
        report.warnings.append(
            "发现跨小节或无法唯一表达的连音候选；已保留展开攻击，请复核小节 "
            + "、".join(str(number) for number in measures)
            + "。"
        )
    if review_onsets:
        measures = sorted(
            {review_measure(onset) for onset in review_onsets}
        )
        report.low_confidence_measures = sorted(
            set(report.low_confidence_measures) | set(measures)
        )
        report.warnings.append(
            "以下小节存在无法在所选网格中唯一量化的快速攻击；已保留展开位置，请人工复核："
            + "、".join(str(number) for number in measures)
            + "。"
        )
    special_review_onsets = {
        chord.onset
        for chord in rendered_chords
        if chord.grace
        or chord.arpeggio_direction is not None
        or chord.tuplet_id is not None
        or chord.ornament is not None
        or any(note.quantization_kind == "swing" for note in chord.notes)
    }
    if special_review_onsets:
        measures = sorted(review_measure(onset) for onset in special_review_onsets)
        measures = sorted(set(measures))
        report.low_confidence_measures = sorted(
            set(report.low_confidence_measures) | set(measures)
        )
        report.warnings.append(
            "以下小节含自动解释的倚音、琶音、比例连音、Swing 或装饰音；"
            "已完成输出，请在 MuseScore 中复核："
            + "、".join(str(number) for number in measures)
            + "。"
        )
    for file in files:
        file.report.arpeggio_count = sum(
            group.arpeggio_direction is not None
            and any(note.source_index == file.source_index for note in group.notes)
            for group in attack_groups
        )
        file.report.tuplet_count = sum(
            any(
                event is not None
                and any(note.source_index == file.source_index for note in event.notes)
                for event in (event_by_id.get(member_id) for member_id in tuplet.member_event_ids)
            )
            for tuplet in tuplets
        )
        file.report.triplet_count = sum(
            tuplet.actual_notes == 3
            and tuplet.normal_notes == 2
            and any(
                event is not None
                and any(note.source_index == file.source_index for note in event.notes)
                for event in (event_by_id.get(member_id) for member_id in tuplet.member_event_ids)
            )
            for tuplet in tuplets
        )
        file.report.grace_count = sum(
            chord.grace and any(note.source_index == file.source_index for note in chord.notes)
            for chord in rendered_chords
        )
        file.report.ornament_count = sum(
            chord.ornament is not None
            and any(note.source_index == file.source_index for note in chord.notes)
            for chord in rendered_chords
        )
    if trim_offset:
        report.warnings.append(f"已裁剪共同前导 {float(trim_offset):g} 拍。")
    if swing_ratio:
        report.warnings.append("检测到区域性 2:1 Swing；谱面以直八分音符记谱。")
    if report.ornament_count:
        report.warnings.append(
            f"已将 {report.ornament_count} 段高置信快速交替音写为颤音记号；请在 MuseScore 中复核。"
        )
    if clipped_tuplet_tails:
        report.warnings.append(
            f"有 {clipped_tuplet_tails} 个连音成员的 MIDI 尾音越过组边界，"
            "已在边界处分段并用延音线持续到下一次攻击，以保持连音记谱合法且不插入休止。"
        )
    if report.pedal_tails_trimmed:
        report.warnings.append(
            f"已按单声部规则截短 {report.pedal_tails_trimmed} 个延续到下一攻击后的踏板/混响尾音。"
        )
    return score


__all__ = ["NormalizedScore", "normalize"]
