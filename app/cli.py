from __future__ import annotations

import argparse
from pathlib import Path

from .video_merger.discovery import discover_videos
from .video_merger.engine import VideoMergerEngine
from .video_merger.main_project import MainProjectEngine
from .video_merger.models import (
    DEFAULT_TRANSITION_TYPE,
    LONG_FORM_INTRO_SECONDS,
    LONG_FORM_MUSIC_VOLUME,
    LONG_FORM_OUTRO_SECONDS,
    LONG_FORM_TRANSITION_DURATION,
    MUSIC_VOLUME_PERCENT,
    SHORT_INTRO_SECONDS,
    SHORT_OUTRO_SECONDS,
    SHORTS_MUSIC_VOLUME,
    SHORTS_TRANSITION_DURATION,
    TRANSITION_DURATION_LEGACY_DEFAULT,
    ExportSettings,
)
from .video_merger.opening_effects import (
    OPENING_EFFECT_NONE,
    OPENING_EFFECTS,
)
from .video_merger.output_manager import make_output_path
from .video_merger.paths import locate_ffmpeg
from .video_merger.project_order import GeneratedOutputStore, ProjectOrderStore
from .video_merger.subtitles import (
    DEFAULT_LONG_ANIMATION,
    DEFAULT_SHORT_ANIMATION,
    accepted_animation_values,
)
from .video_merger.video_pool import order_media_for_video_order
from .video_merger.youtube_outputs import (
    EXPORT_MODE_COMBINED,
    EXPORT_MODE_LONG_FORM,
    EXPORT_MODE_SHORTS,
    normalize_export_mode,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="VideoMerger headless export")
    parser.add_argument("--stage", choices=["basic", "main", "outro", "complete"], default="basic")
    parser.add_argument("--input", type=Path, help="legacy single input folder; use --source-folder repeatedly for multiple folders")
    parser.add_argument(
        "--source-folder", action="append", default=[], type=Path,
        help="configured video source folder; repeat for multiple folders (overrides --input)",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--export-mode", choices=[EXPORT_MODE_LONG_FORM, EXPORT_MODE_SHORTS, EXPORT_MODE_COMBINED],
        default=EXPORT_MODE_LONG_FORM,
        help="YouTube Long-Form, Shorts, or both (Shorts create one output per voiceover)",
    )
    parser.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    parser.add_argument("--resolution", default="Auto")
    parser.add_argument(
        "--transition", type=float, default=None,
        help=(
            f"shared transition duration in seconds for the basic/Main Video merge "
            f"(default {TRANSITION_DURATION_LEGACY_DEFAULT}). An explicit value is also the "
            "migration fallback for both YouTube outputs unless --long-transition / "
            "--short-transition override it"
        ),
    )
    parser.add_argument(
        "--transition-effect", default=DEFAULT_TRANSITION_TYPE,
        choices=["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
    )
    # Long-Form and Shorts own their transition completely: changing one output
    # never changes the other. Both default to Cross Dissolve / 2.0 s.
    parser.add_argument(
        "--long-transition-effect", default=None,
        choices=["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
        help=(
            f"Long-Form transition family (default {DEFAULT_TRANSITION_TYPE}); "
            "independent from --short-transition-effect"
        ),
    )
    parser.add_argument(
        "--long-transition", type=float, default=None,
        help=(
            f"Long-Form transition duration in seconds (default {LONG_FORM_TRANSITION_DURATION}); "
            "independent from --short-transition"
        ),
    )
    parser.add_argument(
        "--short-transition-effect", default=None,
        choices=["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
        help=(
            f"YouTube Shorts transition family (default {DEFAULT_TRANSITION_TYPE}); "
            "independent from --long-transition-effect"
        ),
    )
    parser.add_argument(
        "--short-transition", type=float, default=None,
        help=(
            f"YouTube Shorts transition duration in seconds (default {SHORTS_TRANSITION_DURATION}); "
            "independent from --long-transition"
        ),
    )
    parser.add_argument(
        "--transition-ease", default="ease_in_out",
        choices=["linear", "ease_in", "ease_out", "ease_in_out"],
    )
    parser.add_argument("--encoding", default="CPU", choices=["Auto", "CPU", "NVIDIA NVENC", "Intel Quick Sync", "AMD AMF"])
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--quality", default="maximum", choices=["maximum", "high", "balanced", "fast", "custom"])
    parser.add_argument("--output-preset", default="youtube_landscape", choices=["youtube_landscape", "youtube_vertical", "custom"])
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--voiceover", action="append", default=[], help="voiceover audio file (repeat for multiple units)")
    parser.add_argument("--script", action="append", default=[], help="script file (single or matched per voiceover)")
    parser.add_argument(
        "--script-mode", choices=["single", "global", "matched", "individual"], default="single"
    )
    parser.add_argument(
        "--global-script", default="",
        help="authoritative single script for all ordered voiceover units (single mode)",
    )
    parser.add_argument(
        "--voiceover-pause", type=float, default=0.7,
        help="silence between voiceover units in seconds (default 0.7; does not change --pause end padding)",
    )
    parser.add_argument(
        "--voiceover-order",
        choices=["natural", "mtime_oldest", "mtime_newest", "manual"],
        default="natural",
        help="voiceover order before alignment and rendering",
    )
    parser.add_argument(
        "--video-order",
        choices=["natural", "alphabetical", "random", "manual", "folder_alternating"],
        default="natural",
        help="Natural, Alphabetical, Random, or explicit persisted Manual order",
    )
    parser.add_argument(
        "--music", default="",
        help="background music for the Long-Form / basic Main Video (never used in Shorts)",
    )
    parser.add_argument(
        "--short-music", default="",
        help="separate background music used only for the generated YouTube Shorts",
    )
    parser.add_argument("--original-audio", choices=["mute", "low", "original"], default="mute")
    parser.add_argument(
        "--music-volume", type=int, default=MUSIC_VOLUME_PERCENT,
        help=(
            f"shared background music volume in percent (default {MUSIC_VOLUME_PERCENT}). It is the "
            "migration fallback for both YouTube outputs unless --long-music-volume / "
            "--short-music-volume override it"
        ),
    )
    parser.add_argument(
        "--long-music-volume", type=int, default=None,
        help=(
            f"Long-Form background music volume in percent (default {LONG_FORM_MUSIC_VOLUME}); "
            "independent from --short-music-volume"
        ),
    )
    parser.add_argument(
        "--short-music-volume", type=int, default=None,
        help=(
            f"YouTube Shorts background music volume in percent (default {SHORTS_MUSIC_VOLUME}); "
            "independent from --long-music-volume"
        ),
    )
    parser.add_argument(
        "--pause", "--end-padding", dest="pause", type=float, default=None,
        help=(
            "legacy Main Video end padding. It is the same timeline section as "
            "--long-outro, so the visual outro is never applied twice; when "
            f"omitted, --long-outro (default {LONG_FORM_OUTRO_SECONDS} s) is used"
        ),
    )
    parser.add_argument(
        "--short-video", choices=["hold", "loop"], default="hold",
        help="hold final rendered frame or loop the complete active ordered timeline",
    )
    # Independent Before/After Merge duration controls. ``--video-speed``
    # remains a compatibility alias for older scripts.
    parser.add_argument(
        "--duration-before-merge", type=float, default=0.70,
        help="per-clip playback multiplier before merge (default 0.70; timeline = source / multiplier)",
    )
    parser.add_argument(
        "--duration-after-merge", type=float, default=1.00,
        help="whole-master playback multiplier after merge (default 1.00)",
    )
    parser.add_argument(
        "--enable-duration-after-merge", action="store_true",
        help="run the independent post-merge duration operation",
    )
    parser.add_argument(
        "--video-speed", type=float, default=None,
        help="deprecated alias for --duration-before-merge",
    )
    parser.add_argument(
        "--duration-fit", choices=["cut", "stretch"], default="cut",
        help="cut the last selected clip (default) or stretch (slow) it within the stretch limit",
    )
    parser.add_argument(
        "--max-stretch", type=float, default=10.0,
        help="maximum stretch of the final clip in percent (default 10)",
    )
    # Optional silent Stage-2 Add Image section.
    parser.add_argument(
        "--image-enabled", "--add-image-enabled", action="store_true",
        help="enable Add Image explicitly (normally implied by an image path)",
    )
    parser.add_argument(
        "--image", "--image-insertion", "--add-image", "--add-image-path",
        dest="image_path", default="",
        help="Stage-2 Add Image (.png, .jpg, .jpeg or .webp)",
    )
    parser.add_argument(
        "--image-position", "--add-image-position",
        choices=["before_main", "after_main", "after_intro", "before_outro"],
        default="after_intro",
        help="place Add Image immediately before or after Main Video (legacy aliases accepted)",
    )
    parser.add_argument(
        "--image-duration", "--add-image-duration", type=float, default=4.0,
        help="Add Image duration in seconds (default 4.0)",
    )
    parser.add_argument(
        "--image-transition", "--image-transition-effect", "--add-image-transition",
        dest="image_transition_type",
        choices=["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
        default="cross_dissolve",
        help="transition family at the image boundary (default: Cross Dissolve)",
    )
    parser.add_argument(
        "--image-transition-duration", "--add-image-transition-duration", type=float, default=1.0,
        help="Add Image boundary transition duration in seconds (default 1.0)",
    )
    parser.add_argument(
        "--image-fit-mode", "--image-fit", "--image-sizing", "--add-image-sizing",
        dest="image_fit_mode",
        choices=["fit", "fill", "crop"], default="fit")
    parser.add_argument(
        "--image-zoom", "--add-image-zoom", type=int, default=100,
        help="Add Image zoom in percent (100-300; default 100)",
    )
    parser.add_argument(
        "--image-filter", "--image-look", "--add-image-look",
        choices=["natural", "cinematic", "moody", "film", "dark_editorial"],
        default="natural",
    )
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument(
        "--subtitle-output-mode", "--subtitle-output", dest="subtitle_output_mode",
        default="with_subtitles",
        choices=["with_subtitles", "without_subtitles", "with_and_without_subtitles", "burned_and_sidecars", "burned_only"],
        help="With Subtitles (default), Without Subtitles, or With and Without Subtitles",
    )
    parser.add_argument("--language", choices=["German", "English", "Auto"], default="German")
    parser.add_argument("--subtitle-style", default="long_1")
    parser.add_argument(
        "--subtitle-animation", choices=list(accepted_animation_values("long")),
        default=DEFAULT_LONG_ANIMATION,
        help=(
            "Long-Form caption animation; deprecated values (outline_highlight) "
            "are accepted and migrated to a clean glyph-aligned animation"
        ),
    )
    parser.add_argument("--subtitle-font", choices=["eveleth_clean", "modern_sans_bold", "clean_sans"], default="modern_sans_bold")
    parser.add_argument(
        "--subtitle-position", choices=["Bottom Center", "Center", "Bottom", "Medium-Low", "Middle", "Top"],
        default=None, help="default is Center for landscape and Bottom Center for vertical",
    )
    parser.add_argument("--subtitle-debug-overlay", action="store_true")
    parser.add_argument("--short-subtitle-style", default="short_1")
    parser.add_argument(
        "--short-subtitle-animation", choices=list(accepted_animation_values("short")),
        default=DEFAULT_SHORT_ANIMATION,
        help=(
            "Shorts caption animation. Word Highlight is no longer available for "
            "Shorts and Outline Highlight is deprecated; both are accepted here "
            "and migrated to a safe animation instead of failing the run"
        ),
    )
    parser.add_argument("--short-subtitle-font", choices=["eveleth_clean", "modern_sans_bold", "clean_sans"], default="modern_sans_bold")
    parser.add_argument(
        "--short-subtitle-position", choices=["Bottom Center", "Center", "Bottom", "Medium-Low", "Middle", "Top"],
        default="Bottom Center",
    )
    parser.add_argument(
        "--long-intro", type=float, default=LONG_FORM_INTRO_SECONDS,
        help=f"Long-Form visual-only intro before the voiceover in seconds (default {LONG_FORM_INTRO_SECONDS}; 0 disables it)",
    )
    parser.add_argument(
        "--long-outro", type=float, default=LONG_FORM_OUTRO_SECONDS,
        help=(
            f"Long-Form visual-only outro after the voiceover in seconds (default {LONG_FORM_OUTRO_SECONDS}; "
            "0 disables it). This is the Main Video end padding - it is never added twice"
        ),
    )
    parser.add_argument(
        "--short-intro", type=float, default=SHORT_INTRO_SECONDS,
        help=f"Short visual-only intro before the voiceover in seconds (default {SHORT_INTRO_SECONDS}; 0 disables it)",
    )
    parser.add_argument(
        "--short-outro", type=float, default=SHORT_OUTRO_SECONDS,
        help=(
            f"Short visual-only outro after the voiceover in seconds (default {SHORT_OUTRO_SECONDS}; 0 disables it). "
            "It replaces the historical fixed 0.7 s Short ending"
        ),
    )
    parser.add_argument(
        "--opening-effect", choices=[key for key, _label in OPENING_EFFECTS],
        default=OPENING_EFFECT_NONE,
        help="subtle Main Video opening effect; it never changes timeline, audio or subtitle timing",
    )
    parser.add_argument("--allow-alignment-warning", action="store_true")
    parser.add_argument("--watermark", default="")
    parser.add_argument("--main-video", default="")
    parser.add_argument("--intro", default="")
    parser.add_argument("--outro", default="")
    parser.add_argument("--intro-audio", choices=["mute", "low", "original"], default="original")
    parser.add_argument("--outro-audio", choices=["mute", "low", "original"], default="original")
    parser.add_argument("--no-outro-transition", action="store_true")
    args = parser.parse_args()
    voiceover_paths = [str(Path(value).expanduser().resolve()) for value in args.voiceover]
    script_paths = [str(Path(value).expanduser().resolve()) for value in args.script]
    script_mode = "matched" if args.script_mode in {"matched", "individual"} else "single"
    global_script = str(Path(args.global_script).expanduser().resolve()) if args.global_script else (
        script_paths[0] if script_mode == "single" and script_paths else ""
    )
    if script_mode == "single":
        script_paths = [global_script] if global_script else []
    subtitle_position = args.subtitle_position or ("Center" if args.aspect == "16:9" else "Bottom Center")
    # Output-specific transitions and music volumes. An omitted per-output flag
    # falls back to an explicitly given shared flag, and otherwise to the new
    # per-output default - exactly the migration rule the model resolvers use,
    # so CLI, GUI and project files behave identically. The shared transition
    # duration keeps its historical default for the basic/Main Video merge.
    shared_transition_duration = (
        TRANSITION_DURATION_LEGACY_DEFAULT if args.transition is None else args.transition
    )
    long_transition_duration = (
        args.long_transition
        if args.long_transition is not None
        else (args.transition if args.transition is not None else LONG_FORM_TRANSITION_DURATION)
    )
    short_transition_duration = (
        args.short_transition
        if args.short_transition is not None
        else (args.transition if args.transition is not None else SHORTS_TRANSITION_DURATION)
    )
    long_music_volume = (
        args.long_music_volume if args.long_music_volume is not None else args.music_volume
    )
    short_music_volume = (
        args.short_music_volume if args.short_music_volume is not None else args.music_volume
    )
    configured_sources = [str(Path(value).expanduser().resolve()) for value in args.source_folder]
    settings = ExportSettings(
        export_mode=normalize_export_mode(args.export_mode),
        aspect=args.aspect, resolution=args.resolution,
        source_folders=configured_sources,
        video_order_mode=args.video_order,
        transition_type=args.transition_effect, transition_ease=args.transition_ease,
        transition_duration=shared_transition_duration, encoding=args.encoding,
        # Independent per-output transition settings (Cross Dissolve / 2.0 s by
        # default for both). Combined mode and One-Click use the Long-Form pair
        # for the Long-Form job and the Shorts pair for every Short.
        long_form_transition_type=args.long_transition_effect or args.transition_effect,
        long_form_transition_duration=long_transition_duration,
        shorts_transition_type=args.short_transition_effect or args.transition_effect,
        shorts_transition_duration=short_transition_duration,
        crf=args.crf, quality_preset=args.quality, output_preset=args.output_preset,
        normalize_audio=not args.no_normalize,
        workflow_stage=args.stage, voiceover_path=voiceover_paths[0] if voiceover_paths else "",
        script_path=script_paths[0] if script_paths else "",
        voiceover_paths=voiceover_paths, script_paths=script_paths, script_mode=script_mode,
        global_script_path=global_script,
        voiceover_pause=max(0.0, min(10.0, args.voiceover_pause)),
        voiceover_order_mode=args.voiceover_order,
        music_path=args.music, short_music_path=args.short_music,
        # Explicit visual-only sections. The Long-Form outro is the canonical
        # Main Video end padding (final_pause), so the tail exists exactly once.
        long_form_intro_seconds=args.long_intro,
        # ``--pause``/``--end-padding`` is the legacy alias of the Long-Form
        # outro, so an explicit value drives BOTH names; otherwise the orchestrated
        # Long-Form job would derive its tail from the outro default alone.
        long_form_outro_seconds=(
            args.pause if args.pause is not None else args.long_outro
        ),
        short_intro_seconds=args.short_intro,
        short_outro_seconds=args.short_outro,
        visual_intro_seconds=args.long_intro,
        final_pause=args.pause if args.pause is not None else args.long_outro,
        opening_effect=args.opening_effect,
        # Random order reserves its first three clips from this root.
        legacy_input_root=(
            str(Path(args.input).expanduser().resolve()) if args.input else ""
        ),
        original_audio_mode=args.original_audio,
        music_volume=args.music_volume,
        # Independent per-output music volumes (44 % by default for both). The
        # music itself always plays from 0.000 s to the final video frame.
        long_form_music_volume=long_music_volume,
        shorts_music_volume=short_music_volume,
        short_video_mode=args.short_video,
        duration_fit_mode=args.duration_fit,
        max_stretch_percent=max(1.0, min(50.0, args.max_stretch)),
        duration_before_merge=max(0.25, min(4.0, args.video_speed if args.video_speed is not None else args.duration_before_merge)),
        duration_after_merge=max(0.25, min(4.0, args.duration_after_merge)),
        duration_after_merge_enabled=bool(args.enable_duration_after_merge),
        # Legacy field is retained only for old cache/API compatibility.
        video_speed=1.0,
        image_enabled=bool(args.image_path) or args.image_enabled, image_path=args.image_path,
        image_position=args.image_position,
        image_duration=max(0.5, min(60.0, args.image_duration)),
        image_transition_type=args.image_transition_type,
        image_transition_duration=max(0.0, min(5.0, args.image_transition_duration)),
        image_fit_mode=args.image_fit_mode, image_zoom=max(100, min(300, args.image_zoom)),
        image_filter=args.image_filter,
        subtitle_output_mode=args.subtitle_output_mode,
        subtitle_enabled=args.subtitles, subtitle_language=args.language,
        subtitle_style=args.subtitle_style, subtitle_animation=args.subtitle_animation,
        subtitle_font=args.subtitle_font, subtitle_position=subtitle_position,
        short_subtitle_style=args.short_subtitle_style,
        short_subtitle_animation=args.short_subtitle_animation,
        short_subtitle_font=args.short_subtitle_font,
        short_subtitle_position=args.short_subtitle_position,
        subtitle_debug_overlay=args.subtitle_debug_overlay,
        allow_alignment_warnings=args.allow_alignment_warning,
        watermark_enabled=bool(args.watermark), watermark_path=args.watermark,
        main_video_path=args.main_video, intro_path=args.intro, outro_path=args.outro,
        intro_audio_mode=args.intro_audio, outro_audio_mode=args.outro_audio,
        outro_transition_enabled=not args.no_outro_transition,
    )
    ffmpeg, ffprobe = locate_ffmpeg()
    engine = VideoMergerEngine(ffmpeg, ffprobe)
    engine.preflight(print)
    output_store = GeneratedOutputStore()
    if args.stage == "outro":
        output, _report = MainProjectEngine(engine).add_outro(settings, args.output, log=print)
    else:
        if args.input is None and not configured_sources:
            parser.error("--input or at least one --source-folder is required for basic/main/complete stage")
        inputs = discover_videos(
            configured_sources or args.input,
            order_store=ProjectOrderStore(), excluded_paths=output_store.paths()
        )
        media = engine.analyze(inputs, print)
        media = order_media_for_video_order(
            media, settings.video_order_mode, legacy_root=settings.legacy_input_root,
        )
        if args.stage == "main":
            result = MainProjectEngine(engine).create_youtube_exports(
                media, settings, args.output, log=print, order_already_applied=True,
            )
            output = result.primary_output
        elif args.stage == "complete":
            result = MainProjectEngine(engine).create_youtube_exports(
                media, settings, args.output, log=print,
                order_already_applied=True, complete=True,
            )
            output = result.primary_output
        else:
            resolved = engine.make_plan(media, settings, print)
            output = make_output_path(args.output, settings.aspect)
            engine.export(media, settings, resolved, output, log=print, video_order_applied=True)
    output_store.add(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
