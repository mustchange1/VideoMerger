from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import VideoMergerError
from .hardware import available_encoders
from .paths import ensure_project_directories, locate_ffmpeg, project_root
from .platform_utils import hidden_process_flags, safe_subprocess_env


@dataclass(slots=True)
class DiagnosticItem:
    name: str
    ok: bool
    detail: str


def _version(binary: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(binary), "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=hidden_process_flags(), env=safe_subprocess_env(),
        )
        first = (result.stdout or result.stderr).splitlines()[0]
        return result.returncode == 0, first
    except Exception as exc:  # diagnostics must report rather than crash
        return False, str(exc)


def run_diagnostics(test_encoders: bool = False) -> list[DiagnosticItem]:
    items: list[DiagnosticItem] = []
    items.append(DiagnosticItem("Python", sys.version_info >= (3, 10), f"{platform.python_version()} ({sys.executable})"))
    try:
        from PySide6 import __version__ as pyside_version
        items.append(DiagnosticItem("GUI / PySide6", True, pyside_version))
    except Exception as exc:
        items.append(DiagnosticItem("GUI / PySide6", False, str(exc)))
    try:
        import faster_whisper
        items.append(DiagnosticItem(
            "Local Word Alignment", True,
            f"faster-whisper {faster_whisper.__version__} · local word timestamps"
        ))
    except Exception as exc:
        items.append(DiagnosticItem("Local Word Alignment", False, str(exc)))
    try:
        import fontTools
        from .font_manager import bundled_fonts_dir
        font_dir = bundled_fonts_dir()
        font_ok = all((font_dir / name).is_file() for name in (
            "NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "OFL.txt"
        ))
        items.append(DiagnosticItem(
            "Font Metrics / Legal Fallback", font_ok,
            f"fontTools {fontTools.__version__} · bundled SIL-OFL Noto Sans: {font_dir}"
        ))
    except Exception as exc:
        items.append(DiagnosticItem("Font Metrics / Legal Fallback", False, str(exc)))
    try:
        ensure_project_directories()
        ffmpeg, ffprobe = locate_ffmpeg()
        ok, detail = _version(ffmpeg)
        items.append(DiagnosticItem("FFmpeg", ok, detail))
        ok_probe, detail_probe = _version(ffprobe)
        items.append(DiagnosticItem("FFprobe", ok_probe, detail_probe))
        if ok and ok_probe:
            from .engine import VideoMergerEngine
            preflight_log: list[str] = []
            VideoMergerEngine(ffmpeg, ffprobe).preflight(preflight_log.append)
            items.append(DiagnosticItem(
                "FFmpeg -filter_complex Test", True,
                f"OK · executable: {ffmpeg}"
            ))
        if test_encoders and ok:
            encoders = available_encoders(str(ffmpeg))
            active = [name for name, available in encoders.items() if available]
            items.append(DiagnosticItem("H.264 Encoder", bool(active), ", ".join(active) or "keiner"))
    except Exception as exc:
        items.append(DiagnosticItem("FFmpeg / FFprobe", False, str(exc)))
    for name in ("output", "temp", "logs"):
        directory = project_root() / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, delete=True):
                pass
            items.append(DiagnosticItem(f"Schreibzugriff {name}", True, str(directory)))
        except Exception as exc:
            items.append(DiagnosticItem(f"Schreibzugriff {name}", False, str(exc)))
    return items


