"""Shared data models for the MIDI-to-MuseScore conversion pipeline.

Musical positions are represented with :class:`fractions.Fraction` wherever
possible.  Keeping beat positions exact here avoids accumulating floating point
error while later stages split notes at beat and measure boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal, TypeAlias


Staff = Literal["treble", "bass"]
StaffAssignment = Literal["treble", "bass"]
Sensitivity = Literal["strict", "normal", "aggressive"]
QuantizationKind = Literal[
    "binary", "tuplet", "triplet", "arpeggio", "grace", "swing", "ornament", "free"
]
ArpeggioDirection = Literal["up", "down"]
NoteId: TypeAlias = str | int


def as_fraction(value: Fraction | int | float | str) -> Fraction:
    """Convert a user-facing beat value without preserving float noise."""

    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def _normalise_time_signature(
    value: tuple[int, int] | list[int] | str | None,
) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip().lower() in {"", "auto", "automatic", "自动"}:
            return None
        pieces = value.strip().split("/", 1)
        if len(pieces) != 2:
            raise ValueError("time_signature must look like '4/4'")
        value = (int(pieces[0]), int(pieces[1]))
    if len(value) != 2:
        raise ValueError("time_signature must contain numerator and denominator")
    numerator, denominator = int(value[0]), int(value[1])
    if numerator <= 0 or denominator <= 0 or denominator & (denominator - 1):
        raise ValueError(
            "time_signature needs a positive numerator and power-of-two denominator"
        )
    return numerator, denominator


@dataclass
class InputSpec:
    """One input MIDI and the transformations selected for that source."""

    path: Path | str
    staff: StaffAssignment = "treble"
    transpose_octaves: int = 0
    velocity_min: int = 1
    offset_beats: Fraction | float = Fraction(0)
    transpose_semitones: int = 0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.staff = self.staff.lower().strip()  # type: ignore[assignment]
        if self.staff not in {"treble", "bass"}:
            raise ValueError("staff must be 'treble' or 'bass'")
        self.transpose_octaves = int(self.transpose_octaves)
        self.transpose_semitones = int(self.transpose_semitones)
        self.velocity_min = int(self.velocity_min)
        if not 0 <= self.velocity_min <= 127:
            raise ValueError("velocity_min must be between 0 and 127")
        self.offset_beats = as_fraction(self.offset_beats)

    @property
    def total_transpose_semitones(self) -> int:
        """The complete pitch shift requested for this source."""

        return self.transpose_octaves * 12 + self.transpose_semitones


@dataclass
class ConversionSettings:
    """Options that apply to the combined score."""

    bpm: float | str | None = None
    time_signature: tuple[int, int] | list[int] | str | None = None
    key_fifths: int | str | None = None
    smallest_note: int | str = 16
    auto_latency: bool = True
    auto_trim: bool = True
    detect_arpeggios: bool = True
    detect_triplets: bool = True
    detect_grace_notes: bool = True
    detect_swing: bool = True
    detect_ornaments: bool = True
    pickup: bool = False
    title: str = ""
    composer: str = ""
    musescore_path: Path | str | None = None
    keep_intermediate: bool = False
    triplet_sensitivity: Sensitivity = "normal"
    arpeggio_sensitivity: Sensitivity = "normal"
    pickup_beats: Fraction | float = Fraction(0)
    tempo_smoothing: bool = True
    auto_pickup: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.bpm, str) and self.bpm.strip().lower() in {
            "",
            "auto",
            "automatic",
            "自动",
        }:
            self.bpm = None
        if self.bpm is not None:
            self.bpm = float(self.bpm)
            if self.bpm <= 0:
                raise ValueError("bpm must be positive")
        self.time_signature = _normalise_time_signature(self.time_signature)
        if isinstance(self.key_fifths, str) and self.key_fifths.strip().lower() in {
            "",
            "auto",
            "automatic",
            "自动",
        }:
            self.key_fifths = None
        if self.key_fifths is not None:
            self.key_fifths = int(self.key_fifths)
            if not -7 <= self.key_fifths <= 7:
                raise ValueError("key_fifths must be between -7 and 7")
        if isinstance(self.smallest_note, str):
            text = self.smallest_note.strip()
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                if int(numerator) != 1:
                    raise ValueError("smallest_note must be a unit fraction")
                self.smallest_note = int(denominator)
            else:
                self.smallest_note = int(text)
        self.smallest_note = int(self.smallest_note)
        if self.smallest_note <= 0 or self.smallest_note & (self.smallest_note - 1):
            raise ValueError("smallest_note must be a positive power-of-two denominator")
        if self.triplet_sensitivity not in {"strict", "normal", "aggressive"}:
            raise ValueError("invalid triplet_sensitivity")
        if self.arpeggio_sensitivity not in {"strict", "normal", "aggressive"}:
            raise ValueError("invalid arpeggio_sensitivity")
        self.pickup_beats = as_fraction(self.pickup_beats)
        if self.pickup_beats < 0:
            raise ValueError("pickup_beats cannot be negative")
        self.pickup = bool(self.pickup or self.pickup_beats > 0)
        if self.musescore_path is not None:
            self.musescore_path = Path(self.musescore_path)

    @property
    def smallest_note_beats(self) -> Fraction:
        """Length of the smallest straight note in quarter-note beats."""

        return Fraction(4, self.smallest_note)

    @property
    def detect_tuplets(self) -> bool:
        """Backward-compatible public name for common arbitrary tuplets."""

        return self.detect_triplets


@dataclass(frozen=True)
class TempoEvent:
    tick: int
    beat: Fraction
    tempo: int
    track_index: int = 0
    sequence: int = 0

    @property
    def bpm(self) -> float:
        return 60_000_000.0 / self.tempo


@dataclass(frozen=True)
class TimeSignatureEvent:
    tick: int
    beat: Fraction
    numerator: int
    denominator: int
    clocks_per_click: int = 24
    notated_32nd_notes_per_beat: int = 8
    track_index: int = 0
    sequence: int = 0

    @property
    def signature(self) -> tuple[int, int]:
        return self.numerator, self.denominator


@dataclass(frozen=True)
class KeySignatureEvent:
    tick: int
    beat: Fraction
    key: str
    fifths: int | None
    mode: Literal["major", "minor"]
    track_index: int = 0
    sequence: int = 0


@dataclass(frozen=True)
class TempoChange:
    """A cleaned score tempo instruction on the quarter-note beat timeline."""

    beat: Fraction | int | float
    bpm: float
    confidence: float = 1.0
    inferred: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "beat", as_fraction(self.beat))
        object.__setattr__(self, "bpm", float(self.bpm))
        if self.beat < 0:
            raise ValueError("tempo change beat cannot be negative")
        if self.bpm <= 0:
            raise ValueError("tempo change bpm must be positive")


@dataclass(frozen=True)
class MeterChange:
    """A score time-signature change on the quarter-note beat timeline."""

    beat: Fraction | int | float
    numerator: int
    denominator: int
    confidence: float = 1.0
    inferred: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "beat", as_fraction(self.beat))
        object.__setattr__(self, "numerator", int(self.numerator))
        object.__setattr__(self, "denominator", int(self.denominator))
        if self.beat < 0:
            raise ValueError("meter change beat cannot be negative")
        _normalise_time_signature((self.numerator, self.denominator))

    @property
    def signature(self) -> tuple[int, int]:
        return self.numerator, self.denominator

    @property
    def measure_beats(self) -> Fraction:
        return Fraction(self.numerator * 4, self.denominator)


@dataclass(frozen=True)
class KeyChange:
    """A cleaned key signature, with mode retained for reports and UI labels."""

    beat: Fraction | int | float
    fifths: int
    mode: Literal["major", "minor"] = "major"
    confidence: float = 1.0
    inferred: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "beat", as_fraction(self.beat))
        object.__setattr__(self, "fifths", int(self.fifths))
        if self.beat < 0:
            raise ValueError("key change beat cannot be negative")
        if not -7 <= self.fifths <= 7:
            raise ValueError("key change fifths must be between -7 and 7")
        if self.mode not in {"major", "minor"}:
            raise ValueError("key change mode must be 'major' or 'minor'")


@dataclass
class ScoreMetadata:
    """Canonical metadata selected from one or more imperfect MIDI stems.

    All positions use the score's quarter-note beat timeline.  Per-source note
    alignment offsets never move global tempo/meter/key instructions.  The
    first entry in every non-empty map is guaranteed to begin at beat zero.
    """

    tempo_changes: list[TempoChange] = field(default_factory=list)
    meter_changes: list[MeterChange] = field(default_factory=list)
    key_changes: list[KeyChange] = field(default_factory=list)
    pickup_beats: Fraction | int | float = Fraction(0)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.pickup_beats = as_fraction(self.pickup_beats)

    @property
    def bpm(self) -> float:
        return self.tempo_changes[0].bpm if self.tempo_changes else 120.0

    @property
    def time_signature(self) -> tuple[int, int]:
        return self.meter_changes[0].signature if self.meter_changes else (4, 4)

    @property
    def key_fifths(self) -> int:
        return self.key_changes[0].fifths if self.key_changes else 0

    @property
    def key_mode(self) -> Literal["major", "minor"]:
        return self.key_changes[0].mode if self.key_changes else "major"


@dataclass
class NoteEvent:
    """A paired MIDI note, retaining both raw and transformed information."""

    source_index: int
    source_path: Path | str
    raw_start: int
    raw_end: int
    start_beat: Fraction | int | float
    end_beat: Fraction | int | float
    pitch: int
    velocity: int
    staff: StaffAssignment
    original_pitch: int | None = None
    channel: int = 0
    track_index: int = 0
    note_id: NoteId = ""
    quantized_start: Fraction | None = None
    quantized_end: Fraction | None = None
    quantization_kind: QuantizationKind | None = None
    arpeggio_id: NoteId | None = None
    triplet_id: NoteId | None = None
    grace: bool = False
    grace_slash: bool = True
    grace_order: int = 0
    ornament: str | None = None
    confidence: float = 1.0
    tie_start: bool = False
    tie_stop: bool = False

    def __post_init__(self) -> None:
        self.source_path = Path(self.source_path)
        self.start_beat = as_fraction(self.start_beat)
        self.end_beat = as_fraction(self.end_beat)
        if self.quantized_start is not None:
            self.quantized_start = as_fraction(self.quantized_start)
        if self.quantized_end is not None:
            self.quantized_end = as_fraction(self.quantized_end)
        if self.original_pitch is None:
            self.original_pitch = self.pitch
        self.pitch = int(self.pitch)
        self.original_pitch = int(self.original_pitch)
        self.velocity = int(self.velocity)

    @property
    def raw_duration(self) -> int:
        return self.raw_end - self.raw_start

    @property
    def duration_beats(self) -> Fraction:
        return self.end_beat - self.start_beat

    @property
    def duration(self) -> Fraction:
        return self.duration_beats

    @property
    def quantized_duration(self) -> Fraction | None:
        if self.quantized_start is None or self.quantized_end is None:
            return None
        return self.quantized_end - self.quantized_start

    @property
    def effective_start(self) -> Fraction:
        return self.quantized_start if self.quantized_start is not None else self.start_beat

    @property
    def effective_end(self) -> Fraction:
        return self.quantized_end if self.quantized_end is not None else self.end_beat

    @property
    def transposed_pitch(self) -> int:
        """Alias documenting that ``pitch`` is the transformed pitch."""

        return self.pitch

    @property
    def effective_pitch(self) -> int:
        return self.pitch


@dataclass
class AttackGroup:
    group_id: NoteId
    staff: StaffAssignment
    notes: list[NoteEvent]
    raw_anchor: Fraction
    quantized_onset: Fraction | None = None
    kind: QuantizationKind | None = None
    arpeggio_direction: ArpeggioDirection | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        self.raw_anchor = as_fraction(self.raw_anchor)
        if self.quantized_onset is not None:
            self.quantized_onset = as_fraction(self.quantized_onset)

    @property
    def raw_span_beats(self) -> Fraction:
        if not self.notes:
            return Fraction(0)
        starts = [note.start_beat for note in self.notes]
        return max(starts) - min(starts)


@dataclass
class ChordEvent:
    event_id: NoteId
    staff: StaffAssignment
    onset: Fraction | int | float
    duration: Fraction | int | float
    notes: list[NoteEvent]
    voice: int | None = None
    arpeggio_direction: ArpeggioDirection | None = None
    tuplet_id: NoteId | None = None
    grace: bool = False
    grace_slash: bool = True
    grace_order: int = 0
    ornament: str | None = None
    continuation: bool = False
    confidence: float = 1.0
    tie_starts: set[NoteId] = field(default_factory=set)
    tie_stops: set[NoteId] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.onset = as_fraction(self.onset)
        self.duration = as_fraction(self.duration)

    @property
    def end(self) -> Fraction:
        return self.onset + self.duration


@dataclass
class RestEvent:
    event_id: NoteId
    staff: StaffAssignment
    voice: int
    onset: Fraction | int | float
    duration: Fraction | int | float
    tuplet_id: NoteId | None = None
    visible: bool = True

    def __post_init__(self) -> None:
        self.onset = as_fraction(self.onset)
        self.duration = as_fraction(self.duration)

    @property
    def end(self) -> Fraction:
        return self.onset + self.duration


VoiceEvent: TypeAlias = ChordEvent | RestEvent


@dataclass
class VoiceLine:
    staff: StaffAssignment
    voice_number: int
    events: list[VoiceEvent] = field(default_factory=list)
    source_affinity: set[int] = field(default_factory=set)

    @property
    def voice(self) -> int:
        return self.voice_number

    def sort_events(self) -> None:
        self.events.sort(key=lambda event: (event.onset, event.duration))


@dataclass
class TupletGroup:
    tuplet_id: NoteId
    staff: StaffAssignment
    voice: int
    start: Fraction | int | float
    end: Fraction | int | float
    member_event_ids: list[NoteId] = field(default_factory=list)
    actual_notes: int = 3
    normal_notes: int = 2
    bracket: bool = True
    show_number: bool = True

    def __post_init__(self) -> None:
        self.start = as_fraction(self.start)
        self.end = as_fraction(self.end)


@dataclass
class FileReport:
    """Read/pre-processing statistics for one input file."""

    source_index: int
    path: Path | str
    staff: StaffAssignment
    track_count: int = 0
    notes_seen: int = 0
    paired_notes: int = 0
    notes_kept: int = 0
    velocity_filtered: int = 0
    invalid_duration: int = 0
    unmatched_note_offs: int = 0
    unclosed_note_ons: int = 0
    transposed_notes: int = 0
    out_of_midi_range: int = 0
    out_of_piano_range: int = 0
    merged_duplicates: int = 0
    arpeggio_count: int = 0
    triplet_count: int = 0
    tuplet_count: int = 0
    grace_count: int = 0
    ornament_count: int = 0
    pedal_tails_trimmed: int = 0
    latency_bias_beats: Fraction = Fraction(0)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.latency_bias_beats = as_fraction(self.latency_bias_beats)

    @property
    def input_notes(self) -> int:
        return self.notes_seen

    @property
    def output_notes(self) -> int:
        return self.notes_kept

    @property
    def filtered_notes(self) -> int:
        return self.velocity_filtered


@dataclass
class MidiFileData:
    """Normalised contents of one MIDI file before rhythm quantisation."""

    source_index: int
    path: Path | str
    spec: InputSpec
    midi_type: int
    ticks_per_beat: int
    notes: list[NoteEvent]
    report: FileReport
    tempo_events: list[TempoEvent] = field(default_factory=list)
    time_signature_events: list[TimeSignatureEvent] = field(default_factory=list)
    key_signature_events: list[KeySignatureEvent] = field(default_factory=list)
    track_names: list[str] = field(default_factory=list)
    duration_ticks: int = 0

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    @property
    def duration_beats(self) -> Fraction:
        return Fraction(self.duration_ticks, self.ticks_per_beat)

    @property
    def initial_bpm(self) -> float:
        events = [event for event in self.tempo_events if event.tick == 0]
        return events[-1].bpm if events else 120.0

    @property
    def bpm(self) -> float:
        return self.initial_bpm

    @property
    def initial_time_signature(self) -> tuple[int, int]:
        events = [event for event in self.time_signature_events if event.tick == 0]
        return events[-1].signature if events else (4, 4)

    @property
    def time_signature(self) -> tuple[int, int]:
        return self.initial_time_signature

    @property
    def initial_key_fifths(self) -> int:
        events = [event for event in self.key_signature_events if event.tick == 0]
        return events[-1].fifths if events and events[-1].fifths is not None else 0

    @property
    def key_fifths(self) -> int:
        return self.initial_key_fifths

    @property
    def tempo_map(self) -> list[TempoEvent]:
        return self.tempo_events

    @property
    def time_signatures(self) -> list[TimeSignatureEvent]:
        return self.time_signature_events

    @property
    def key_signatures(self) -> list[KeySignatureEvent]:
        return self.key_signature_events


@dataclass
class ConversionReport:
    """Aggregate report, extended by later conversion stages as they run."""

    files: list[FileReport] = field(default_factory=list)
    output_path: Path | str | None = None
    musicxml_path: Path | str | None = None
    final_notes: int = 0
    quantized_chords: int = 0
    merged_duplicates: int = 0
    arpeggio_count: int = 0
    triplet_count: int = 0
    tuplet_count: int = 0
    grace_count: int = 0
    ornament_count: int = 0
    swing_detected: bool = False
    low_confidence_measures: list[int] = field(default_factory=list)
    tempo_bpm: float | None = None
    time_signature: tuple[int, int] | None = None
    key_fifths: int | None = None
    key_mode: Literal["major", "minor"] | None = None
    key_name: str | None = None
    tempo_change_count: int = 0
    time_signature_change_count: int = 0
    key_change_count: int = 0
    pickup_beats: Fraction = Fraction(0)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.output_path is not None:
            self.output_path = Path(self.output_path)
        if self.musicxml_path is not None:
            self.musicxml_path = Path(self.musicxml_path)
        self.pickup_beats = as_fraction(self.pickup_beats)

    @property
    def file_reports(self) -> list[FileReport]:
        return self.files

    @property
    def notes_seen(self) -> int:
        return sum(report.notes_seen for report in self.files)

    @property
    def paired_notes(self) -> int:
        return sum(report.paired_notes for report in self.files)

    @property
    def notes_kept(self) -> int:
        return sum(report.notes_kept for report in self.files)

    @property
    def velocity_filtered(self) -> int:
        return sum(report.velocity_filtered for report in self.files)

    @property
    def invalid_duration(self) -> int:
        return sum(report.invalid_duration for report in self.files)

    @property
    def transposed_notes(self) -> int:
        return sum(report.transposed_notes for report in self.files)

    @property
    def out_of_midi_range(self) -> int:
        return sum(report.out_of_midi_range for report in self.files)

    @property
    def out_of_piano_range(self) -> int:
        return sum(report.out_of_piano_range for report in self.files)

    @property
    def pedal_tails_trimmed(self) -> int:
        """Notes shortened because the next attack in that MIDI began."""

        return sum(report.pedal_tails_trimmed for report in self.files)

    @property
    def input_notes(self) -> int:
        return self.notes_seen

    @property
    def output_notes(self) -> int:
        return self.notes_kept

    @property
    def filtered_notes(self) -> int:
        return self.velocity_filtered


__all__ = [
    "ArpeggioDirection",
    "AttackGroup",
    "ChordEvent",
    "ConversionReport",
    "ConversionSettings",
    "FileReport",
    "InputSpec",
    "KeyChange",
    "KeySignatureEvent",
    "MeterChange",
    "MidiFileData",
    "NoteEvent",
    "NoteId",
    "QuantizationKind",
    "RestEvent",
    "Sensitivity",
    "ScoreMetadata",
    "Staff",
    "StaffAssignment",
    "TempoEvent",
    "TempoChange",
    "TimeSignatureEvent",
    "TupletGroup",
    "VoiceEvent",
    "VoiceLine",
    "as_fraction",
]
