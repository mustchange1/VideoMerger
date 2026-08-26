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


def main() -> int:
    parser = argparse.ArgumentParser(description="VideoMerger headless export")
    parser.add_argument("--stage", choices=["basic", "main", "outro", "complete"], default="basic")
    parser.add_argument("--input", type=Path, help="video folder for basic/main stage")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aspect", choices=["16:9", "9:16"], default="16:9")
    parser.add_argument("--resolution", default="Auto")
    parser.add_argument("--transition", type=float, default=0.5, help="transition duration in seconds")
    parser.add_argument(
        "--transition-effect", default="smooth_blur",
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
    parser.add_argument("--script-mode", choices=["single", "matched"], default="single")
    parser.add_argument("--music", default="")
    parser.add_argument("--original-audio", choices=["mute", "low", "original"], default="mute")
    parser.add_argument("--music-volume", type=int, default=22)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument(
        "--short-video", choices=["hold", "loop"], default="hold",
        help="hold final rendered frame or loop the complete active ordered timeline",
    )
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument("--language", choices=["German", "English", "Auto"], default="German")
    parser.add_argument("--subtitle-style", default="long_1")
    parser.add_argument("--subtitle-animation", choices=["type_reveal", "color_change", "word_highlight", "outline_highlight", "static_phrase"], default="type_reveal")
    parser.add_argument("--subtitle-font", choices=["eveleth_clean", "modern_sans_bold", "clean_sans"], default="modern_sans_bold")
    parser.add_argument("--subtitle-position", choices=["Bottom", "Medium-Low", "Middle", "Top"], default="Bottom")
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
    settings = ExportSettings(
        aspect=args.aspect, resolution=args.resolution,
        transition_type=args.transition_effect, transition_ease=args.transition_ease,
        transition_duration=args.transition, encoding=args.encoding,
        crf=args.crf, quality_preset=args.quality, output_preset=args.output_preset,
        normalize_audio=not args.no_normalize,
        workflow_stage=args.stage, voiceover_path=voiceover_paths[0] if voiceover_paths else "",
        script_path=script_paths[0] if script_paths else "",
        voiceover_paths=voiceover_paths, script_paths=script_paths, script_mode=args.script_mode,
        music_path=args.music, original_audio_mode=args.original_audio,
        music_volume=args.music_volume, final_pause=args.pause,
        short_video_mode=args.short_video,
        subtitle_enabled=args.subtitles, subtitle_language=args.language,
        subtitle_style=args.subtitle_style, subtitle_animation=args.subtitle_animation,
        subtitle_font=args.subtitle_font, subtitle_position=args.subtitle_position,
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
        if args.input is None:
            parser.error("--input is required for basic/main/complete stage")
        inputs = discover_videos(
            args.input, order_store=ProjectOrderStore(), excluded_paths=output_store.paths()
        )
        media = engine.analyze(inputs, print)
        if args.stage == "main":
            result = MainProjectEngine(engine).create_main(media, settings, args.output, log=print)
            output = result.video
        elif args.stage == "complete":
            result = MainProjectEngine(engine).create_complete(media, settings, args.output, log=print)
            output = result.final_video
        else:
            resolved = engine.make_plan(media, settings, print)
            output = make_output_path(args.output, settings.aspect)
            engine.export(media, settings, resolved, output, log=print)
    output_store.add(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
