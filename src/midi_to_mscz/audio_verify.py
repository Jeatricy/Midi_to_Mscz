"""Conservative local audio verification for imperfect MIDI transcriptions.

The verifier deliberately treats neural transcription as one witness rather
than ground truth.  A note can only be removed when harmonic, onset, model and
musical-context evidence agree that it is probably spurious.  Uncertain and
special-rhythm candidates are retained and surfaced as measure-level review
items.

Heavy dependencies are imported lazily so the normal MIDI-to-MuseScore path
continues to work without audio verification.  Basic Pitch is mandatory unless
the caller explicitly selects ``dsp_only=True``.
"""

from __future__ import annotations

import bisect
import importlib.util
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from importlib import metadata as package_metadata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .models import (
    AudioAlignment,
    AudioNoteEvidence,
    AudioReviewItem,
    AudioVerificationResult,
    AudioVerificationSettings,
    ConversionReport,
    MidiFileData,
    NoteEvent,
)


Progress = Callable[[str], None]


class AudioVerificationError(ValueError):
    """Raised when audio verification cannot produce trustworthy evidence."""


class AudioVerificationDependencyError(AudioVerificationError):
    """Raised when an explicitly requested local model/decoder is unavailable."""


@dataclass(frozen=True)
class _ModelEvent:
    start: float
    end: float
    pitch: int
    amplitude: float


@dataclass
class _ModelOutput:
    events: list[_ModelEvent]
    activation: Any | None
    pitch_offset: int
    duration_seconds: float
    name: str


@dataclass
class _AudioAnalysis:
    samples: Any
    sample_rate: int
    onset_envelope: Any
    onset_hop: int

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / float(self.sample_rate)


@dataclass(frozen=True)
class _ContextInfo:
    support: float
    special_guard: float
    labels: tuple[str, ...]
    structural_risk: float = 0.0
    structural_reasons: tuple[str, ...] = ()


_MODEL_CACHE: dict[tuple[str, int, int], _ModelOutput] = {}
_MODEL_LOCK = threading.Lock()
_MAX_AUDIO_SECONDS = 45 * 60


def _announce(progress: Progress | None, message: str) -> None:
    if progress:
        progress(message)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    direct = math.exp(value)
    return direct / (1.0 + direct)


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise AudioVerificationDependencyError(
            "音频复核需要 NumPy。请安装音频复核依赖，或关闭原始音频复核。"
        ) from exc
    return np


def _resolved_executable(name: str) -> str | None:
    executable = shutil.which(name)
    if not executable and os.name == "nt":
        # A project-local virtual environment does not always inherit the
        # per-user WinGet Links directory even though PowerShell can resolve
        # the command.  Resolve that narrow, standard location explicitly so
        # Compressed-audio decoding keeps working when FFmpeg was installed by WinGet.
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            winget_link = (
                Path(local_app_data)
                / "Microsoft"
                / "WinGet"
                / "Links"
                / f"{name}.exe"
            )
            try:
                if winget_link.is_file():
                    executable = str(winget_link)
            except OSError:
                # Corporate security policies may deny following the WinGet
                # symlink.  The soundfile/WAV decoders remain available and
                # capability reporting must never make the WebUI fail.
                pass
    if not executable:
        return None
    try:
        # Winget often exposes a zero-byte symlink that CreateProcess cannot
        # launch directly in restricted Windows environments.
        return str(Path(executable).resolve(strict=True))
    except OSError:
        return executable


