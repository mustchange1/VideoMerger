from __future__ import annotations

import argparse
from pathlib import Path

from .video_merger.discovery import discover_videos
from .video_merger.engine import VideoMergerEngine
from .video_merger.main_project import MainProjectEngine
from .video_merger.models import ExportSettings
from .video_merger.output_manager import make_output_path
from .video_merger.paths import locate_ffmpeg
from .video_merger.project_order import GeneratedOutputStore, ProjectOrderStore
from .video_merger.video_pool import order_media_for_video_order


def main() -> int:
    parser = argparse.ArgumentParser(description="VideoMerger headless export")
    parser.add_argument("--stage", choices=["basic", "main", "outro", "complete"], default="basic")
    parser.add_argument("--input", type=Path, help="legacy single input folder; use --source-folder repeatedly for multiple folders")
    parser.add_argument(
        "--source-folder", action="append", default=[], type=Path,
        help="configured video source folder; repeat for multiple folders (overrides --input)",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    parser.add_argument("--resolution", default="Auto")
    parser.add_argument("--transition", type=float, default=1.0, help="transition duration in seconds")
    parser.add_argument(
        "--transition-effect", default="cross_dissolve",
        choices=["smooth_blur", "cross_dissolve", "film_dissolve", "additive_dissolve"],
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
    parser.add_argument("--music", default="")
    parser.add_argument("--original-audio", choices=["mute", "low", "original"], default="mute")
    parser.add_argument("--music-volume", type=int, default=44)
    parser.add_argument("--pause", type=float, default=1.0)
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
    # Optional silent Quote/Flyer artwork between Intro and Main.
    parser.add_argument("--quote", action="store_true", help="enable the Quote / Flyer section")
    parser.add_argument(
        "--quote-artwork", default="",
        help="Quote/Flyer artwork (.png, .jpg, .jpeg, .webp or .pdf)",
    )
    parser.add_argument(
        "--quote-pdf-page", "--quote-page", type=int, default=1,
        help="one-based PDF page for --quote-artwork (default: 1)",
    )
    parser.add_argument(
        "--quote-fit-mode", "--quote-fit", dest="quote_fit_mode",
        choices=["fit", "fill", "crop"], default="fit",
        help="artwork framing mode; Fit preserves the complete artwork (default)",
    )
    parser.add_argument(
        "--quote-duration", type=float, default=4.0,
        help="Quote/Flyer duration in seconds (0.5-5.0; default 4.0)",
    )
    # Independent silent Stage-2 Image Insertion (never Quote/Flyer/PDF).
    parser.add_argument("--image", "--image-insertion", dest="image_path", default="",
        help="Stage-2 image insertion (.png, .jpg, .jpeg or .webp)")
    parser.add_argument("--image-position", choices=["after_intro", "before_outro"], default="after_intro")
    parser.add_argument("--image-duration", type=float, default=4.0,
        help="Image Insertion duration in seconds (default 4.0)")
    parser.add_argument("--image-transition-duration", type=float, default=1.0,
        help="Image boundary transition duration in seconds (default 1.0)")
    parser.add_argument("--image-fit-mode", "--image-fit", dest="image_fit_mode",
        choices=["fit", "fill", "crop"], default="fit")
    parser.add_argument("--image-zoom", type=int, default=100,
        help="Image Insertion zoom in percent (100-300; default 100)")
    parser.add_argument("--image-filter", choices=["natural", "cinematic", "moody", "film", "dark_editorial"], default="natural")
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument(
        "--subtitle-output-mode", "--subtitle-output", dest="subtitle_output_mode",
        default="burned_and_sidecars",
        choices=["burned_and_sidecars", "burned_only", "without_subtitles"],
        help="With Burned-in Subtitles + SRT + VTT (default), burned_only, or without_subtitles",
    )
    parser.add_argument("--language", choices=["German", "English", "Auto"], default="German")
    parser.add_argument("--subtitle-style", default="long_1")
    parser.add_argument("--subtitle-animation", choices=["type_reveal", "color_change", "word_highlight", "outline_highlight", "static_phrase"], default="static_phrase")
    parser.add_argument("--subtitle-font", choices=["eveleth_clean", "modern_sans_bold", "clean_sans"], default="modern_sans_bold")
    parser.add_argument(
        "--subtitle-position", choices=["Bottom Center", "Center", "Bottom", "Medium-Low", "Middle", "Top"],
        default=None, help="default is Center for landscape and Bottom Center for vertical",
    )
    parser.add_argument("--subtitle-debug-overlay", action="store_true")
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
    configured_sources = [str(Path(value).expanduser().resolve()) for value in args.source_folder]
    settings = ExportSettings(
        aspect=args.aspect, resolution=args.resolution,
        source_folders=configured_sources,
        video_order_mode=args.video_order,
        transition_type=args.transition_effect, transition_ease=args.transition_ease,
        transition_duration=args.transition, encoding=args.encoding,
        crf=args.crf, quality_preset=args.quality, output_preset=args.output_preset,
        normalize_audio=not args.no_normalize,
        workflow_stage=args.stage, voiceover_path=voiceover_paths[0] if voiceover_paths else "",
        script_path=script_paths[0] if script_paths else "",
        voiceover_paths=voiceover_paths, script_paths=script_paths, script_mode=script_mode,
        global_script_path=global_script,
        voiceover_pause=max(0.0, min(10.0, args.voiceover_pause)),
        voiceover_order_mode=args.voiceover_order,
        music_path=args.music, original_audio_mode=args.original_audio,
        music_volume=args.music_volume, final_pause=args.pause,
        short_video_mode=args.short_video,
        duration_fit_mode=args.duration_fit,
        max_stretch_percent=max(1.0, min(50.0, args.max_stretch)),
        duration_before_merge=max(0.25, min(4.0, args.video_speed if args.video_speed is not None else args.duration_before_merge)),
        duration_after_merge=max(0.25, min(4.0, args.duration_after_merge)),
        duration_after_merge_enabled=bool(args.enable_duration_after_merge),
        # Legacy field is retained only for old cache/API compatibility.
        video_speed=1.0,
        quote_enabled=args.quote or bool(args.quote_artwork),
        quote_input_mode="artwork",
        quote_artwork_path=args.quote_artwork,
        quote_pdf_page=args.quote_pdf_page,
        quote_artwork_fit_mode=args.quote_fit_mode,
        quote_duration=max(0.5, min(5.0, args.quote_duration)),
        image_enabled=bool(args.image_path), image_path=args.image_path,
        image_position=args.image_position,
        image_duration=max(0.5, min(60.0, args.image_duration)),
        image_transition_duration=max(0.0, min(5.0, args.image_transition_duration)),
        image_fit_mode=args.image_fit_mode, image_zoom=max(100, min(300, args.image_zoom)),
        image_filter=args.image_filter,
        subtitle_output_mode=args.subtitle_output_mode,
        subtitle_enabled=args.subtitles, subtitle_language=args.language,
        subtitle_style=args.subtitle_style, subtitle_animation=args.subtitle_animation,
        subtitle_font=args.subtitle_font, subtitle_position=subtitle_position,
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
        media = order_media_for_video_order(media, settings.video_order_mode)
        if args.stage == "main":
            result = MainProjectEngine(engine).create_main(
                media, settings, args.output, log=print, order_already_applied=True,
            )
            output = result.video
        elif args.stage == "complete":
            result = MainProjectEngine(engine).create_complete(
                media, settings, args.output, log=print, order_already_applied=True,
            )
            output = result.final_video
        else:
            resolved = engine.make_plan(media, settings, print)
            output = make_output_path(args.output, settings.aspect)
            engine.export(media, settings, resolved, output, log=print, video_order_applied=True)
    output_store.add(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