def run_project_diagnostics(settings, media=None) -> list[DiagnosticItem]:
    """Report assigned Stage-1/Stage-2 roles without performing a render."""
    from .alignment import script_word_spans
    from .project_assets import optional_path, probe_audio, read_script

    items: list[DiagnosticItem] = []
    try:
        _ffmpeg, ffprobe = locate_ffmpeg()
    except Exception as exc:
        return [DiagnosticItem("Project Assets", False, str(exc))]
    from .main_project import global_script_path, script_paths, voiceover_paths, voiceover_pause
    voices = voiceover_paths(settings)
    if voices:
        total = 0.0
        failed = None
        for voice in voices:
            try:
                info = probe_audio(ffprobe, voice)
                total += info.duration
            except Exception as exc:
                failed = exc
                break
        total += voiceover_pause(settings) * max(0, len(voices) - 1)
        items.append(DiagnosticItem(
            "Voiceover" if len(voices) == 1 else f"Voiceover ({len(voices)} units)",
            failed is None,
            (
                f"{total:.3f} s concatenated · " + " → ".join(path.name for path in voices)
                if failed is None else f"fehlgeschlagen: {failed}"
            ),
        ))
    else:
        items.append(DiagnosticItem("Voiceover", True, "optional · not assigned"))
    scripts = script_paths(settings)
    if str(settings.script_mode).casefold() in {"matched", "individual"} and voices and len(scripts) < len(voices):
        items.append(DiagnosticItem(
            "Script Matching", False,
            f"{len(scripts)}/{len(voices)} Skripte zugewiesen – fehlende Skripte blockieren den Export.",
        ))
    music = optional_path(settings.music_path)
    if music:
        try:
            info = probe_audio(ffprobe, music)
            items.append(DiagnosticItem(
                "Background Music", True,
                f"{info.duration:.3f} s · {info.sample_rate} Hz · loops only inside Main Video"
            ))
        except Exception as exc:
            items.append(DiagnosticItem("Background Music", False, str(exc)))
    else:
        items.append(DiagnosticItem("Background Music", True, "optional · not assigned (Long-Form)"))
    # Shorts use their own strictly separate track; report it independently so
    # an unreadable Shorts file is visible before the export starts.
    short_music = optional_path(getattr(settings, "short_music_path", ""))
    if short_music:
        try:
            info = probe_audio(ffprobe, short_music)
            items.append(DiagnosticItem(
                "Background Music (Shorts)", True,
                f"{info.duration:.3f} s · {info.sample_rate} Hz · loops only inside YouTube Shorts"
            ))
        except Exception as exc:
            items.append(DiagnosticItem("Background Music (Shorts)", False, str(exc)))
    else:
        items.append(DiagnosticItem("Background Music (Shorts)", True, "optional · not assigned (Shorts stay without music)"))
    script = global_script_path(settings) if str(settings.script_mode).casefold() not in {"matched", "individual"} else (
        optional_path(settings.script_path)
    )
    if script:
        try:
            text = read_script(script)
            items.append(DiagnosticItem(
                "Script", True,
                f"{len(script_word_spans(text))} words · selected language {settings.subtitle_language}"
            ))
        except Exception as exc:
            items.append(DiagnosticItem("Script", False, str(exc)))
    elif settings.subtitle_enabled:
        items.append(DiagnosticItem("Script", False, "required while subtitles are enabled"))
    else:
        items.append(DiagnosticItem("Script", True, "not required while subtitles are disabled"))
    alignment_detail = (
        f"pending render · faster-whisper/{settings.subtitle_model} word timestamps + authoritative script mapping"
        if settings.subtitle_enabled else "disabled"
    )
    items.append(DiagnosticItem("Subtitle Alignment", True, alignment_detail))
    try:
        from .font_manager import font_status
        from .subtitles import normalize_subtitle_animation
        items.append(DiagnosticItem(
            "Subtitle Presentation", True,
            # The effective (migrated) animations are reported, so a deprecated
            # Outline Highlight or a Shorts Word Highlight from an old project is
            # visible as the clean animation that will actually render.
            f"style={settings.subtitle_style} · "
            f"animation={normalize_subtitle_animation(settings.subtitle_animation, 'long')} · "
            f"font={font_status(settings.subtitle_font)} · position={settings.subtitle_position} · "
            f"debug={'ON' if settings.subtitle_debug_overlay else 'OFF'} · "
            f"Shorts animation="
            f"{normalize_subtitle_animation(getattr(settings, 'short_subtitle_animation', ''), 'short')}"
        ))
    except Exception as exc:
        items.append(DiagnosticItem("Subtitle Presentation", False, str(exc)))
    try:
        from .opening_effects import OPENING_EFFECT_LABELS, normalize_opening_effect
        from .youtube_outputs import visual_section_seconds
        items.append(DiagnosticItem(
            "Visual Timeline Sections", True,
            f"Long-Form intro "
            f"{visual_section_seconds(getattr(settings, 'long_form_intro_seconds', 0.0), label='Long-Form Intro'):.3f} s · "
            f"Long-Form outro "
            f"{visual_section_seconds(getattr(settings, 'long_form_outro_seconds', 0.0), label='Long-Form Outro'):.3f} s · "
            f"Short intro "
            f"{visual_section_seconds(getattr(settings, 'short_intro_seconds', 0.0), label='Short Intro'):.3f} s · "
            f"Short outro "
            f"{visual_section_seconds(getattr(settings, 'short_outro_seconds', 0.0), label='Short Outro'):.3f} s · "
            f"opening effect "
            f"{OPENING_EFFECT_LABELS[normalize_opening_effect(getattr(settings, 'opening_effect', ''))]}"
        ))
    except VideoMergerError as exc:
        # An invalid saved section length is reported, never raised: the
        # diagnostics view must stay readable for a broken project.
        items.append(DiagnosticItem("Visual Timeline Sections", False, str(exc)))
    try:
        from .models import (
            LONG_FORM_MUSIC_VOLUME,
            LONG_FORM_TRANSITION_DURATION,
            SHORTS_MUSIC_VOLUME,
            SHORTS_TRANSITION_DURATION,
        )
        from .transition_effects import transition_label
        from .youtube_outputs import (
            output_music_volume,
            output_transition_duration,
            output_transition_type,
        )
        # The values each output really renders with: Long-Form and Shorts own
        # their music volume and transition, and the resolved values make an old
        # project's migrated shared setting visible instead of ambiguous.
        long_volume = output_music_volume(
            settings, getattr(settings, "long_form_music_volume", None),
            label="Long-Form Music Volume", default=LONG_FORM_MUSIC_VOLUME,
        )
        short_volume = output_music_volume(
            settings, getattr(settings, "shorts_music_volume", None),
            label="Shorts Music Volume", default=SHORTS_MUSIC_VOLUME,
        )
        long_type = output_transition_type(
            settings, getattr(settings, "long_form_transition_type", ""),
            label="Long-Form Transition",
        )
        short_type = output_transition_type(
            settings, getattr(settings, "shorts_transition_type", ""),
            label="Shorts Transition",
        )
        long_duration = output_transition_duration(
            settings, getattr(settings, "long_form_transition_duration", None),
            label="Long-Form Transition Duration", default=LONG_FORM_TRANSITION_DURATION,
        )
        short_duration = output_transition_duration(
            settings, getattr(settings, "shorts_transition_duration", None),
            label="Shorts Transition Duration", default=SHORTS_TRANSITION_DURATION,
        )
        items.append(DiagnosticItem(
            "Output Music & Transitions", True,
            f"Long-Form music {long_volume} % · Shorts music {short_volume} % · "
            "a selected track plays 0.000 s → video end (visual intro and outro "
            f"included) · Long-Form transition {transition_label(long_type)} / "
            f"{long_duration:.3f} s · Shorts transition {transition_label(short_type)} / "
            f"{short_duration:.3f} s"
        ))
    except VideoMergerError as exc:
        items.append(DiagnosticItem("Output Music & Transitions", False, str(exc)))
    if media:
        visual = sum(item.source_duration or item.duration for item in media)
        items.append(DiagnosticItem(
            "Video Material", True,
            f"{visual:.3f} s · {len(media)} clips · active GUI order"
        ))
    main_path = optional_path(settings.main_video_path)
    intro_path = optional_path(settings.intro_path)
    outro_path = optional_path(settings.outro_path)
    items.append(DiagnosticItem("Main Video for Stage 2", not main_path or main_path.is_file(), str(main_path or "not assigned")))
    items.append(DiagnosticItem("Intro", not intro_path or intro_path.is_file(), str(intro_path or "optional · not assigned")))
    items.append(DiagnosticItem("Outro", not outro_path or outro_path.is_file(), str(outro_path or "optional · not assigned")))
    return items
