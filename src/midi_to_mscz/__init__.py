"""Public API for :mod:`midi_to_mscz`."""

from .midi_io import MidiReadError, parse_midi, read_midi_file, read_midi_files
from .metadata import infer_score_metadata, key_name
from .pipeline import convert
from .models import (
    AttackGroup,
    ChordEvent,
    ConversionReport,
    ConversionSettings,
    FileReport,
    InputSpec,
    KeyChange,
    KeySignatureEvent,
    MeterChange,
    MidiFileData,
    NoteEvent,
    RestEvent,
    ScoreMetadata,
    TempoChange,
    TempoEvent,
    TimeSignatureEvent,
    TupletGroup,
    VoiceLine,
)

__version__ = "0.3.0"

__all__ = [
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
    "MidiReadError",
    "NoteEvent",
    "RestEvent",
    "ScoreMetadata",
    "TempoChange",
    "TempoEvent",
    "TimeSignatureEvent",
    "TupletGroup",
    "VoiceLine",
    "parse_midi",
    "convert",
    "infer_score_metadata",
    "key_name",
    "read_midi_file",
    "read_midi_files",
]
