from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import VideoMergerError
from .font_manager import resolve_font
from .models import AlignmentResult, WordTiming
from .subtitle_presets import SubtitlePreset, get_preset

ANIMATION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("type_reveal", "Type Reveal"),
    ("color_change", "Color Change"),
    ("word_highlight", "Word Highlight"),
    ("outline_highlight", "Outline Highlight"),
    ("static_phrase", "Static Phrase"),
)
ANIMATION_KEYS = {key for key, _label in ANIMATION_OPTIONS}


@dataclass(slots=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str
    words: list[WordTiming]
    line_break_after: int | None = None  # number of words on line one
    line_count: int = 1


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _font_size(width: int, height: int, preset: SubtitlePreset) -> int:
    return max(11, round(min(width, height) * preset.font_ratio))


def _layout_words(
    words: list[WordTiming], font_key: str, size: int, available_width: float,
) -> tuple[bool, int | None]:
    font = resolve_font(font_key)
    tokens = [word.text for word in words]
    full = " ".join(tokens)
    if font.text_width(full, size) <= available_width:
        return True, None
    best: tuple[float, int] | None = None
    for split in range(1, len(tokens)):
        left = font.text_width(" ".join(tokens[:split]), size)
        right = font.text_width(" ".join(tokens[split:]), size)
        if left <= available_width and right <= available_width:
            # Prefer balanced measured width, then a visually fuller first line.
            score = abs(left - right) + (available_width - max(left, right)) * .03
            if best is None or score < best[0]:
                best = score, split
    return (best is not None, best[1] if best else None)


def build_cues(
    script: str,
    alignment: AlignmentResult,
    preset_key: str,
    program_end: float | None = None,
    *,
    width: int = 1920,
    height: int = 1080,
    font_key: str = "modern_sans_bold",
) -> list[SubtitleCue]:
    """Build phrase-oriented cues solely on the canonical acoustic word list.

    Timing rate never participates in grouping. Punctuation, phrase boundaries,
    selected-font advances, available width and visual line balance do.
    """
    preset = get_preset(preset_key)
    words = alignment.words
    # A complete mismatch is valid output: the audio still renders, while the
    # subtitle track simply contains no cues. Partial matches are represented
    # by gaps in ``words`` and resume at later reliable acoustic matches.
    if not words:
        return []
    size = _font_size(width, height, preset)
    width_ratio = .86 if preset.collection == "long" else .90
    available_width = max(40.0, width * width_ratio)
    # Caption grouping and later long-form rebalancing must share this guard;
    # otherwise a repair merge could accidentally cross an inter-unit pause.
    hard_breaks = sorted(float(value) for value in getattr(alignment, "hard_breaks", []))

    def fits(group: list[WordTiming]) -> bool:
        if len(group) > preset.max_words:
            return False
        if any(
            left.start < boundary <= right.start
            for left, right in zip(group, group[1:])
            for boundary in hard_breaks
        ):
            return False
        # Only words present in the canonical alignment may reach a cue. Using
        # the enclosing script slice here would re-introduce unmatched text
        # between two valid anchors.
        exact = _clean_text(" ".join(word.text for word in group))
        # max_chars is a guard only; real selected-font geometry is authoritative.
        if len(exact) > max(preset.max_chars, 24) * 2:
            return False
        return _layout_words(group, font_key, size, available_width)[0]

    groups: list[list[WordTiming]] = []
    current: list[WordTiming] = []
    # A multi-voiceover pause is an actual quiet interval, not an end pad.
    # Never keep a caption alive across a substantial acoustic gap: doing so
    # would display the previous unit's subtitle while the next unit is silent.
    for word in words:
        candidate = current + [word]
        sentence_end = bool(re.search(r"[.!?…][\"”’)]?$", word.text))
        clause_end = bool(re.search(r"[,;:][\"”’)]?$", word.text))
        gap_break = bool(
            current and any(current[-1].start < boundary <= word.start for boundary in hard_breaks)
        )
        if current and (gap_break or not fits(candidate)):
            groups.append(current)
            current = [word]
        else:
            current = candidate
        if sentence_end:
            groups.append(current)
            current = []
        elif clause_end and len(current) >= max(4, min(7, preset.max_words - 2)):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    # Long-form defaults must not emit isolated words where neighboring words
    # exist. Repair boundary singletons without inventing or retiming words.
    if preset.collection == "long" and len(words) > 1:
        index = 0
        while index < len(groups):
            if len(groups[index]) != 1:
                index += 1
                continue
            merged = False
            if index > 0 and fits(groups[index - 1] + groups[index]):
                groups[index - 1].extend(groups.pop(index))
                merged = True
                index = max(0, index - 1)
            elif index + 1 < len(groups) and fits(groups[index] + groups[index + 1]):
                groups[index].extend(groups.pop(index + 1))
                merged = True
            if not merged and index > 0 and len(groups[index - 1]) >= 3:
                # Rebalance 10+1 style boundaries to 9+2 without changing any
                # acoustic timestamps or crossing the two-line geometry limit.
                moved = groups[index - 1].pop()
                groups[index].insert(0, moved)
                merged = fits(groups[index - 1]) and fits(groups[index])
                if not merged:
                    groups[index].pop(0)
                    groups[index - 1].append(moved)
            if not merged and index + 1 < len(groups) and len(groups[index + 1]) >= 3:
                # Only the resulting groups must fit; the two original groups
                # need not fit together. The ``fits`` guard still prevents
                # crossing a hard acoustic boundary.
                moved = groups[index + 1].pop(0)
                groups[index].append(moved)
                merged = fits(groups[index]) and fits(groups[index + 1])
                if not merged:
                    groups[index].pop()
                    groups[index + 1].insert(0, moved)
            if not merged:
                # A single extreme token may be unavoidable; retain it rather
                # than losing or fabricating authoritative script text.
                index += 1

        # 1.3.0 Long-Form YouTube readability: preferably 1–2 caption lines
        # showing natural phrases — avoid constant one/two-word captions. Very
        # small groups are merged into the better-fitting neighbor while the
        # measured two-line geometry and the word-budget guard still hold; if
        # a direct merge does not fit, one word is rebalanced from the larger
        # neighbor instead. Word-level timing is never changed (cue boundaries
        # follow the canonical acoustic word timeline).
        index = 0
        while index < len(groups):
            if len(groups[index]) >= 3 or len(groups) < 2:
                index += 1
                continue
            merged = False
            if index > 0 and fits(groups[index - 1] + groups[index]):
                groups[index - 1].extend(groups.pop(index))
                merged = True
            elif index + 1 < len(groups) and fits(groups[index] + groups[index + 1]):
                groups[index].extend(groups.pop(index + 1))
                merged = True
            elif index > 0 and len(groups[index - 1]) >= 4:
                moved = groups[index - 1].pop()
                groups[index].insert(0, moved)
                merged = fits(groups[index - 1]) and fits(groups[index])
                if not merged:
                    groups[index].pop(0)
                    groups[index - 1].append(moved)
            elif index + 1 < len(groups) and len(groups[index + 1]) >= 4:
                moved = groups[index + 1].pop(0)
                groups[index].append(moved)
                merged = fits(groups[index]) and fits(groups[index + 1])
                if not merged:
                    groups[index].pop()
                    groups[index + 1].insert(0, moved)
            if not merged:
                index += 1

    cues: list[SubtitleCue] = []
    for index, group in enumerate(groups, start=1):
        valid, split = _layout_words(group, font_key, size, available_width)
        if not valid:
            raise VideoMergerError(f"Subtitle cue {index} cannot fit inside two measured lines.")
        start = group[0].start
        next_start = groups[index][0].start if index < len(groups) else None
        desired_end = group[-1].end + (0.18 if preset.collection == "long" else 0.10)
        end = min(desired_end, next_start - 0.01) if next_start is not None else desired_end
        # A hard boundary is an acoustic silence boundary, so even a generous
        # long-form display allowance must end before it.
        boundary_after_group = next(
            (boundary for boundary in hard_breaks
             if group[-1].start < boundary and (next_start is None or boundary <= next_start)),
            None,
        )
        if boundary_after_group is not None:
            end = min(end, boundary_after_group - 0.01)
        if program_end is not None:
            end = min(end, float(program_end))
        end = max(start + 0.02, end)
        text = _clean_text(" ".join(word.text for word in group))
        cues.append(SubtitleCue(index, start, end, text, group, split, 2 if split else 1))
    validate_cues(cues, len(words))
    return cues


def validate_cues(cues: list[SubtitleCue], expected_word_count: int | None = None) -> None:
    previous_end = -1.0
    word_count = 0
    for cue in cues:
        if cue.start < 0 or cue.end <= cue.start:
            raise VideoMergerError(f"Ungültige Untertitelzeit bei Cue {cue.index}.")
        if cue.start < previous_end - 0.001:
            raise VideoMergerError(f"Überlappende Untertitel bei Cue {cue.index}.")
        if not cue.text.strip():
            raise VideoMergerError(f"Leerer Untertitel bei Cue {cue.index}.")
        if cue.line_count not in {1, 2}:
            raise VideoMergerError(f"Untertitel Cue {cue.index} überschreitet zwei Zeilen.")
        previous_end = cue.end
        word_count += len(cue.words)
    if expected_word_count is not None and word_count != expected_word_count:
        raise VideoMergerError(f"Untertitelvalidierung: {expected_word_count - word_count} Skriptwörter fehlen.")


def _srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _vtt_time(seconds: float) -> str:
    return _srt_time(seconds).replace(",", ".")


def write_srt(cues: list[SubtitleCue], path: Path) -> None:
    validate_cues(cues)
    body = "\n".join(
        f"{c.index}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{c.text}\n" for c in cues
    )
    # An empty, valid SRT is useful when all script text is unmatched: the
    # audio still renders and the sidecar explicitly contains no captions.
    path.write_text(body if body else "\n", encoding="utf-8", newline="\n")


def write_vtt(cues: list[SubtitleCue], path: Path) -> None:
    validate_cues(cues)
    path.write_text(
        "WEBVTT\n\n" + "\n".join(
            f"{_vtt_time(c.start)} --> {_vtt_time(c.end)}\n{c.text}\n" for c in cues
        ),
        encoding="utf-8", newline="\n",
    )


def validate_subtitle_file(path: Path, kind: str) -> None:
    text = path.read_text(encoding="utf-8-sig")
    if kind == "vtt" and not text.startswith("WEBVTT"):
        raise VideoMergerError("VTT-Validierung fehlgeschlagen: WEBVTT-Kopf fehlt.")
    if text.count(" --> ") <= 0:
        if (kind == "srt" and not text.strip()) or (kind == "vtt" and text.strip() == "WEBVTT"):
            return
        raise VideoMergerError(f"{kind.upper()}-Validierung fehlgeschlagen: keine Zeitstempel.")


def write_canonical_timeline(script: str, alignment: AlignmentResult, cues: list[SubtitleCue], path: Path) -> None:
    validate_cues(cues, len(alignment.words))
    payload = {
        "schema": 2,
        "authoritative_script": script,
        "language": alignment.language,
        "method": alignment.method,
        "compatibility": alignment.compatibility,
        "average_confidence": alignment.average_confidence,
        "hard_breaks": alignment.hard_breaks,
        "words": [asdict(word) for word in alignment.words],
        "cues": [{
            "index": cue.index, "start": cue.start, "end": cue.end, "text": cue.text,
            "word_indexes": [alignment.words.index(word) for word in cue.words],
            "line_break_after": cue.line_break_after, "line_count": cue.line_count,
        } for cue in cues],
        "verification_word_indexes": (
            [0, len(alignment.words) // 2, len(alignment.words) - 1]
            if alignment.words else []
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _position(position: str, width: int, height: int, collection: str) -> tuple[int, int]:
    # New defaults are explicit labels; old project values remain valid.
    aliases = {"Bottom Center": "Bottom", "Center": "Middle"}
    normalized = aliases.get(str(position), str(position))
    pos = normalized if normalized in {"Bottom", "Medium-Low", "Middle", "Top"} else (
        "Medium-Low" if collection == "short" else "Middle"
    )
    if pos == "Top":
        return 8, round(height * .08)
    if pos == "Middle":
        return 5, 0
    if pos == "Medium-Low":
        return 2, round(height * (.22 if height > width else .16))
    return 2, round(height * .07)


def _render_phrase(cue: SubtitleCue, active: int, animation: str, preset: SubtitlePreset, outline: float) -> str:
    pieces: list[str] = []
    accent = preset.accent + ("" if preset.accent.endswith("&") else "&")
    white = "&H00F7F7F7&"
    dark = "&H00101010&"
    for index, word in enumerate(cue.words):
        token = _ass_escape(word.text)
        if animation == "type_reveal":
            tag = r"{\1a&H00&\3a&H00&}" if index <= active else r"{\1a&HFF&\3a&HFF&}"
            rendered = tag + token
        elif animation == "color_change":
            rendered = (r"{\c" + accent + "}" if index <= active else r"{\c" + white + "}") + token
        elif animation == "outline_highlight":
            if index == active:
                rendered = r"{\3c" + accent + rf"\bord{outline * 1.8:.1f}" + "}" + token
            else:
                rendered = r"{\3c" + dark + rf"\bord{outline:.1f}" + "}" + token
        else:  # word_highlight and static phrase
            rendered = (r"{\c" + accent + "}" if animation == "word_highlight" and index == active else r"{\c" + white + "}") + token
        pieces.append(rendered)
    if cue.line_break_after:
        left = " ".join(pieces[:cue.line_break_after])
        right = " ".join(pieces[cue.line_break_after:])
        return left + r"\N" + right
    return " ".join(pieces)


def write_ass(
    script: str,
    cues: list[SubtitleCue],
    path: Path,
    preset_key: str,
    position: str,
    width: int,
    height: int,
    *,
    animation: str | None = None,
    font_key: str | None = None,
    debug_overlay: bool = False,
) -> None:
    preset = get_preset(preset_key)
    # None preserves 1.2.1's explicit API/Arial regression behavior. The 1.2.2
    # workflow always passes its selected logical font key.
    resolved_font = resolve_font(font_key) if font_key else None
    family = resolved_font.family if resolved_font else "Arial"
    animation = animation if animation in ANIMATION_KEYS else ("type_reveal" if preset.progressive else "word_highlight")
    basis = min(width, height)
    font_size = _font_size(width, height, preset)
    outline = max(1.0, round(basis * preset.outline_ratio, 1))
    alignment, margin_v = _position(position, width, height, preset.collection)
    margin_h = round(width * (.07 if preset.collection == "long" else .055))
    border_style = 3 if preset.box else 1
    back = "&H78000000" if preset.box else "&H00000000"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{family},{font_size},&H00F7F7F7,{preset.accent},&H00101010,{back},{-1 if preset.bold or (resolved_font and resolved_font.weight == 'bold') else 0},0,0,0,100,100,0,0,{border_style},{outline},{max(.5, outline * .45):.1f},{alignment},{margin_h},{margin_h},{margin_v},1
Style: Debug,{family},{max(11, round(basis * .024))},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,3,1,0,7,{max(12, round(width*.02))},{max(12, round(width*.02))},{max(12, round(height*.02))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    for cue in cues:
        if animation == "static_phrase":
            rendered = _render_phrase(cue, -1, animation, preset, outline)
            events.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,{rendered}")
        else:
            for index, word in enumerate(cue.words):
                start = max(cue.start, word.start)
                end = cue.words[index + 1].start if index + 1 < len(cue.words) else cue.end
                end = max(start + .02, min(cue.end, end))
                rendered = _render_phrase(cue, index, animation, preset, outline)
                events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{rendered}")
        if debug_overlay:
            for word in cue.words:
                debug = (
                    f"CURRENT WORD: {_ass_escape(word.text)}  |  "
                    f"START: {word.start:09.3f}  END: {word.end:09.3f}"
                )
                events.append(f"Dialogue: 1,{_ass_time(word.start)},{_ass_time(word.end)},Debug,,0,0,0,,{debug}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig", newline="\n")
