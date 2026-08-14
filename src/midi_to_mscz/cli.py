"""Command line interface for the MIDI to MuseScore converter.

The GUI imports the small backend-adapter helpers in this module too.  Keeping
that adapter in one place makes the front ends tolerant of harmless model field
renames while the notation pipeline evolves.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence, get_type_hints


STAFF_NAMES = {
    "treble": "treble",
    "upper": "treble",
    "right": "treble",
    "high": "treble",
    "高音": "treble",
    "高音谱表": "treble",
    "bass": "bass",
    "lower": "bass",
    "left": "bass",
    "low": "bass",
    "低音": "bass",
    "低音谱表": "bass",
}


class UserInputError(ValueError):
    """An error that should be shown without a traceback."""


def _load_backend() -> tuple[Callable[..., Any], type[Any], type[Any]]:
    """Load the conversion function and its two public configuration models."""

    try:
        pipeline = importlib.import_module(".pipeline", __package__)
    except Exception as exc:  # pragma: no cover - depends on local installation
        raise RuntimeError(f"无法载入转换管线：{exc}") from exc

    convert = getattr(pipeline, "convert", None)
    if not callable(convert):
        raise RuntimeError("转换管线缺少 convert() 函数。")

    modules = [pipeline]
    for name in ("models", "config", "types"):
        try:
            modules.append(importlib.import_module(f".{name}", __package__))
        except ImportError:
            pass

    def find_model(name: str) -> type[Any] | None:
        for module in modules:
            value = getattr(module, name, None)
            if isinstance(value, type):
                return value
        return None

    input_spec = find_model("InputSpec")
    settings = find_model("ConversionSettings")
    if input_spec is None or settings is None:
        raise RuntimeError(
            "转换管线未导出 InputSpec / ConversionSettings；请检查安装是否完整。"
        )
    return convert, input_spec, settings


_INPUT_ALIASES: Mapping[str, tuple[str, ...]] = {
    "path": ("path", "source_path", "midi_path", "input_path", "file"),
    "staff": ("staff", "target_staff", "clef", "hand"),
    "transpose_octaves": (
        "transpose_octaves",
        "octave_shift",
        "transpose_octave",
        "octaves",
    ),
    "transpose_semitones": (
        "transpose_semitones",
        "semitone_shift",
        "pitch_shift",
        "transpose",
        "semitones",
    ),
    "velocity_min": (
        "velocity_min",
        "min_velocity",
        "velocity_threshold",
        "velocity_cutoff",
    ),
    "offset_beats": (
        "offset_beats",
        "beat_offset",
        "manual_offset_beats",
        "timing_offset_beats",
    ),
}


_SETTINGS_ALIASES: Mapping[str, tuple[str, ...]] = {
    "bpm": ("bpm", "tempo"),
    "time_signature": ("time_signature", "meter"),
    "numerator": ("numerator", "time_numerator", "beats_per_measure"),
    "denominator": ("denominator", "time_denominator", "beat_unit"),
    "key_signature": ("key_signature", "key_fifths", "fifths", "key"),
    "min_note": (
        "min_note",
        "minimum_note",
        "smallest_note",
        "quantization",
        "quantize_unit",
        "grid",
    ),
    "auto_latency": (
        "auto_latency",
        "detect_latency",
        "correct_latency",
        "automatic_delay",
    ),
    "trim_common_leading": (
        "trim_common_leading",
        "auto_trim",
        "crop_common_leading",
        "trim_leading",
        "crop_leading",
    ),
    "preserve_arpeggios": (
        "preserve_arpeggios",
        "detect_arpeggios",
        "arpeggios",
    ),
    "detect_tuplets": (
        "detect_tuplets",
        "detect_triplets",
        "preserve_tuplets",
        "tuplets",
        "triplets",
    ),
    "detect_grace_notes": (
        "detect_grace_notes",
        "preserve_grace_notes",
        "grace_notes",
        "graces",
    ),
    "detect_swing": ("detect_swing", "preserve_swing", "swing"),
    "detect_ornaments": (
        "detect_ornaments",
        "preserve_ornaments",
        "ornaments",
    ),
    "pickup": ("pickup", "has_pickup", "anacrusis", "enable_pickup"),
    "pickup_beats": ("pickup_beats", "anacrusis_beats"),
    "title": ("title", "score_title"),
}


def _normalise_parameter_name(name: str) -> str:
    return name.lower().strip().replace("-", "_")


def _coerce_for_annotation(value: Any, annotation: Any) -> Any:
    """Convert staff strings to an enum when a model uses one."""

    if value is None or annotation in (None, inspect.Parameter.empty, Any):
        return value
    try:
        if inspect.isclass(annotation) and issubclass(annotation, __import__("enum").Enum):
            for member in annotation:
                if str(member.value).lower() == str(value).lower():
                    return member
                if member.name.lower() == str(value).lower():
                    return member
    except (TypeError, AttributeError):
        pass
    return value


def _construct_model(
    model: type[Any],
    values: Mapping[str, Any],
    aliases: Mapping[str, tuple[str, ...]],
) -> Any:
    """Construct a public pipeline dataclass using documented logical fields."""

    try:
        signature = inspect.signature(model)
    except (TypeError, ValueError):
        return model(**dict(values))
    try:
        hints = get_type_hints(model)
    except Exception:
        hints = {}

    reverse: dict[str, str] = {}
    for logical, names in aliases.items():
        for name in names:
            reverse[_normalise_parameter_name(name)] = logical

    kwargs: dict[str, Any] = {}
    missing: list[str] = []
    has_var_kwargs = False
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls"}:
            continue
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_kwargs = True
            continue
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        logical = reverse.get(_normalise_parameter_name(parameter.name))
        if logical is not None and logical in values:
            annotation = hints.get(parameter.name, parameter.annotation)
            kwargs[parameter.name] = _coerce_for_annotation(values[logical], annotation)
        elif parameter.default is inspect.Parameter.empty:
            missing.append(parameter.name)

    if has_var_kwargs:
        for logical, value in values.items():
            canonical = aliases.get(logical, (logical,))[0]
            kwargs.setdefault(canonical, value)
    if missing:
        raise RuntimeError(
            f"{model.__name__} 存在前端无法识别的必填字段：{', '.join(missing)}"
        )
    return model(**kwargs)


def make_input_spec(
    *,
    path: Path | str,
    staff: str,
    transpose_octaves: int = 0,
    transpose_semitones: int = 0,
    velocity_min: int = 1,
    offset_beats: float = 0.0,
    model: type[Any] | None = None,
) -> Any:
    """Create an InputSpec after validating all user-editable values."""

    source = Path(path).expanduser()
    staff_key = STAFF_NAMES.get(staff.strip().lower())
    if staff_key is None:
        raise UserInputError(f"未知谱表“{staff}”，请使用 treble/高音 或 bass/低音。")
    if not -3 <= int(transpose_octaves) <= 3:
        raise UserInputError("升降八度必须在 -3 到 3 之间。")
    if not -11 <= int(transpose_semitones) <= 11:
        raise UserInputError("半音移调必须在 -11 到 11 之间。")
    if not 1 <= int(velocity_min) <= 127:
        raise UserInputError("最低力度必须在 1 到 127 之间。")
    if model is None:
        _, model, _ = _load_backend()
    return _construct_model(
        model,
        {
            "path": source,
            "staff": staff_key,
            "transpose_octaves": int(transpose_octaves),
            "transpose_semitones": int(transpose_semitones),
            "velocity_min": int(velocity_min),
            "offset_beats": float(offset_beats),
        },
        _INPUT_ALIASES,
    )


def make_settings(
    *,
    bpm: float | None,
    time_signature: str,
    key_signature: int | str | None,
    min_note: str,
    auto_latency: bool,
    trim_common_leading: bool,
    preserve_arpeggios: bool,
    detect_tuplets: bool,
    pickup_beats: float,
    title: str,
    detect_grace_notes: bool = True,
    detect_swing: bool = True,
    detect_ornaments: bool = True,
    model: type[Any] | None = None,
) -> Any:
    """Create ConversionSettings with friendly range validation."""

    if bpm is not None and not 20 <= float(bpm) <= 400:
        raise UserInputError("BPM 必须在 20 到 400 之间，或设为 auto。")
    automatic_meter = str(time_signature).strip().lower() in {
        "", "auto", "automatic", "自动"
    }
    if automatic_meter:
        numerator, denominator = 4, 4
        meter_value: str | None = None
    else:
        try:
            numerator_text, denominator_text = time_signature.strip().split("/", 1)
            numerator, denominator = int(numerator_text), int(denominator_text)
        except (ValueError, AttributeError) as exc:
            raise UserInputError("拍号应填 auto，或使用 4/4、3/4、6/8 等格式。") from exc
        if not 1 <= numerator <= 32 or denominator not in {1, 2, 4, 8, 16, 32}:
            raise UserInputError("拍号不合法；分母应为 1、2、4、8、16 或 32。")
        meter_value = f"{numerator}/{denominator}"
    automatic_key = key_signature is None or str(key_signature).strip().lower() in {
        "", "auto", "automatic", "自动"
    }
    if automatic_key:
        key_value: int | None = None
    else:
        try:
            key_value = int(key_signature)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise UserInputError("调号应填 auto，或使用 -7 到 +7 的数字。") from exc
        if not -7 <= key_value <= 7:
            raise UserInputError("调号必须在 -7（七个降号）到 7（七个升号）之间。")
    if min_note not in {"1/8", "1/16", "1/32"}:
        raise UserInputError("最小音符只能是 1/8、1/16 或 1/32。")
    if float(pickup_beats) < 0:
        raise UserInputError("弱起拍长度不能小于 0。")
    if model is None:
        _, _, model = _load_backend()
    return _construct_model(
        model,
        {
            "bpm": None if bpm is None else float(bpm),
            "time_signature": meter_value,
            "numerator": numerator,
            "denominator": denominator,
            "key_signature": key_value,
            "min_note": min_note,
            "auto_latency": bool(auto_latency),
            "trim_common_leading": bool(trim_common_leading),
            "preserve_arpeggios": bool(preserve_arpeggios),
            "detect_tuplets": bool(detect_tuplets),
            "detect_grace_notes": bool(detect_grace_notes),
            "detect_swing": bool(detect_swing),
            "detect_ornaments": bool(detect_ornaments),
            "pickup": float(pickup_beats) > 0,
            "pickup_beats": float(pickup_beats),
            "title": title.strip(),
        },
        _SETTINGS_ALIASES,
    )


def _staff(value: str) -> str:
    result = STAFF_NAMES.get(value.strip().lower())
    if result is None:
        raise UserInputError(f"未知谱表：{value}")
    return result


def _parse_bpm(value: str) -> float | None:
    if value.strip().lower() in {"auto", "自动", "none", ""}:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise UserInputError("--bpm 应为 auto 或一个数值。") from exc


def _parse_detailed_input(token: str, defaults: argparse.Namespace) -> dict[str, Any]:
    """Parse PATH|STAFF|OCTAVES|SEMITONES|VELOCITY|OFFSET."""

    fields = token.split("|")
    if not fields[0].strip() or len(fields) > 6:
        raise UserInputError(
            "--input 格式为 PATH|STAFF|OCTAVES|SEMITONES|VELOCITY|OFFSET。"
        )
    converters: tuple[Callable[[str], Any], ...] = (
        str,
        _staff,
        int,
        int,
        int,
        float,
    )
    fallback = (
        "",
        defaults.default_staff,
        defaults.transpose_octaves,
        defaults.transpose_semitones,
        defaults.velocity_min,
        defaults.offset_beats,
    )
    parsed: list[Any] = []
    for index, converter in enumerate(converters):
        raw = fields[index].strip() if index < len(fields) else ""
        try:
            parsed.append(converter(raw) if raw else fallback[index])
        except (ValueError, UserInputError) as exc:
            raise UserInputError(f"--input 的第 {index + 1} 项不合法：{raw}") from exc
    return dict(
        path=parsed[0],
        staff=parsed[1],
        transpose_octaves=parsed[2],
        transpose_semitones=parsed[3],
        velocity_min=parsed[4],
        offset_beats=parsed[5],
    )


def _check_input_path(path: Path) -> None:
    if not path.is_file():
        raise UserInputError(f"找不到 MIDI 文件：{path}")
    if path.suffix.lower() not in {".mid", ".midi"}:
        raise UserInputError(f"输入不是 .mid/.midi 文件：{path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="midi-to-mscz",
        description="把扒谱 MIDI 量化、合并并导出为适合 MuseScore 编辑的钢琴谱。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  midi-to-mscz --treble vocal.mid --bass piano.mid -o score.mscz\n"
            "  midi-to-mscz --input \"vocal.mid|treble|1|0|18|0\" "
            "--input \"piano.mid|bass|-1|0|12|0.25\" -o score.mscz\n\n"
            "--input 各项依次为：路径|谱表|八度|半音|最低力度|偏移拍；后五项可省略。"
        ),
    )
    parser.add_argument("paths", nargs="*", metavar="MIDI", help="输入 MIDI（默认放入高音谱表）")
    parser.add_argument("--input", action="append", default=[], metavar="SPEC", help="添加带独立设置的 MIDI")
    parser.add_argument("--treble", action="append", default=[], metavar="PATH", help="添加到高音谱表，可重复（最多 4 个 MIDI）")
    parser.add_argument("--bass", action="append", default=[], metavar="PATH", help="添加到低音谱表，可重复（最多 4 个 MIDI）")
    parser.add_argument("-o", "--output", metavar="MSCZ", help="输出 .mscz 路径；默认使用第一个输入文件名")

    inputs = parser.add_argument_group("每个输入的默认设置")
    inputs.add_argument("--default-staff", choices=("treble", "bass"), default="treble", help="位置参数和简写输入的默认谱表")
    inputs.add_argument("--transpose-octaves", type=int, default=0, metavar="N", help="整体升降八度，-3..3")
    inputs.add_argument("--transpose-semitones", type=int, default=0, metavar="N", help="额外半音移调，-11..11")
    inputs.add_argument("--velocity-min", type=int, default=1, metavar="N", help="删除力度低于此值的音，1..127")
    inputs.add_argument("--offset-beats", type=float, default=0.0, metavar="BEATS", help="整体拍偏移，可为小数或负数")

    score = parser.add_argument_group("全局记谱设置")
    score.add_argument("--bpm", default="auto", metavar="AUTO|N", help="速度；auto 表示自动读取/估算")
    score.add_argument("--time-signature", default="auto", metavar="AUTO|N/D", help="拍号；auto 会保留 MIDI 中的临时变拍")
    score.add_argument("--key-signature", default="auto", metavar="AUTO|-7..7", help="调号；auto 根据实际音符识别，数字仅用于手动覆盖")
    score.add_argument("--min-note", choices=("1/8", "1/16", "1/32"), default="1/16", help="允许的最小音符")
    score.add_argument("--auto-latency", action=argparse.BooleanOptionalAction, default=True, help="自动校正统一演奏延迟")
    score.add_argument("--trim-common-leading", action=argparse.BooleanOptionalAction, default=True, help="裁剪所有轨道共有的前导空白")
    score.add_argument("--arpeggios", action=argparse.BooleanOptionalAction, default=True, help="识别并保留琶音，不压成同时发声")
    score.add_argument("--tuplets", action=argparse.BooleanOptionalAction, default=True, help="识别 2:3、3:2、5:4、7:4/8、9:8、11/12/13:8 等比例连音")
    score.add_argument("--grace-notes", action=argparse.BooleanOptionalAction, default=True, help="识别倚音和倚音串")
    score.add_argument("--swing", action=argparse.BooleanOptionalAction, default=True, help="识别区域性的摇摆 / Shuffle 节奏")
    score.add_argument("--ornaments", action=argparse.BooleanOptionalAction, default=True, help="保护颤音、波音等快速装饰音型")
    score.add_argument("--pickup-beats", type=float, default=0.0, metavar="BEATS", help="手动弱起长度（四分音符拍）；0 表示允许自动识别")
    score.add_argument("--title", default="", help="乐谱标题")
    parser.add_argument("--quiet", action="store_true", help="不显示转换过程，只输出结果")
    parser.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    return parser


def _input_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    common = dict(
        transpose_octaves=args.transpose_octaves,
        transpose_semitones=args.transpose_semitones,
        velocity_min=args.velocity_min,
        offset_beats=args.offset_beats,
    )
    rows = [dict(path=path, staff=args.default_staff, **common) for path in args.paths]
    rows.extend(dict(path=path, staff="treble", **common) for path in args.treble)
    rows.extend(dict(path=path, staff="bass", **common) for path in args.bass)
    rows.extend(_parse_detailed_input(token, args) for token in args.input)
    if not rows:
        raise UserInputError("请至少指定一个 MIDI 文件（MIDI、--treble、--bass 或 --input）。")
    for staff, display_name in (("treble", "高音"), ("bass", "低音")):
        count = sum(row["staff"] == staff for row in rows)
        if count > 4:
            raise UserInputError(
                f"{display_name}谱表分配了 {count} 个 MIDI；"
                "MuseScore 每个谱表最多四个独立声部。"
            )
    return rows


def _serialisable_report(report: Any) -> Mapping[str, Any]:
    if report is None:
        return {}
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        return dataclasses.asdict(report)
    if isinstance(report, Mapping):
        return report
    try:
        return vars(report)
    except TypeError:
        return {"result": report}


def format_report(report: Any) -> str:
    """Turn a pipeline report into a short, stable Chinese summary."""

    to_text = getattr(report, "to_text", None)
    if callable(to_text):
        rendered = str(to_text()).strip()
        if rendered:
            return rendered
    data = dict(_serialisable_report(report))
    # Aggregate properties are intentionally properties in the public model,
    # so dataclasses.asdict() does not include them.  Add them when available.
    for attribute in (
        "notes_seen",
        "paired_notes",
        "notes_kept",
        "velocity_filtered",
        "invalid_duration",
        "transposed_notes",
        "out_of_midi_range",
        "out_of_piano_range",
        "pedal_tails_trimmed",
    ):
        if attribute not in data and hasattr(report, attribute):
            data[attribute] = getattr(report, attribute)
    labels = {
        "output_path": "输出文件",
        "input_files": "输入文件",
        "notes_seen": "原始音符",
        "paired_notes": "成功配对",
        "input_notes": "原始音符",
        "total_notes": "原始音符",
        "notes_kept": "保留音符",
        "kept_notes": "保留音符",
        "output_notes": "输出音符",
        "final_notes": "输出音符",
        "quantized_chords": "记谱事件",
        "velocity_filtered": "删除弱音",
        "removed_notes": "删除弱音",
        "filtered_notes": "删除弱音",
        "invalid_duration": "删除无效时值",
        "transposed_notes": "移调音符",
        "out_of_midi_range": "超出 MIDI 音域",
        "out_of_piano_range": "超出钢琴音域",
        "quantized_notes": "量化音符",
        "arpeggios": "识别琶音",
        "arpeggio_count": "识别琶音",
        "tuplets": "识别连音",
        "tuplet_count": "识别连音",
        "grace_count": "识别倚音",
        "ornament_count": "识别快速装饰音",
        "pedal_tails_trimmed": "截短踏板/混响尾音",
        "swing_detected": "检测到 Swing",
        "low_confidence_measures": "人工复核小节",
        "tempo_bpm": "输出速度（BPM）",
        "time_signature": "识别拍号",
        "key_name": "识别调号",
        "key_mode": "调式",
        "tempo_change_count": "速度变化",
        "time_signature_change_count": "拍号变化",
        "key_change_count": "调号变化",
        "pickup_beats": "弱起长度（四分音符拍）",
        "triplet_count": "识别三连音",
        "warnings": "提示",
    }
    lines: list[str] = []
    used: set[str] = set()
    for key, label in labels.items():
        if key not in data or key in used or data[key] in (None, "", [], ()):
            continue
        value = data[key]
        if key == "key_mode":
            value = {"major": "大调", "minor": "小调"}.get(str(value), value)
        elif key == "time_signature" and isinstance(value, (list, tuple)) and len(value) == 2:
            value = f"{value[0]}/{value[1]}"
        if isinstance(value, (list, tuple)):
            value = "；".join(str(item) for item in value)
        lines.append(f"{label}：{value}")
        used.add(key)
    if not lines and data:
        for key, value in data.items():
            if value not in (None, "", [], ()):
                lines.append(f"{key}：{value}")
    return "\n".join(lines) if lines else "转换已完成。"


def run_conversion(args: argparse.Namespace) -> Any:
    convert, input_model, settings_model = _load_backend()
    rows = _input_rows(args)
    specs = []
    for row in rows:
        path = Path(row["path"]).expanduser().resolve()
        _check_input_path(path)
        specs.append(make_input_spec(path=path, model=input_model, **{k: v for k, v in row.items() if k != "path"}))

    first_path = Path(rows[0]["path"]).expanduser()
    output = Path(args.output).expanduser() if args.output else first_path.with_name(f"{first_path.stem}_标准化.mscz")
    if output.suffix.lower() != ".mscz":
        output = output.with_suffix(".mscz")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    settings = make_settings(
        bpm=_parse_bpm(args.bpm),
        time_signature=args.time_signature,
        key_signature=args.key_signature,
        min_note=args.min_note,
        auto_latency=args.auto_latency,
        trim_common_leading=args.trim_common_leading,
        preserve_arpeggios=args.arpeggios,
        detect_tuplets=args.tuplets,
        detect_grace_notes=args.grace_notes,
        detect_swing=args.swing,
        detect_ornaments=args.ornaments,
        pickup_beats=args.pickup_beats,
        title=args.title,
        model=settings_model,
    )

    def progress(message: str) -> None:
        if not args.quiet:
            print(f"[转换] {message}", file=sys.stderr, flush=True)

    return convert(specs, output, settings, progress)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gui:
        from .webui import main as gui_main

        return gui_main()
    try:
        report = run_conversion(args)
    except (UserInputError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消转换。", file=sys.stderr)
        return 130
    except Exception as exc:  # keep third-party errors friendly by default
        print(f"转换失败：{exc}", file=sys.stderr)
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