def _decode_wave(path: Path) -> tuple[Any, int]:
    np = _require_numpy()
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        if frame_count / max(1, sample_rate) > _MAX_AUDIO_SECONDS:
            raise AudioVerificationError("原始音频超过 45 分钟，已拒绝解码以保护内存。")
        frames = handle.readframes(frame_count)
    if sample_width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        values = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values - 0x1000000, values)
        data = values.astype(np.float32) / 8_388_608.0
    elif sample_width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2_147_483_648.0
    else:
        raise AudioVerificationError(f"不支持 {sample_width * 8} 位 WAV：{path.name}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return data, int(sample_rate)


def _decode_audio(path: Path, target_rate: int) -> tuple[Any, int]:
    """Decode locally with soundfile, WAV stdlib, or an existing FFmpeg."""

    np = _require_numpy()
    try:
        import soundfile as sf  # type: ignore[import-not-found]

        # High-resolution masters can occupy several hundred MB when decoded.
        # Resample in bounded blocks instead of materialising 192-kHz stereo,
        # two time axes, and the 22-kHz result at once.
        with sf.SoundFile(str(path), "r") as handle:
            sample_rate = int(handle.samplerate)
            frame_count = int(handle.frames)
            if frame_count / max(1, sample_rate) > _MAX_AUDIO_SECONDS:
                raise AudioVerificationError(
                    "原始音频超过 45 分钟，已拒绝解码以保护内存。"
                )
            ratio = target_rate / float(sample_rate)
            output_count = max(1, int(round(frame_count * ratio)))
            chunks: list[Any] = []
            target_cursor = 0
            source_cursor = 0
            previous: Any | None = None
            while source_cursor < frame_count:
                block = handle.read(
                    min(262_144, frame_count - source_cursor),
                    dtype="float32",
                    always_2d=True,
                )
                if not len(block):
                    break
                mono = block.mean(axis=1, dtype=np.float32)
                if sample_rate == target_rate:
                    chunks.append(mono)
                    target_cursor += len(mono)
                else:
                    extended = (
                        np.concatenate((previous, mono))
                        if previous is not None
                        else mono
                    )
                    extended_start = source_cursor - (1 if previous is not None else 0)
                    next_source_cursor = source_cursor + len(mono)
                    last_target = min(
                        output_count,
                        int(math.ceil(next_source_cursor * ratio)),
                    )
                    target_indices = np.arange(target_cursor, last_target)
                    source_positions = target_indices / ratio - extended_start
                    chunks.append(
                        np.interp(
                            source_positions,
                            np.arange(len(extended)),
                            extended,
                        ).astype(np.float32)
                    )
                    target_cursor = last_target
                    previous = mono[-1:].copy()
                source_cursor += len(mono)
            data = (
                np.concatenate(chunks).astype(np.float32, copy=False)
                if chunks
                else np.asarray([], dtype=np.float32)
            )
            if sample_rate != target_rate:
                if len(data) < output_count:
                    data = np.pad(data, (0, output_count - len(data)), mode="edge")
                elif len(data) > output_count:
                    data = data[:output_count]
                sample_rate = target_rate
    except (ImportError, OSError, RuntimeError, ValueError) as soundfile_error:
        if isinstance(soundfile_error, AudioVerificationError):
            raise
        if path.suffix.lower() == ".wav":
            try:
                data, sample_rate = _decode_wave(path)
            except (wave.Error, EOFError, OSError) as exc:
                raise AudioVerificationError(f"无法读取 WAV {path.name}：{exc}") from exc
        else:
            ffmpeg = _resolved_executable("ffmpeg")
            if ffmpeg is None:
                raise AudioVerificationDependencyError(
                    f"无法解码 {path.suffix or '该音频格式'}。请安装 soundfile，"
                    "或让 FFmpeg 可从 PATH 访问。"
                )
            command = [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(path),
                "-map_metadata",
                "-1",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(target_rate),
                "-t",
                str(_MAX_AUDIO_SECONDS + 0.01),
                "-f",
                "f32le",
                "pipe:1",
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise AudioVerificationDependencyError(
                    f"FFmpeg 无法启动，不能解码 {path.name}：{exc}"
                ) from exc
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise AudioVerificationError(
                    f"FFmpeg 无法解码 {path.name}：{detail[-500:]}"
                )
            data = np.frombuffer(completed.stdout, dtype="<f4").copy()
            sample_rate = target_rate
            if len(data) >= int(target_rate * _MAX_AUDIO_SECONDS):
                raise AudioVerificationError(
                    "原始音频达到或超过 45 分钟，已停止分析以保护内存。"
                )

    data = np.asarray(data, dtype=np.float32)
    if data.ndim != 1 or not len(data):
        raise AudioVerificationError(f"音频没有可分析的采样：{path.name}")
    if len(data) / max(1, sample_rate) > _MAX_AUDIO_SECONDS:
        raise AudioVerificationError("原始音频超过 45 分钟，已拒绝分析以保护内存。")
    data = np.nan_to_num(data, copy=False)
    peak = float(np.max(np.abs(data)))
    if peak <= 1e-8:
        raise AudioVerificationError(f"音频几乎完全静音：{path.name}")
    if sample_rate != target_rate:
        duration = len(data) / float(sample_rate)
        output_count = max(1, int(round(duration * target_rate)))
        old_axis = np.linspace(0.0, duration, len(data), endpoint=False)
        new_axis = np.linspace(0.0, duration, output_count, endpoint=False)
        data = np.interp(new_axis, old_axis, data).astype(np.float32)
        sample_rate = target_rate
    return data, int(sample_rate)


def _onset_envelope(samples: Any, sample_rate: int) -> tuple[Any, int]:
    """Return a robust spectral-flux/energy-rise onset curve in small batches."""

    np = _require_numpy()
    frame_size = 1024
    hop = 256
    if len(samples) < frame_size:
        samples = np.pad(samples, (0, frame_size - len(samples)))
    frame_count = 1 + (len(samples) - frame_size) // hop
    flux = np.zeros(frame_count, dtype=np.float32)
    rms = np.zeros(frame_count, dtype=np.float32)
    window = np.hanning(frame_size).astype(np.float32)
    previous = None
    batch_size = 512
    for first in range(0, frame_count, batch_size):
        count = min(batch_size, frame_count - first)
        offsets = (first + np.arange(count)) * hop
        frames = np.stack(
            [samples[int(offset) : int(offset) + frame_size] for offset in offsets]
        )
        rms[first : first + count] = np.sqrt(
            np.mean(frames * frames, axis=1) + 1e-12
        )
        spectra = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
        spectra /= np.maximum(spectra.sum(axis=1, keepdims=True), 1e-9)
        for local, spectrum in enumerate(spectra):
            if previous is not None:
                flux[first + local] = float(np.maximum(spectrum - previous, 0).sum())
            previous = spectrum
    rms_rise = np.maximum(np.diff(np.log1p(rms * 1000), prepend=0.0), 0.0)

    def normalise(values: Any) -> Any:
        floor = float(np.percentile(values, 20))
        ceiling = float(np.percentile(values, 98))
        return np.clip((values - floor) / max(ceiling - floor, 1e-8), 0.0, 1.0)

    envelope = 0.72 * normalise(np.log1p(flux * 5000)) + 0.28 * normalise(rms_rise)
    envelope = np.convolve(
        envelope, np.asarray([0.10, 0.22, 0.36, 0.22, 0.10]), mode="same"
    )
    return np.clip(envelope, 0.0, 1.0).astype(np.float32), hop


def _analyse_audio(path: Path, settings: AudioVerificationSettings) -> _AudioAnalysis:
    samples, sample_rate = _decode_audio(path, settings.sample_rate)
    onset, hop = _onset_envelope(samples, sample_rate)
    return _AudioAnalysis(samples, sample_rate, onset, hop)


class _TempoMapper:
    def __init__(self, file: MidiFileData) -> None:
        by_beat: dict[Fraction, int] = {}
        for event in file.tempo_events:
            by_beat[Fraction(event.beat)] = int(event.tempo)
        events = sorted(by_beat.items())
        active_tempo = 500_000
        for beat, tempo in events:
            if beat <= 0:
                active_tempo = tempo
        self._segments: list[tuple[Fraction, float, int]] = [
            (Fraction(0), 0.0, active_tempo)
        ]
        beat_cursor = Fraction(0)
        seconds = 0.0
        for beat, tempo in events:
            if beat <= 0:
                continue
            seconds += float(beat - beat_cursor) * active_tempo / 1_000_000.0
            self._segments.append((beat, seconds, tempo))
            beat_cursor = beat
            active_tempo = tempo
        self._beats = [segment[0] for segment in self._segments]

    def seconds(self, beat: Fraction | int | float) -> float:
        target = Fraction(beat)
        if target < 0:
            tempo = self._segments[0][2]
            return float(target) * tempo / 1_000_000.0
        index = bisect.bisect_right(self._beats, target) - 1
        segment_beat, segment_seconds, tempo = self._segments[max(0, index)]
        return segment_seconds + float(target - segment_beat) * tempo / 1_000_000.0


def _attack_times(
    files: Sequence[MidiFileData], mappers: dict[int, _TempoMapper]
) -> list[tuple[float, float]]:
    attacks: dict[int, float] = {}
    for file in files:
        mapper = mappers[file.source_index]
        for note in file.notes:
            time = mapper.seconds(note.start_beat)
            bucket = int(round(time * 100.0))
            attacks[bucket] = max(
                attacks.get(bucket, 0.0), math.sqrt(max(1, note.velocity) / 127.0)
            )
    return sorted((bucket / 100.0, weight) for bucket, weight in attacks.items())


def _local_peaks(values: Any, threshold: float = 0.16) -> Any:
    np = _require_numpy()
    if len(values) < 3:
        return np.asarray([], dtype=np.int64)
    return np.where(
        (values[1:-1] >= values[:-2])
        & (values[1:-1] > values[2:])
        & (values[1:-1] >= threshold)
    )[0] + 1


def _nearest_peak(
    peak_times: Any, target: float, tolerance: float
) -> tuple[float, int] | None:
    np = _require_numpy()
    if not len(peak_times):
        return None
    index = int(np.searchsorted(peak_times, target))
    candidates = [candidate for candidate in (index - 1, index) if 0 <= candidate < len(peak_times)]
    if not candidates:
        return None
    best = min(candidates, key=lambda candidate: abs(float(peak_times[candidate]) - target))
    delta = float(peak_times[best]) - target
    return (delta, best) if abs(delta) <= tolerance else None


def _align_midi_to_audio(
    attacks: Sequence[tuple[float, float]],
    analysis: _AudioAnalysis,
    settings: AudioVerificationSettings,
) -> AudioAlignment:
    np = _require_numpy()
    if not attacks:
        return AudioAlignment()
    envelope = analysis.onset_envelope.astype(np.float64)
    frame_rate = analysis.sample_rate / analysis.onset_hop
    midi = np.zeros(len(envelope), dtype=np.float64)
    eligible: list[float] = []
    for time, weight in attacks:
        index = int(round(time * frame_rate))
        if 0 <= index < len(midi):
            midi[index] += weight
            eligible.append(time)
    if not eligible or float(np.linalg.norm(midi)) <= 1e-9:
        return AudioAlignment(total_attacks=len(attacks))
    kernel = np.exp(-0.5 * (np.arange(-5, 6) / 2.0) ** 2)
    kernel /= kernel.sum()
    midi = np.convolve(midi, kernel, mode="same")
    audio = envelope - float(np.percentile(envelope, 25))
    audio = np.maximum(audio, 0.0)
    nfft = 1 << (len(audio) + len(midi) - 2).bit_length()
    correlation = np.fft.irfft(
        np.fft.rfft(audio, nfft) * np.fft.rfft(midi[::-1], nfft), nfft
    )[: len(audio) + len(midi) - 1]
    max_lag = int(round(settings.alignment_search_seconds * frame_rate))
    centre = len(midi) - 1
    low = max(0, centre - max_lag)
    high = min(len(correlation), centre + max_lag + 1)
    searched = correlation[low:high]
    best_local = int(np.argmax(searched))
    best_index = low + best_local
    lag_frames = best_index - centre
    offset = lag_frames / frame_rate

    median = float(np.median(searched))
    deviation = float(np.median(np.abs(searched - median))) * 1.4826 + 1e-9
    z_score = (float(searched[best_local]) - median) / deviation
    norm_score = float(searched[best_local]) / (
        float(np.linalg.norm(audio)) * float(np.linalg.norm(midi)) + 1e-9
    )
    corr_confidence = _clip(0.65 * ((z_score - 2.0) / 10.0) + 0.35 * norm_score * 5.0)
    # Repeated verses/choruses can create several equally plausible global
    # peaks.  Compare the best peak with the best non-neighbouring candidate;
    # an ambiguous alignment must never authorise destructive cleanup.
    peak_height = float(searched[best_local]) - median
    competing = searched.copy()
    exclusion = max(4, int(round(0.75 * frame_rate)))
    competing[
        max(0, best_local - exclusion) : min(len(competing), best_local + exclusion + 1)
    ] = median
    second_height = max(0.0, float(np.max(competing)) - median)
    ambiguity_ratio = second_height / max(peak_height, 1e-9)
    ambiguous = bool(ambiguity_ratio >= 0.94)

    peak_indices = _local_peaks(envelope)
    peak_times = peak_indices.astype(np.float64) / frame_rate
    pairs: list[tuple[float, float, float]] = []
    fit_tolerance = max(0.20, settings.onset_tolerance_seconds * 1.8)
    for midi_time in eligible:
        nearest = _nearest_peak(peak_times, midi_time + offset, fit_tolerance)
        if nearest is None:
            continue
        _delta, peak_index = nearest
        envelope_index = int(peak_indices[peak_index])
        pairs.append((midi_time, float(peak_times[peak_index]), float(envelope[envelope_index])))

    scale = 1.0
    if len(pairs) >= 16 and pairs[-1][0] - pairs[0][0] >= 30.0:
        x = np.asarray([pair[0] for pair in pairs], dtype=np.float64)
        y = np.asarray([pair[1] for pair in pairs], dtype=np.float64)
        weights = np.asarray([pair[2] for pair in pairs], dtype=np.float64)
        keep = np.ones(len(x), dtype=bool)
        for _ in range(3):
            if int(keep.sum()) < 12:
                break
            design = np.column_stack((x[keep], np.ones(int(keep.sum()))))
            root_weights = np.sqrt(np.maximum(weights[keep], 0.05))
            solution, *_ = np.linalg.lstsq(
                design * root_weights[:, None], y[keep] * root_weights, rcond=None
            )
            candidate_scale, candidate_offset = map(float, solution)
            residual = np.abs(y - (candidate_scale * x + candidate_offset))
            keep = residual <= max(0.10, settings.onset_tolerance_seconds)
        if (
            int(keep.sum()) >= 12
            and 0.995 <= candidate_scale <= 1.005
            and abs(candidate_offset - offset) <= 0.35
        ):
            scale, offset = candidate_scale, candidate_offset

    match_tolerance = settings.onset_tolerance_seconds
    matched = sum(
        _nearest_peak(peak_times, scale * time + offset, match_tolerance) is not None
        for time in eligible
    )
    match_ratio = matched / max(1, len(eligible))
    # Validate the mapping independently across the full song.  Local offsets
    # should follow the fitted affine map; one strong repeated section is not
    # enough.  Seven windows cover intros/outros and temporary tempo regions
    # while still leaving enough attacks in each window.
    window_offsets: list[float] = []
    window_midpoints: list[float] = []
    if len(eligible) >= 28:
        first_time, last_time = min(eligible), max(eligible)
        if last_time - first_time >= 20.0:
            window_count = min(9, max(6, len(eligible) // 100 + 6))
            boundaries = np.linspace(first_time, last_time + 1e-6, window_count + 1)
            for window_index in range(window_count):
                members = [
                    (time, weight)
                    for time, weight in attacks
                    if boundaries[window_index] <= time < boundaries[window_index + 1]
                ]
                if len(members) < 5:
                    continue
                midpoint = 0.5 * (boundaries[window_index] + boundaries[window_index + 1])
                expected = offset + (scale - 1.0) * midpoint
                candidates = np.arange(
                    expected - 0.24,
                    expected + 0.24 + 0.5 / frame_rate,
                    1.0 / frame_rate,
                )
                scores = np.zeros(len(candidates), dtype=np.float64)
                total_weight = sum(weight for _time, weight in members) + 1e-9
                for candidate_index, candidate in enumerate(candidates):
                    value = 0.0
                    for time, weight in members:
                        frame = int(round((time + candidate) * frame_rate))
                        lo_frame = max(0, frame - 1)
                        hi_frame = min(len(envelope), frame + 2)
                        if hi_frame > lo_frame:
                            value += weight * float(np.max(envelope[lo_frame:hi_frame]))
                    scores[candidate_index] = value / total_weight
                window_offsets.append(float(candidates[int(np.argmax(scores))]))
                window_midpoints.append(float(midpoint))
    residuals: list[float] = []
    if window_offsets:
        for midpoint, local_offset in zip(window_midpoints, window_offsets):
            residuals.append(local_offset - (offset + (scale - 1.0) * midpoint))
    max_window_residual = max((abs(value) for value in residuals), default=0.0)
    window_consistency = (
        _clip(1.0 - max_window_residual / 0.12)
        if len(window_offsets) >= 4
        else 0.55
    )
    confidence = _clip(
        0.32 * corr_confidence
        + 0.46 * min(1.0, match_ratio / 0.60)
        + 0.22 * window_consistency
    )
    if ambiguous:
        confidence *= 0.48
    if max_window_residual > 0.12:
        confidence *= 0.55
    if abs(lag_frames) >= max_lag - 2:
        confidence *= 0.45
    return AudioAlignment(
        offset_seconds=float(offset),
        scale=float(scale),
        confidence=float(confidence),
        matched_attacks=int(matched),
        total_attacks=len(eligible),
        window_offsets_seconds=tuple(window_offsets),
        max_window_residual_seconds=float(max_window_residual),
        ambiguous=ambiguous,
    )


def _coerce_model_event(value: Any) -> _ModelEvent | None:
    if isinstance(value, dict):
        start = value.get("start_time", value.get("start", value.get("onset")))
        end = value.get("end_time", value.get("end", value.get("offset")))
        pitch = value.get("pitch_midi", value.get("pitch", value.get("note")))
        amplitude = value.get("amplitude", value.get("velocity", value.get("confidence", 1.0)))
    elif all(hasattr(value, name) for name in ("start", "end", "pitch")):
        start, end, pitch = value.start, value.end, value.pitch
        amplitude = getattr(value, "amplitude", getattr(value, "velocity", 1.0))
    else:
        try:
            start, end, pitch = value[:3]
            amplitude = value[3] if len(value) >= 4 else 1.0
        except (TypeError, ValueError, IndexError):
            return None
    try:
        amplitude = float(amplitude)
        if amplitude > 1.0:
            amplitude /= 127.0
        event = _ModelEvent(float(start), float(end), int(round(float(pitch))), _clip(amplitude))
    except (TypeError, ValueError, OverflowError):
        return None
    if event.start < 0 or event.end <= event.start or not 0 <= event.pitch <= 127:
        return None
    return event


def _extract_activation(model_output: Any) -> tuple[Any | None, int]:
    np = _require_numpy()
    if not isinstance(model_output, dict):
        return None, 21
    candidates = [
        value
        for key, value in model_output.items()
        if "note" in str(key).lower() and "onset" not in str(key).lower()
    ]
    for candidate in candidates:
        array = np.asarray(candidate)
        while array.ndim > 2 and array.shape[0] == 1:
            array = array[0]
        if array.ndim != 2:
            continue
        if 84 <= array.shape[1] <= 128:
            pitch_offset = 21 if array.shape[1] == 88 else max(0, 60 - array.shape[1] // 2)
            return np.clip(array.astype(np.float32), 0.0, 1.0), pitch_offset
        if 84 <= array.shape[0] <= 128:
            array = array.T
            pitch_offset = 21 if array.shape[1] == 88 else max(0, 60 - array.shape[1] // 2)
            return np.clip(array.astype(np.float32), 0.0, 1.0), pitch_offset
    return None, 21


def _basic_pitch_unlocked(path: Path, duration_seconds: float) -> _ModelOutput:
    stat = path.stat()
    key = (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from basic_pitch.inference import predict  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        raise AudioVerificationDependencyError(
            "已启用模型复核，但 Basic Pitch 不可用。请安装 audio/model 可选依赖；"
            "若确实只想使用频谱算法，请显式选择 DSP-only。"
        ) from exc
    try:
        prediction = predict(str(path), minimum_note_length=50.0)
    except Exception as exc:  # model backends raise many framework-specific types
        raise AudioVerificationError(
            f"Basic Pitch 无法分析 {path.name}：{type(exc).__name__}: {exc}。"
            "为避免静默降级，本次没有继续删音；可修复模型环境或显式选择 DSP-only。"
        ) from exc
    if not isinstance(prediction, (tuple, list)) or len(prediction) < 3:
        raise AudioVerificationError("Basic Pitch 返回了无法识别的结果格式。")
    raw_output, _midi, raw_events = prediction[:3]
    events = [
        event
        for event in (_coerce_model_event(value) for value in (raw_events or []))
        if event is not None
    ]
    events.sort(key=lambda event: (event.pitch, event.start, event.end))
    activation, pitch_offset = _extract_activation(raw_output)
    try:
        version = package_metadata.version("basic-pitch")
    except package_metadata.PackageNotFoundError:
        version = "unknown"
    result = _ModelOutput(
        events=events,
        activation=activation,
        pitch_offset=pitch_offset,
        duration_seconds=duration_seconds,
        name=f"Basic Pitch {version}",
    )
    _MODEL_CACHE[key] = result
    while len(_MODEL_CACHE) > 2:
        _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
    return result


def _basic_pitch(path: Path, duration_seconds: float) -> _ModelOutput:
    """Serialise heavyweight inference/cache mutation across WebUI jobs."""

    with _MODEL_LOCK:
        return _basic_pitch_unlocked(path, duration_seconds)


def _infer_audio_role(file: MidiFileData) -> str:
    if file.spec.audio_role != "auto":
        return file.spec.audio_role
    name = file.path.stem.casefold()
    if any(token in name for token in ("vocal", "voice", "人声", "歌声", "主唱")):
        return "vocals"
    if any(
        token in name
        for token in ("piano", "accompaniment", "instrumental", "钢琴", "伴奏")
    ):
        return "accompaniment"
    return "mix"


def _demucs_available() -> bool:
    try:
        return importlib.util.find_spec("demucs") is not None
    except (ImportError, ValueError):
        return False


def get_backend_status() -> dict[str, bool | str | None]:
    """Return lightweight local capability flags without loading any model."""

    def available(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            return False

    numpy_available = available("numpy")
    soundfile_available = available("soundfile")
    ffmpeg_path = _resolved_executable("ffmpeg")
    return {
        "numpy_available": numpy_available,
        "basic_pitch_available": available("basic_pitch"),
        "demucs_available": _demucs_available(),
        "soundfile_available": soundfile_available,
        "ffmpeg_available": ffmpeg_path is not None,
        "ffmpeg_path": ffmpeg_path,
        "wav_decoder_available": True,
        "compressed_audio_decoder_available": soundfile_available
        or ffmpeg_path is not None,
        "dsp_available": numpy_available
        and (soundfile_available or ffmpeg_path is not None),
    }


def _separate_audio(
    audio_path: Path,
    output_root: Path,
    required: bool,
    progress: Progress | None,
) -> tuple[Path | None, Path | None, str | None]:
    if not _demucs_available():
        if required:
            raise AudioVerificationDependencyError(
                "已要求音源分离，但 Demucs 未安装。请安装 Demucs，或关闭音源分离；"
                "Basic Pitch 和 DSP 仍可直接复核完整混音。"
            )
        return None, None, "Demucs 不可用，已直接使用完整混音复核。"
    _announce(progress, "正在用 Demucs 本地分离人声与伴奏…")
    command = [
        sys.executable,
        "-m",
        "demucs.separate",
        "--two-stems",
        "vocals",
        "-o",
        str(output_root),
        str(audio_path),
    ]
    completed = subprocess.run(
        command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    vocals = list(output_root.rglob("vocals.wav"))
    accompaniment = list(output_root.rglob("no_vocals.wav"))
    if completed.returncode or not vocals or not accompaniment:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if required:
            raise AudioVerificationError(f"Demucs 音源分离失败：{detail[-500:]}")
        return None, None, "Demucs 分离失败，已直接使用完整混音复核。"
    return vocals[0], accompaniment[0], None


def _spectral_measure(
    samples: Any, sample_rate: int, time_seconds: float, pitch: int
) -> tuple[float, float]:
    """Return harmonic support and absolute target-band energy."""

    np = _require_numpy()
    frequency = 440.0 * 2.0 ** ((pitch - 69) / 12.0)
    if frequency <= 0 or frequency >= sample_rate / 2:
        return 0.0, 0.0
    wanted = max(2048, int(math.ceil(sample_rate * 9.0 / frequency)))
    nfft = 1 << (wanted - 1).bit_length()
    nfft = max(2048, min(16384, nfft))
    centre = int(round(time_seconds * sample_rate))
    first = centre - nfft // 2
    last = first + nfft
    frame = np.zeros(nfft, dtype=np.float32)
    source_first = max(0, first)
    source_last = min(len(samples), last)
    if source_last <= source_first:
        return 0.0, 0.0
    frame[source_first - first : source_last - first] = samples[source_first:source_last]
    frame *= np.hanning(nfft).astype(np.float32)
    power = np.abs(np.fft.rfft(frame)) ** 2
    if float(power.sum()) <= 1e-10:
        return 0.0, 0.0
    bin_hz = sample_rate / nfft
    scores: list[tuple[float, float]] = []
    total_target = 0.0
    for harmonic in range(1, 7):
        target_hz = frequency * harmonic
        if target_hz >= sample_rate * 0.47:
            break
        centre_bin = int(round(target_hz / bin_hz))
        width = max(1, int(round(target_hz * (2 ** (38 / 1200) - 1) / bin_hz)))
        lo = max(1, centre_bin - width)
        hi = min(len(power), centre_bin + width + 1)
        target = float(np.max(power[lo:hi])) if hi > lo else 0.0
        radius = max(width * 5, 5)
        noise_lo = max(1, centre_bin - radius)
        noise_hi = min(len(power), centre_bin + radius + 1)
        noise_values = np.concatenate((power[noise_lo:lo], power[hi:noise_hi]))
        noise = float(np.median(noise_values)) if len(noise_values) else 0.0
        snr_db = 10.0 * math.log10((target + 1e-15) / (noise + 1e-15))
        score = _sigmoid((snr_db - 7.0) / 3.0)
        weight = 1.0 / harmonic
        scores.append((score, weight))
        total_target += target * weight
    if not scores:
        return 0.0, 0.0
    weighted = sum(score * weight for score, weight in scores) / sum(
        weight for _score, weight in scores
    )
    # A real fundamental should not be supported only by a single coincident
    # upper partial.  Give the fundamental extra authority without requiring
    # every harmonic in a dense mix.
    support = 0.62 * weighted + 0.38 * scores[0][0]
    return _clip(support), total_target


def _note_dsp_evidence(
    analysis: _AudioAnalysis,
    audio_start: float,
    audio_end: float,
    pitch: int,
    onset_tolerance: float,
    cache: dict[tuple[int, int], tuple[float, float]],
) -> tuple[float, float]:
    np = _require_numpy()
    duration = max(0.03, audio_end - audio_start)
    after_time = audio_start + min(0.07, duration * 0.35)
    sustain_time = audio_start + min(0.30, duration * 0.60)
    before_time = max(0.0, audio_start - 0.075)

    def measured(time: float) -> tuple[float, float]:
        key = (int(round(time * 200)), int(pitch))
        if key not in cache:
            cache[key] = _spectral_measure(
                analysis.samples, analysis.sample_rate, time, pitch
            )
        return cache[key]

    after_support, after_energy = measured(after_time)
    sustain_support, _sustain_energy = measured(sustain_time)
    _before_support, before_energy = measured(before_time)
    harmonic = _clip(0.70 * max(after_support, sustain_support) + 0.30 * min(after_support, sustain_support))

    frame_rate = analysis.sample_rate / analysis.onset_hop
    first = max(0, int((audio_start - onset_tolerance) * frame_rate))
    last = min(
        len(analysis.onset_envelope),
        int(math.ceil((audio_start + onset_tolerance) * frame_rate)) + 1,
    )
    global_onset = (
        float(np.max(analysis.onset_envelope[first:last])) if last > first else 0.0
    )
    rise_db = 10.0 * math.log10((after_energy + 1e-14) / (before_energy + 1e-14))
    pitch_rise = _sigmoid((rise_db - 1.5) / 3.5)
    onset = _clip(0.67 * global_onset + 0.33 * pitch_rise)
    return harmonic, onset


def _model_support(
    model: _ModelOutput,
    start: float,
    end: float,
    pitch: int,
    settings: AudioVerificationSettings,
) -> float:
    tolerance = settings.onset_tolerance_seconds * 1.6
    best = 0.0
    for event in model.events:
        pitch_delta = abs(event.pitch - pitch)
        if pitch_delta > settings.pitch_tolerance_semitones:
            continue
        if event.start > end + tolerance or event.end < start - tolerance:
            continue
        onset_distance = abs(event.start - start)
        onset_score = math.exp(-0.5 * (onset_distance / max(tolerance, 1e-3)) ** 2)
        overlap = max(0.0, min(end, event.end) - max(start, event.start))
        duration_score = _clip(overlap / max(0.05, min(end - start, event.end - event.start)))
        pitch_score = 1.0 if pitch_delta == 0 else 0.72
        amplitude = _clip(
            (event.amplitude - settings.model_min_amplitude)
            / max(0.25, 1.0 - settings.model_min_amplitude)
        )
        score = pitch_score * (
            0.38 * onset_score + 0.27 * duration_score + 0.35 * amplitude
        )
        best = max(best, score)
    activation = model.activation
    pitch_index = pitch - model.pitch_offset
    if activation is not None and 0 <= pitch_index < activation.shape[1]:
        duration = max(model.duration_seconds, 1e-6)
        first = max(0, int((start - settings.onset_tolerance_seconds) / duration * len(activation)))
        last = min(
            len(activation),
            int(math.ceil((end + settings.onset_tolerance_seconds) / duration * len(activation))),
        )
        if last > first:
            activation_score = float(activation[first:last, pitch_index].max())
            best = max(best, 0.90 * _clip(activation_score))
    return _clip(best)


def _attack_groups(notes: Sequence[NoteEvent]) -> list[list[NoteEvent]]:
    groups: list[list[NoteEvent]] = []
    tolerance = Fraction(1, 40)
    for note in sorted(notes, key=lambda item: (item.start_beat, item.pitch)):
        if groups and abs(note.start_beat - groups[-1][0].start_beat) <= tolerance:
            groups[-1].append(note)
        else:
            groups.append([note])
    return groups


def _context_map(file: MidiFileData) -> dict[tuple[int, str], _ContextInfo]:
    np = _require_numpy()
    groups = _attack_groups(file.notes)
    if not groups:
        return {}
    anchors = [float(np.median([float(note.start_beat) for note in group])) for group in groups]
    centres = [float(np.median([note.pitch for note in group])) for group in groups]
    protected: dict[int, tuple[float, set[str]]] = {
        index: (0.0, set()) for index in range(len(groups))
    }

    def protect(indices: Iterable[int], score: float, label: str) -> None:
        for index in indices:
            old_score, labels = protected[index]
            labels.add(label)
            protected[index] = (max(old_score, score), labels)

    # Simultaneous notes and extremely short attacks are never deleted merely
    # because a transcription model missed them.  They may be chord tones,
    # acciaccaturas or deliberately clipped articulation.
    for index, group in enumerate(groups):
        if len(group) > 1:
            protect((index,), 0.76, "同时发声和弦")
        if min(float(note.duration_beats) for note in group) <= 0.16:
            protect((index,), 0.78, "极短攻击/可能的倚音")

    # Grace-like lead-ins: brief, close and pitch-related to a stronger attack.
    for index, group in enumerate(groups[:-1]):
        gap = anchors[index + 1] - anchors[index]
        duration = max(float(note.duration_beats) for note in group)
        if duration <= 0.28 and 0.015 <= gap <= 0.36 and abs(centres[index + 1] - centres[index]) <= 12:
            protect((index, index + 1), 0.88, "倚音/短装饰音")

    # Alternating rapid pitches are turns, mordents or trills.
    for first in range(len(groups) - 2):
        a, b, c = centres[first : first + 3]
        gaps = [anchors[first + 1] - anchors[first], anchors[first + 2] - anchors[first + 1]]
        if max(gaps) <= 0.55 and abs(a - c) <= 1 and 1 <= abs(a - b) <= 4:
            protect(range(first, first + 3), 0.93, "颤音/回音/波音")

    # Three or more monotonic quick attacks form a likely written arpeggio.
    for first in range(len(groups) - 2):
        last = first + 2
        while last + 1 < len(groups) and anchors[last + 1] - anchors[first] <= 0.70:
            last += 1
        sequence = centres[first : last + 1]
        if len(sequence) >= 3 and anchors[last] - anchors[first] <= 0.70:
            deltas = [right - left for left, right in zip(sequence, sequence[1:])]
            if all(delta > 0 for delta in deltas) or all(delta < 0 for delta in deltas):
                if abs(sequence[-1] - sequence[0]) >= 4:
                    protect(range(first, last + 1), 0.90, "琶音")

    # Any evenly spaced 3/5/6/7-note run is protected.  Quantisation later
    # decides whether it is a triplet, quintuplet, septuplet or straight run.
    for size in (7, 6, 5, 3):
        for first in range(len(groups) - size + 1):
            gaps = np.diff(anchors[first : first + size])
            mean_gap = float(np.mean(gaps))
            tuplet_slots = [span / size for span in (0.5, 1.0, 2.0, 4.0)]
            tuplet_like = min(
                abs(mean_gap - slot) / max(slot, 1e-9) for slot in tuplet_slots
            ) <= 0.13
            if (
                0.025 <= mean_gap <= 0.70
                and tuplet_like
                and float(np.std(gaps)) <= mean_gap * 0.16
            ):
                protect(range(first, first + size), 0.82, f"{size}音等距连音候选")

    # Repeating long-short pairs can be performed swing rather than bad timing.
    for first in range(len(groups) - 4):
        gaps = np.diff(anchors[first : first + 5])
        if min(gaps) <= 0:
            continue
        ratios = [max(gaps[i], gaps[i + 1]) / min(gaps[i], gaps[i + 1]) for i in (0, 2)]
        orientations = [gaps[i] > gaps[i + 1] for i in (0, 2)]
        if all(1.45 <= ratio <= 3.2 for ratio in ratios) and orientations[0] == orientations[1]:
            protect(range(first, first + 5), 0.80, "Swing/不均分节奏")

    note_to_group = {
        (note.source_index, str(note.note_id)): index
        for index, group in enumerate(groups)
        for note in group
    }
    output: dict[tuple[int, str], _ContextInfo] = {}
    velocities = np.asarray([note.velocity for note in file.notes], dtype=np.float64)
    global_low = float(np.percentile(velocities, 15))
    global_high = float(np.percentile(velocities, 80))
    global_median = float(np.median(velocities))
    # A percentile is not itself evidence of an outlier: when every attack has
    # velocity 85, P15 is also 85.  Require a musically meaningful drop from
    # the body of the track, and only admit a tied P15 value when that lower
    # cluster is genuinely separated from the median.
    velocity_margin = max(6.0, global_median * 0.10)
    low_tail_separated = global_median - global_low >= velocity_margin
    pitch_class_counts: dict[int, int] = defaultdict(int)
    for note in file.notes:
        pitch_class_counts[note.pitch % 12] += 1
    maximum_pc = max(pitch_class_counts.values(), default=1)
    for note in file.notes:
        key = (note.source_index, str(note.note_id))
        index = note_to_group[key]
        velocity = _clip((note.velocity - global_low) / max(5.0, global_high - global_low))
        neighbours: list[float] = []
        if index:
            neighbours.extend(item.pitch for item in groups[index - 1])
        if index + 1 < len(groups):
            neighbours.extend(item.pitch for item in groups[index + 1])
        distance = min((abs(note.pitch - pitch) for pitch in neighbours), default=7.0)
        continuity = math.exp(-max(0.0, distance - 5.0) / 12.0)
        prevalence = math.sqrt(pitch_class_counts[note.pitch % 12] / maximum_pc)
        duration = _clip(float(note.duration_beats) / 0.50)
        chord = groups[index]
        if len(chord) > 1:
            intervals = {abs(note.pitch - other.pitch) % 12 for other in chord if other is not note}
            chord_fit = 0.90 if intervals & {0, 3, 4, 5, 7, 8, 9} else 0.58
        else:
            chord_fit = 0.72
        support = _clip(
            0.30 * velocity
            + 0.28 * continuity
            + 0.18 * prevalence
            + 0.12 * duration
            + 0.12 * chord_fit
        )
        guard, labels = protected[index]
        risk = 0.0
        risk_labels: list[str] = []
        velocity_outlier = (
            note.velocity <= global_median - velocity_margin
            and (
                note.velocity < global_low
                or (low_tail_separated and note.velocity <= global_low)
            )
        )
        if velocity_outlier:
            risk = max(risk, 0.78)
            risk_labels.append("力度位于本轨最低约15%")
        if distance >= 12:
            risk = max(risk, 0.68)
            risk_labels.append("与前后攻击相距至少一个八度")
        if len(chord) > 1:
            same_pitch = sum(other.pitch == note.pitch for other in chord)
            chord_velocities = [other.velocity for other in chord]
            dissonant_only = intervals and not intervals & {0, 3, 4, 5, 7, 8, 9}
            if same_pitch > 1:
                risk = max(risk, 0.90)
                risk_labels.append("同一攻击含重复音高")
            elif dissonant_only and note.velocity < float(np.median(chord_velocities)):
                risk = max(risk, 0.62)
                risk_labels.append("同起和弦中的弱不协和离群音")
        previous_gap = anchors[index] - anchors[index - 1] if index else math.inf
        next_gap = anchors[index + 1] - anchors[index] if index + 1 < len(groups) else math.inf
        if len(chord) == 1 and min(previous_gap, next_gap) >= 1.5 and distance >= 9:
            risk = max(risk, 0.72)
            risk_labels.append("时间与音高都孤立")
        output[key] = _ContextInfo(
            support,
            guard,
            tuple(sorted(labels)),
            risk,
            tuple(risk_labels),
        )
    return output


def _evidence_scores(
    harmonic: float,
    onset: float,
    model: float | None,
    context: float,
) -> tuple[float, float]:
    if model is None:
        confidence = 0.50 * harmonic + 0.29 * onset + 0.21 * context
    else:
        confidence = 0.38 * harmonic + 0.20 * onset + 0.31 * model + 0.11 * context
    absence = 1.0 - confidence
    extra_probability = _sigmoid((absence - 0.58) * 8.5)
    return _clip(confidence), _clip(extra_probability)


_DECISION_THRESHOLDS = {
    # extra, harmonic-max, onset-max, model-max, context-max, guard-max,
    # alignment-min, structural-risk-min
    "conservative": (0.91, 0.18, 0.23, 0.20, 0.48, 0.24, 0.42, 0.60),
    "balanced": (0.84, 0.26, 0.32, 0.29, 0.58, 0.35, 0.34, 0.54),
    "strict": (0.76, 0.34, 0.40, 0.38, 0.68, 0.48, 0.28, 0.45),
}


def _decision(
    settings: AudioVerificationSettings,
    alignment: AudioAlignment,
    harmonic: float,
    onset: float,
    model: float | None,
    context: _ContextInfo,
    extra_probability: float,
) -> str:
    (
        extra_min,
        harmonic_max,
        onset_max,
        model_max,
        context_max,
        guard_max,
        align_min,
        risk_min,
    ) = _DECISION_THRESHOLDS[settings.mode]
    if settings.dsp_only:
        # DSP-only deletion demands stronger absence than model-assisted mode.
        extra_min = min(0.97, extra_min + 0.045)
        harmonic_max *= 0.78
        onset_max *= 0.80
    removable = (
        alignment.confidence >= align_min
        and not alignment.ambiguous
        and alignment.max_window_residual_seconds <= 0.08
        and extra_probability >= extra_min
        and harmonic <= harmonic_max
        and onset <= onset_max
        and (model is None or model <= model_max)
        and context.support <= context_max
        and context.special_guard <= guard_max
        and context.structural_risk >= risk_min
    )
    if removable:
        # DSP-only is a diagnostic fallback, not an authority to delete.  The
        # destructive decision contract requires model + frequency + onset
        # absence and an independent structural risk.
        return "review" if settings.dsp_only else "remove"
    review_min = {"conservative": 0.66, "balanced": 0.60, "strict": 0.54}[
        settings.mode
    ]
    conflicting = max(
        harmonic,
        onset,
        model if model is not None else 0.0,
    ) - min(harmonic, onset, model if model is not None else harmonic) >= 0.58
    if (
        extra_probability >= review_min
        or (context.special_guard >= 0.75 and extra_probability >= 0.48)
        or conflicting
    ):
        return "review"
    return "keep"


def _measure_number(file: MidiFileData, beat: Fraction) -> int:
    changes: dict[Fraction, tuple[int, int]] = {Fraction(0): file.initial_time_signature}
    for event in file.time_signature_events:
        if event.beat >= 0:
            changes[Fraction(event.beat)] = event.signature
    ordered = sorted(changes.items())
    measure = 1
    cursor = Fraction(0)
    signature = ordered[0][1]
    for change_beat, next_signature in ordered[1:]:
        if beat < change_beat:
            break
        measure_length = Fraction(signature[0] * 4, signature[1])
        measure += max(0, math.ceil(float((change_beat - cursor) / measure_length)))
        cursor = change_beat
        signature = next_signature
    measure_length = Fraction(signature[0] * 4, signature[1])
    if beat > cursor:
        measure += int((beat - cursor) // measure_length)
    return max(1, measure)


def _reason_list(
    harmonic: float,
    onset: float,
    model: float | None,
    context: _ContextInfo,
    alignment: AudioAlignment,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if harmonic < 0.28:
        reasons.append("基频与泛音证据很弱")
    if onset < 0.28:
        reasons.append("对应时刻没有清晰起音")
    if model is not None and model < 0.28:
        reasons.append("Basic Pitch 未支持该音")
    if context.support < 0.38:
        reasons.append("力度、时值或相邻音上下文异常")
    if context.labels:
        reasons.append("疑似" + "、".join(context.labels))
    if context.structural_reasons:
        reasons.append("结构风险：" + "、".join(context.structural_reasons))
    if alignment.confidence < 0.42:
        reasons.append("音频与 MIDI 对齐置信度不足")
    if alignment.ambiguous:
        reasons.append("全曲存在多个近似对齐峰")
    return tuple(reasons)


def _append_warning(target: list[str], warning: str) -> None:
    if warning not in target:
        target.append(warning)


def _possible_missing_items(
    files: Sequence[MidiFileData],
    mappers: dict[int, _TempoMapper],
    alignment: AudioAlignment,
    models_by_source: dict[int, _ModelOutput],
    analyses_by_source: dict[int, _AudioAnalysis],
    settings: AudioVerificationSettings,
    spectral_caches: dict[int, dict[tuple[int, int], tuple[float, float]]],
) -> list[AudioReviewItem]:
    if settings.dsp_only or alignment.confidence < 0.34:
        return []
    note_times: dict[int, list[tuple[float, NoteEvent]]] = {}
    for file in files:
        mapper = mappers[file.source_index]
        note_times[file.source_index] = sorted(
            (
                (
                    alignment.map_seconds(mapper.seconds(note.start_beat)),
                    note,
                )
                for note in file.notes
            ),
            key=lambda pair: (pair[0], pair[1].pitch, str(pair[1].note_id)),
        )
    items: list[AudioReviewItem] = []
    files_by_model: dict[int, list[MidiFileData]] = defaultdict(list)
    model_objects: dict[int, _ModelOutput] = {}
    for file in files:
        model = models_by_source[file.source_index]
        identity = id(model)
        model_objects[identity] = model
        files_by_model[identity].append(file)
    for identity, candidate_files in files_by_model.items():
        model = model_objects[identity]
        for event in model.events:
            if event.amplitude < max(0.68, settings.model_min_amplitude + 0.28):
                continue
            if not 0.08 <= event.end - event.start <= 12.0:
                continue
            matched = False
            for other_file in candidate_files:
                other_mapper = mappers[other_file.source_index]
                for note in other_file.notes:
                    original_pitch = (
                        note.original_pitch
                        if note.original_pitch is not None
                        else note.pitch - other_file.spec.total_transpose_semitones
                    )
                    if min(
                        abs(int(original_pitch) - event.pitch),
                        abs(note.pitch - event.pitch),
                    ) > settings.pitch_tolerance_semitones:
                        continue
                    start = alignment.map_seconds(other_mapper.seconds(note.start_beat))
                    end = alignment.map_seconds(other_mapper.seconds(note.end_beat))
                    if event.start <= end + 0.08 and event.end >= start - 0.08:
                        matched = True
                        break
                if matched:
                    break
            if matched:
                continue

            neighbouring_attacks: list[tuple[float, MidiFileData, NoteEvent]] = []
            for candidate_file in candidate_files:
                source = candidate_file.source_index
                times = note_times[source]
                attack_values = [value[0] for value in times]
                index = bisect.bisect_left(attack_values, event.start)
                for candidate in (index - 1, index):
                    if not 0 <= candidate < len(times):
                        continue
                    delta = abs(times[candidate][0] - event.start)
                    if delta <= settings.onset_tolerance_seconds:
                        neighbouring_attacks.append(
                            (delta, candidate_file, times[candidate][1])
                        )
            if not neighbouring_attacks:
                # A full mix contains instruments not represented by the
                # supplied MIDI files.  Only report a missing tone when it
                # coincides with an existing attack in a candidate stem.
                continue
            _delta, file, anchor = min(
                neighbouring_attacks,
                key=lambda value: (
                    value[0],
                    abs(value[2].pitch - event.pitch),
                    value[1].source_index,
                ),
            )
            source = file.source_index
            analysis = analyses_by_source[source]
            harmonic, onset = _note_dsp_evidence(
                analysis,
                event.start,
                event.end,
                event.pitch,
                settings.onset_tolerance_seconds,
                spectral_caches[source],
            )
            if harmonic < 0.58 or onset < 0.38:
                continue
            confidence = _clip(
                0.46 * event.amplitude + 0.34 * harmonic + 0.20 * onset
            )
            output_pitch = event.pitch + file.spec.total_transpose_semitones
            items.append(
                AudioReviewItem(
                    kind="possible_missing",
                    message=(
                        f"模型和频谱都检测到原始音高 MIDI {event.pitch}，"
                        f"对应输出音高为 {output_pitch}，但同一攻击中没有该音；"
                        "完整混音可能包含其他乐器，仅建议人工复核，不会自动补音。"
                    ),
                    measure=_measure_number(file, Fraction(anchor.start_beat)),
                    source_index=source,
                    source_path=file.path,
                    pitch=output_pitch,
                    original_pitch=event.pitch,
                    start_beat=anchor.start_beat,
                    audio_start_seconds=event.start,
                    confidence=confidence,
                )
            )
    return items


def _sync_report(
    files: Sequence[MidiFileData],
    report: ConversionReport,
    result: AudioVerificationResult,
) -> None:
    report.audio_verification_enabled = True
    report.audio_model_name = result.model_name
    report.audio_dsp_only = result.dsp_only
    report.audio_source_separated = result.source_separated
    report.audio_alignment_offset_seconds = result.alignment.offset_seconds
    report.audio_alignment_scale = result.alignment.scale
    report.audio_alignment_confidence = result.alignment.confidence
    report.audio_notes_checked = len(result.evidence)
    report.audio_notes_removed = len(result.removed_note_ids)
    report.audio_possible_extra = sum(
        item.kind == "possible_extra"
        and (item.evidence is None or item.evidence.decision != "remove")
        for item in result.review_items
    )
    report.audio_possible_missing = sum(
        item.kind == "possible_missing" for item in result.review_items
    )
    report.audio_review_items = list(result.review_items)
    report.low_confidence_measures = sorted(
        set(report.low_confidence_measures)
        | {item.measure for item in result.review_items if item.measure > 0}
    )
    for warning in result.warnings:
        _append_warning(report.warnings, warning)
    for file in files:
        file.report.audio_alignment_offset_seconds = result.alignment.offset_seconds
        file.report.audio_alignment_scale = result.alignment.scale
        file.report.audio_alignment_confidence = result.alignment.confidence
        file.report.audio_source_separated = result.source_separated


def verify_audio(
    files: Sequence[MidiFileData],
    settings: AudioVerificationSettings,
    report: ConversionReport | None = None,
    progress: Progress | None = None,
) -> AudioVerificationResult:
    """Verify and optionally filter notes against a local original audio file.

    ``files`` are mutated only after all evidence has been computed.  A model
    import/inference failure therefore cannot leave a half-filtered score.
    The returned list contains the same ``MidiFileData`` objects for efficient
    integration with the existing normalisation pipeline.
    """

    if not files:
        raise ValueError("音频复核需要至少一个已读取的 MIDI 文件。")
    if not settings.enabled:
        return AudioVerificationResult(
            files=list(files), alignment=AudioAlignment(), dsp_only=settings.dsp_only
        )
    if settings.audio_path is None:
        raise ValueError("已启用原始音频复核，但没有选择原始音频文件。")
    audio_path = Path(settings.audio_path).expanduser().resolve()
    if not audio_path.is_file():
        raise FileNotFoundError(audio_path)
    if not any(file.notes for file in files):
        raise ValueError("MIDI 中没有可供音频复核的音符。")
    report = report or ConversionReport(files=[file.report for file in files])
    warnings: list[str] = []
    mappers = {file.source_index: _TempoMapper(file) for file in files}

    _announce(progress, f"正在解码原始音频：{audio_path.name}…")
    original_analysis = _analyse_audio(audio_path, settings)
    analyses_by_path: dict[Path, _AudioAnalysis] = {audio_path: original_analysis}
    source_paths = {file.source_index: audio_path for file in files}

    with tempfile.TemporaryDirectory(prefix="midi-to-mscz-demucs-") as temp_name:
        source_separated = False
        if settings.source_separation is not False:
            vocals, accompaniment, separation_warning = _separate_audio(
                audio_path,
                Path(temp_name),
                required=settings.source_separation is True,
                progress=progress,
            )
            if separation_warning:
                warnings.append(separation_warning)
            if vocals and accompaniment:
                source_separated = True
                roles = {file.source_index: _infer_audio_role(file) for file in files}
                for file in files:
                    role = roles[file.source_index]
                    if role == "vocals":
                        source_paths[file.source_index] = vocals
                    elif role == "accompaniment":
                        source_paths[file.source_index] = accompaniment
                warnings.append(
                    "已用 Demucs 分离人声/伴奏后分别复核；无法单独分离的乐器仍按伴奏整体判断。"
                )

        attacks = _attack_times(files, mappers)
        _announce(progress, "正在对齐音频与 MIDI 时间轴…")
        alignment = _align_midi_to_audio(attacks, original_analysis, settings)
        if alignment.confidence < 0.42:
            warnings.append(
                "音频与 MIDI 的自动对齐置信度较低；已禁止保守模式下的自动删音，"
                "相关位置只进入人工复核。"
            )
        if alignment.ambiguous:
            warnings.append(
                "全曲互相关出现多个强度接近的对齐候选；已降低对齐置信度并禁止自动删音。"
            )
        if alignment.max_window_residual_seconds > 0.08:
            warnings.append(
                "分段对齐残差偏大，可能存在局部速度漂移或临时速度信息不一致；"
                "相关结果仅作人工复核参考。"
            )

        for path in sorted(set(source_paths.values()), key=str):
            if path not in analyses_by_path:
                _announce(progress, f"正在分析分离音轨：{path.name}…")
                analyses_by_path[path] = _analyse_audio(path, settings)
        analyses_by_source = {
            source: analyses_by_path[path] for source, path in source_paths.items()
        }

        models_by_source: dict[int, _ModelOutput] = {}
        model_name: str | None = None
        if not settings.dsp_only:
            models_by_path: dict[Path, _ModelOutput] = {}
            for path in sorted(set(source_paths.values()), key=str):
                _announce(progress, f"正在用 Basic Pitch 复核：{path.name}…")
                models_by_path[path] = _basic_pitch(
                    path, analyses_by_path[path].duration_seconds
                )
            models_by_source = {
                source: models_by_path[path] for source, path in source_paths.items()
            }
            model_names = sorted({model.name for model in models_by_path.values()})
            model_name = " + ".join(model_names) + " + DSP"
        else:
            warnings.append(
                "当前为显式 DSP-only：报告没有神经模型证据，自动删除阈值已进一步收紧。"
            )
            model_name = "DSP-only"

        _announce(progress, "正在综合频谱、起音、模型与音乐上下文证据…")
        context_by_source = {
            file.source_index: _context_map(file) for file in files
        }
        spectral_caches: dict[int, dict[tuple[int, int], tuple[float, float]]] = {
            file.source_index: {} for file in files
        }
        evidence: list[AudioNoteEvidence] = []
        pending_remove: dict[int, set[str]] = defaultdict(set)
        review_items: list[AudioReviewItem] = []
        for file in files:
            source = file.source_index
            mapper = mappers[source]
            analysis = analyses_by_source[source]
            model = models_by_source.get(source)
            for note in file.notes:
                midi_start = mapper.seconds(note.start_beat)
                midi_end = mapper.seconds(note.end_beat)
                audio_start = alignment.map_seconds(midi_start)
                audio_end = alignment.map_seconds(max(midi_start + 0.025, midi_end))
                analysis_pitch = int(
                    note.original_pitch
                    if note.original_pitch is not None
                    else note.pitch - file.spec.total_transpose_semitones
                )
                candidate_pitches = tuple(dict.fromkeys((analysis_pitch, note.pitch)))
                pitch_evidence: list[tuple[int, float, float, float | None]] = []
                for candidate_pitch in candidate_pitches:
                    candidate_harmonic, candidate_onset = _note_dsp_evidence(
                        analysis,
                        audio_start,
                        audio_end,
                        candidate_pitch,
                        settings.onset_tolerance_seconds,
                        spectral_caches[source],
                    )
                    candidate_model = (
                        _model_support(
                            model,
                            audio_start,
                            audio_end,
                            candidate_pitch,
                            settings,
                        )
                        if model is not None
                        else None
                    )
                    pitch_evidence.append(
                        (
                            candidate_pitch,
                            candidate_harmonic,
                            candidate_onset,
                            candidate_model,
                        )
                    )
                # A user transpose may be notation-only or may correct an
                # octave error in the recogniser.  Destructive cleanup is only
                # allowed when *both* original and transformed pitches lack
                # support, so keep the strongest evidence in every family.
                harmonic = max(value[1] for value in pitch_evidence)
                onset = max(value[2] for value in pitch_evidence)
                model_score = (
                    max(float(value[3] or 0.0) for value in pitch_evidence)
                    if model is not None
                    else None
                )
                matched_audio_pitch = max(
                    pitch_evidence,
                    key=lambda value: (
                        0.42 * value[1]
                        + 0.23 * value[2]
                        + 0.35 * float(value[3] or 0.0)
                    ),
                )[0]
                context = context_by_source[source].get(
                    (source, str(note.note_id)), _ContextInfo(0.5, 0.0, ())
                )
                confidence, extra_probability = _evidence_scores(
                    harmonic, onset, model_score, context.support
                )
                decision = _decision(
                    settings,
                    alignment,
                    harmonic,
                    onset,
                    model_score,
                    context,
                    extra_probability,
                )
                reasons = _reason_list(
                    harmonic, onset, model_score, context, alignment
                )
                item = AudioNoteEvidence(
                    note_id=note.note_id,
                    source_index=source,
                    source_path=file.path,
                    pitch=note.pitch,
                    original_pitch=analysis_pitch,
                    matched_audio_pitch=matched_audio_pitch,
                    start_beat=note.start_beat,
                    audio_start_seconds=audio_start,
                    audio_end_seconds=audio_end,
                    harmonic_support=harmonic,
                    onset_support=onset,
                    model_support=model_score,
                    context_support=context.support,
                    confidence=confidence,
                    extra_probability=extra_probability,
                    special_rhythm_guard=context.special_guard,
                    structural_risk=context.structural_risk,
                    structural_reasons=context.structural_reasons,
                    decision=decision,  # type: ignore[arg-type]
                    reasons=reasons,
                )
                evidence.append(item)
                note.audio_confidence = confidence
                note.audio_decision = decision  # type: ignore[assignment]
                if decision == "remove":
                    pending_remove[source].add(str(note.note_id))
                if decision in {"review", "remove"}:
                    review_items.append(
                        AudioReviewItem(
                            kind="possible_extra",
                            message=(
                                f"疑似多余音 MIDI {note.pitch}"
                                + (
                                    f"（音频同时核对原始音高 {analysis_pitch} 与输出音高；"
                                    f"较强证据来自 {matched_audio_pitch}）"
                                    if analysis_pitch != note.pitch
                                    else ""
                                )
                                + "；"
                                + ("；".join(reasons) if reasons else "四类证据总体偏弱")
                                + ("。已建议删除。" if decision == "remove" else "。已保留待复核。")
                            ),
                            measure=_measure_number(file, Fraction(note.start_beat)),
                            source_index=source,
                            source_path=file.path,
                            note_id=note.note_id,
                            pitch=note.pitch,
                            original_pitch=analysis_pitch,
                            start_beat=note.start_beat,
                            audio_start_seconds=audio_start,
                            confidence=extra_probability,
                            evidence=item,
                        )
                    )

        missing = _possible_missing_items(
            files,
            mappers,
            alignment,
            models_by_source,
            analyses_by_source,
            settings,
            spectral_caches,
        )
        review_items.extend(missing)

        removed_note_ids: list[str | int] = []
        for file in files:
            source = file.source_index
            recommendations = pending_remove[source]
            file.report.audio_notes_checked = sum(
                item.source_index == source for item in evidence
            )
            file.report.audio_possible_extra = sum(
                item.kind == "possible_extra"
                and item.source_index == source
                and (item.evidence is None or item.evidence.decision != "remove")
                for item in review_items
            )
            file.report.audio_possible_missing = sum(
                item.kind == "possible_missing" and item.source_index == source
                for item in review_items
            )
            if settings.auto_remove and recommendations:
                kept: list[NoteEvent] = []
                for note in file.notes:
                    if str(note.note_id) in recommendations:
                        removed_note_ids.append(note.note_id)
                    else:
                        kept.append(note)
                file.notes = kept
            file.report.audio_notes_removed = (
                len(recommendations) if settings.auto_remove else 0
            )
            file.report.notes_kept = len(file.notes)
            file.report.audio_alignment_offset_seconds = alignment.offset_seconds
            file.report.audio_alignment_scale = alignment.scale
            file.report.audio_alignment_confidence = alignment.confidence
            if recommendations:
                action = "已删除" if settings.auto_remove else "建议删除但已保留"
                _append_warning(
                    file.report.warnings,
                    f"原始音频复核{action} {len(recommendations)} 个高置信疑似多余音。",
                )

        review_items.sort(
            key=lambda item: (
                item.measure,
                item.source_index if item.source_index is not None else -1,
                item.audio_start_seconds if item.audio_start_seconds is not None else -1.0,
                item.pitch if item.pitch is not None else -1,
            )
        )
        if len(review_items) > settings.max_review_items:
            hidden = len(review_items) - settings.max_review_items
            review_items = review_items[: settings.max_review_items]
            warnings.append(
                f"复核项超过显示上限，已省略 {hidden} 项；可提高 max_review_items 后重新分析。"
            )
        if any(item.kind == "possible_missing" for item in review_items):
            warnings.append(
                "疑似漏音只做报告，绝不会自动补音；完整混音中的其他乐器也可能触发该提示。"
            )
        result = AudioVerificationResult(
            files=list(files),
            alignment=alignment,
            evidence=evidence,
            review_items=review_items,
            removed_note_ids=removed_note_ids,
            model_name=model_name,
            dsp_only=settings.dsp_only,
            source_separated=source_separated,
            warnings=warnings,
        )
        _sync_report(files, report, result)
        _announce(
            progress,
            f"音频复核完成：检查 {len(evidence)} 个音，"
            f"删除 {len(removed_note_ids)} 个，需人工复核 {len(review_items)} 项。",
        )
        return result


__all__ = [
    "AudioVerificationDependencyError",
    "AudioVerificationError",
    "get_backend_status",
    "verify_audio",
]
