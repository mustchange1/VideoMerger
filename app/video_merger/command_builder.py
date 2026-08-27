from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import quote
from .filter_escape import filter_file_value
from .hardware import encoder_arguments
from .models import ExportSettings, MediaSequence, ResolvedExport
from .paths import project_root
from .transition_effects import normalize_transition, transition_blur_sigma, xfade_expression


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _mode_gain(mode: str) -> float:
    return {"mute": 0.0, "low": 0.22, "original": 1.0}.get(mode, 1.0)


def _percent_gain(value: int) -> float:
    # Slider is presented as percent; the filter receives a proper linear gain
    # (20*log10(gain) dB). Zero remains true silence.
    return max(0.0, min(1.5, float(value) / 100.0))


def _filter_path(value: str) -> str:
    """Filter value for a file path (subtitles/fontsdir/drawtext fontfile).

    1.3.0 root-cause Windows fix: FFmpeg runs with ``cwd`` = project root and
    every render-time file referenced by the graph is app-staged under that
    root with an ASCII name, so the graph normally receives a plain relative
    POSIX path — no drive-letter colon, no backslash, no space, no umlaut can
    appear in the value on any Windows machine.  Paths outside the anchor
    fall back to the verified UNQUOTED two-level escaped absolute form
    (forward slashes; apostrophe-safe — the 1.2.4 quoted form raised
    ValueError for ``C:/Users/O'Brien/...`` and could not represent it).
    See :mod:`filter_escape` for the verified two-pass parse rules.
    """
    return filter_file_value(value, project_root())


def _atempo_chain(rate: float) -> str:
    """atempo chain for a clip playback rate (atempo range is 0.5–100)."""
    parts: list[str] = []
    remaining = max(0.25, min(100.0, rate))
    while remaining < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={_number(remaining)}")
    return ",".join(parts)


def _watermark_active(settings: ExportSettings) -> bool:
    if not settings.watermark_enabled or not settings.watermark_path:
        return False
    if settings.workflow_stage == "main":
        return settings.watermark_scope in {"main", "both"}
    if settings.workflow_stage == "outro":
        return settings.watermark_scope in {"outro", "both"}
    return False


@dataclass(slots=True)
class BuiltCommand:
    command: list[str]
    filter_graph: str


class FFmpegCommandBuilder:
    """Build one filter graph and one final encode for all original clips."""

    def __init__(self, ffmpeg_path: Path | str):
        self.ffmpeg_path = str(ffmpeg_path)

    def build_filter_graph(self, media: MediaSequence, settings: ExportSettings, resolved: ResolvedExport) -> str:
        width, height = resolved.width, resolved.height
        lines: list[str] = []
        video_labels: list[str] = []
        audio_labels: list[str] = []
        transition_type = normalize_transition(settings.transition_type)
        transition_blur = transition_blur_sigma(transition_type, width, height)
        dissolve_expression = xfade_expression(transition_type, settings.transition_ease)
        # 1.2.4: Erzeugte Quote-Karten erhalten bewusst KEIN -i-Input. Die
        # echten Input-Nummern aller übrigen Medien sind deshalb versetzt
        # um die Anzahl der vor ihnen stehenden Karten.
        real_input: list[int | None] = []
        running = 0
        for item in media:
            if item.is_generated_quote:
                real_input.append(None)
            else:
                real_input.append(running)
                running += 1
        next_input = running
        voice_indices: list[int] = []
        music_index: int | None = None
        watermark_index: int | None = None
        voice_inputs = list(getattr(settings, "voiceover_paths", None) or [])
        if not voice_inputs and settings.voiceover_path:
            voice_inputs = [settings.voiceover_path]
        if settings.workflow_stage == "main" and voice_inputs:
            voice_indices = list(range(next_input, next_input + len(voice_inputs)))
            next_input += len(voice_inputs)
        if settings.workflow_stage == "main" and settings.music_path:
            music_index, next_input = next_input, next_input + 1
        if _watermark_active(settings):
            watermark_index, next_input = next_input, next_input + 1

        for index, item in enumerate(media):
            duration = resolved.effective_durations[index]
            base = f"base{index}"
            if item.is_generated_quote:
                # 1.2.4/1.3.0: Die Quote-Karte wird vollständig im Graph
                # generiert (color-Quelle + stilabhängige Behandlung +
                # drawtext + optionaler subtiler Zoom). Sie ist stumm: das
                # Audio-Label kommt unten über den anullsrc-Zweig
                # (audio.present=False). Kein -i-Input, keine Quelle auf
                # Festplatte.
                lines.extend(
                    quote.quote_video_chain(
                        quote.layout_quote(
                            settings.quote_text,
                            settings.quote_attribution,
                            settings.quote_font,
                            width,
                            height,
                            style_key=settings.quote_style,
                            font_size_percent=settings.quote_font_size_percent,
                            font_weight=settings.quote_font_weight,
                            text_color=settings.quote_text_color,
                            background_color=settings.quote_background_color,
                            zoom_percent=settings.quote_zoom_percent,
                            position=settings.quote_position,
                            safe_padding_percent=settings.quote_safe_padding_percent,
                        ),
                        width, height, resolved.fps, duration, base,
                    )
                )
            else:
                original_duration = item.source_duration or item.duration
                # 1.3.0 playback_rate: global Main Video speed and/or Smart
                # Last-Clip Stretch. rate < 1 slows the clip (stretch), rate
                # > 1 speeds it up. trim works in SOURCE seconds, so the clip
                # is trimmed to duration*rate and then time-scaled; tpad must
                # cover the *scaled* source so a slowed clip never runs out of
                # real frames before its timeline duration ends.
                rate = max(0.25, min(4.0, float(getattr(item, "playback_rate", 1.0) or 1.0)))
                needed_source = max(2.0 / resolved.fps, duration * rate)
                extra_pad = max(2.0 / resolved.fps, needed_source - original_duration + 2.0 / resolved.fps)
                rate_video = "" if abs(rate - 1.0) < 1e-6 else f"setpts=PTS/{_number(rate)},"
                source = f"[{real_input[index]}:v:0]"
                pre = f"pre{index}"
                lines.append(
                    f"{source}fps={resolved.fps_expr}:round=near,"
                    f"tpad=stop_mode=clone:stop_duration={_number(extra_pad)},"
                    f"trim=duration={_number(needed_source)},{rate_video}settb=AVTB,setpts=PTS-STARTPTS[{pre}]"
                )

                target_ratio = width / height
                same_aspect = abs(item.aspect_ratio - target_ratio) < 0.008
                if settings.fit_mode == "crop_fill":
                    lines.append(
                        f"[{pre}]scale=w={width}:h={height}:force_original_aspect_ratio=increase:"
                        f"force_divisible_by=2:flags=lanczos,crop={width}:{height}:(iw-ow)/2:(ih-oh)/2,"
                        f"setsar=1,format=yuv420p[{base}]"
                    )
                elif same_aspect:
                    lines.append(
                        f"[{pre}]scale=w={width}:h={height}:flags=lanczos,setsar=1,format=yuv420p[{base}]"
                    )
                else:
                    bg_source, fg_source = f"bgsrc{index}", f"fgsrc{index}"
                    bg, fg = f"bg{index}", f"fg{index}"
                    zoom = max(1.0, min(1.25, settings.background_zoom / 100.0))
                    bg_width = int(round(width * zoom / 2) * 2)
                    bg_height = int(round(height * zoom / 2) * 2)
                    lines.append(f"[{pre}]split=2[{bg_source}][{fg_source}]")
                    # Blurring a full 1080p/4K background for every frame was the
                    # dominant CPU bottleneck in 1.2.0. Build the synchronized blur
                    # at half linear resolution, then upscale once. This preserves
                    # the same live source and canvas while cutting blur pixels by
                    # roughly 75 percent; small canvases keep the original path.
                    use_half_blur = settings.background_blur > 0 and min(width, height) >= 720
                    factor = 0.5 if use_half_blur else 1.0
                    work_width = max(16, round(bg_width * factor / 2) * 2)
                    work_height = max(16, round(bg_height * factor / 2) * 2)
                    crop_width = max(16, round(width * factor / 2) * 2)
                    crop_height = max(16, round(height * factor / 2) * 2)
                    bg_filters = (
                        f"scale=w={work_width}:h={work_height}:force_original_aspect_ratio=increase:"
                        f"force_divisible_by=2:flags=lanczos,crop={crop_width}:{crop_height}:(iw-ow)/2:(ih-oh)/2"
                    )
                    if settings.background_blur > 0:
                        sigma = max(1.0, settings.background_blur * factor)
                        bg_filters += f",gblur=sigma={_number(sigma)}:steps=2"
                    if use_half_blur:
                        bg_filters += f",scale=w={width}:h={height}:flags=bicubic"
                    if settings.background_darkness > 0:
                        darkness = min(0.30, settings.background_darkness / 100.0)
                        bg_filters += f",eq=brightness=-{_number(darkness)}"
                    bg_filters += ",setsar=1,format=yuv420p"
                    lines.append(f"[{bg_source}]{bg_filters}[{bg}]")
                    lines.append(
                        f"[{fg_source}]scale=w={width}:h={height}:force_original_aspect_ratio=decrease:"
                        f"force_divisible_by=2:flags=lanczos,setsar=1,format=yuv420p[{fg}]"
                    )
                    lines.append(
                        f"[{bg}][{fg}]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1:format=auto,"
                        f"format=yuv420p[{base}]"
                    )

            incoming = resolved.transitions[index - 1] if index > 0 else 0.0
            outgoing = resolved.transitions[index] if index < len(resolved.transitions) else 0.0
            if transition_blur > 0 and (incoming > 0 or outgoing > 0):
                normal, blurred = f"normal{index}", f"blurred{index}"
                lines.append(f"[{base}]split=2[{normal}][blurin{index}]")
                # 1.2.0 blurred every frame of every clip even though transition
                # blur is only visible at clip boundaries. Timeline-enable the
                # expensive gblur so long programs do not pay that cost outside
                # the selected transition windows.
                blur_windows: list[str] = []
                if incoming > 0:
                    blur_windows.append(f"between(t,0,{_number(incoming)})")
                if outgoing > 0:
                    blur_start = max(0.0, duration - outgoing)
                    blur_windows.append(
                        f"between(t,{_number(blur_start)},{_number(duration)})"
                    )
                blur_enable = "+".join(blur_windows) or "0"
                lines.append(
                    f"[blurin{index}]gblur=sigma={_number(transition_blur)}:steps=2:"
                    f"enable='{blur_enable}'[{blurred}]"
                )
                ramps: list[str] = []
                if incoming > 0:
                    ramps.append(f"if(lt(T,{_number(incoming)}),1-T/{_number(incoming)},0)")
                if outgoing > 0:
                    start = duration - outgoing
                    ramps.append(f"if(gt(T,{_number(start)}),(T-{_number(start)})/{_number(outgoing)},0)")
                weight = ramps[0] if len(ramps) == 1 else f"max({ramps[0]},{ramps[1]})"
                weight = f"min(1,max(0,{weight}))"
                final_video = f"v{index}"
                # The ramp blend is also per-pixel expensive. Its weight is zero
                # outside these same transition windows, so timeline gating is
                # visually identical while avoiding full-program blend work.
                lines.append(
                    f"[{normal}][{blurred}]blend=all_expr='A*(1-({weight}))+B*({weight})':"
                    f"shortest=1:enable='{blur_enable}',"
                    f"fps={resolved.fps_expr}:round=near,settb=AVTB[{final_video}]"
                )
            else:
                final_video = f"v{index}"
                lines.append(f"[{base}]fps={resolved.fps_expr}:round=near,settb=AVTB[{final_video}]")
            video_labels.append(final_video)

            audio_label = f"a{index}"
            stage2_modes = getattr(settings, "stage2_audio_modes", None) or []
            if settings.workflow_stage == "main":
                clip_gain = _mode_gain(settings.original_audio_mode)
            elif settings.workflow_stage == "outro" and len(stage2_modes) == len(media):
                clip_gain = _mode_gain(stage2_modes[index])
            elif settings.workflow_stage == "outro" and index == 1:
                clip_gain = _mode_gain(settings.outro_audio_mode)
            else:
                clip_gain = 1.0
            if item.audio.present and real_input[index] is not None:
                # 1.3.0: the clip's own audio follows its playback rate so a
                # stretched/sped-up clip keeps internal A/V sync. Voiceover,
                # music and subtitles are never affected.
                rate_audio = ""
                rate = max(0.25, min(4.0, float(getattr(item, "playback_rate", 1.0) or 1.0)))
                if abs(rate - 1.0) > 1e-6:
                    rate_audio = _atempo_chain(rate) + ","
                lines.append(
                    f"[{real_input[index]}:a:0]aresample=48000:async=1:first_pts=0,"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                    f"{rate_audio}volume={_number(clip_gain)},"
                    f"apad=pad_dur={_number(duration + 0.25)},atrim=duration={_number(duration)},"
                    f"asetpts=PTS-STARTPTS[{audio_label}]"
                )
            else:
                lines.append(
                    f"anullsrc=r=48000:cl=stereo:d={_number(duration)},"
                    f"atrim=duration={_number(duration)},asetpts=PTS-STARTPTS[{audio_label}]"
                )
            audio_labels.append(audio_label)

        video_chain = video_labels[0]
        chain_duration = resolved.effective_durations[0]
        for index in range(1, len(video_labels)):
            transition = resolved.transitions[index - 1]
            offset = max(0.0, chain_duration - transition)
            output = f"vx{index}"
            if transition <= 0:
                lines.append(
                    f"[{video_chain}][{video_labels[index]}]concat=n=2:v=1:a=0,"
                    f"fps={resolved.fps_expr}:round=near,settb=AVTB[{output}]"
                )
            else:
                lines.append(
                    f"[{video_chain}][{video_labels[index]}]xfade=transition=custom:"
                    f"duration={_number(transition)}:offset={_number(offset)}:"
                    f"expr='{dissolve_expression}',"
                    f"fps={resolved.fps_expr}:round=near,settb=AVTB[{output}]"
                )
            video_chain = output
            chain_duration += resolved.effective_durations[index] - transition

        audio_chain = audio_labels[0]
        for index in range(1, len(audio_labels)):
            transition = resolved.transitions[index - 1]
            output = f"ax{index}"
            if transition <= 0:
                lines.append(
                    f"[{audio_chain}][{audio_labels[index]}]concat=n=2:v=0:a=1[{output}]"
                )
            else:
                lines.append(
                    f"[{audio_chain}][{audio_labels[index]}]acrossfade=d={_number(transition)}:"
                    f"c1=tri:c2=tri[{output}]"
                )
            audio_chain = output

        # Stage-1 visual finishing remains in this single render graph: ASS
        # burn-in and image overlay do not create another lossy encode.
        visual_label = "vprogram"
        lines.append(
            f"[{video_chain}]trim=duration={_number(resolved.expected_duration)},"
            f"setpts=PTS-STARTPTS,format=yuv420p,setsar=1[{visual_label}]"
        )
        if (
            settings.workflow_stage == "main" and settings.subtitle_enabled
            and settings.subtitle_ass_path
        ):
            output = "vsubtitles"
            # 1.3.0: both values are emitted UNQUOTED (see filter_escape).
            # Under the project-root working directory they are plain ASCII
            # relative paths; the absolute fallback is two-level escaped.
            fonts_option = (
                f":fontsdir={_filter_path(settings.subtitle_fonts_dir)}"
                if settings.subtitle_fonts_dir else ""
            )
            lines.append(
                f"[{visual_label}]subtitles=filename={_filter_path(settings.subtitle_ass_path)}"
                f"{fonts_option}:charenc=UTF-8[{output}]"
            )
            visual_label = output
        if watermark_index is not None:
            wm_width = max(16, round(width * max(2, min(35, settings.watermark_size)) / 100 / 2) * 2)
            opacity = max(0.0, min(1.0, settings.watermark_opacity / 100.0))
            margin = max(2, round(min(width, height) * max(0, min(15, settings.watermark_margin)) / 100))
            wm = "watermark"
            lines.append(
                f"[{watermark_index}:v:0]scale=w={wm_width}:h=-1:flags=lanczos,"
                f"format=rgba,colorchannelmixer=aa={_number(opacity)}[{wm}]"
            )
            position = settings.watermark_position
            x = str(margin) if position in {"top_left", "bottom_left"} else f"W-w-{margin}"
            y = str(margin) if position in {"top_left", "top_right"} else f"H-h-{margin}"
            enable = ""
            if settings.workflow_stage == "outro":
                # The final section (outro) starts fully visible after the last
                # transition ends; keep the watermark off during intro/main.
                start = max(0.0, sum(resolved.effective_durations[:-1]) - sum(resolved.transitions))
                enable = f":enable='gte(t,{_number(start)})'"
            output = "vwatermark"
            lines.append(
                f"[{visual_label}][{wm}]overlay=x={x}:y={y}:shortest=1{enable}[{output}]"
            )
            visual_label = output
        lines.append(
            f"[{visual_label}]fps={resolved.fps_expr}:round=near,format=yuv420p,setsar=1[vout]"
        )

        # Audio in Stage 1 is explicitly tied to the resolved visual timeline.
        # Voiceover is never looped. Music is looped at input level, then
        # trimmed at the spoken-program boundary and padded with silence for
        # the configurable final pause.
        final_audio = audio_chain
        if settings.workflow_stage == "main":
            target = resolved.expected_duration
            program = min(target, settings.program_duration or target)
            lines.append(
                f"[{audio_chain}]atrim=duration={_number(program)},"
                f"apad=pad_dur={_number(target + 0.25)},atrim=duration={_number(target)}[original_main]"
            )
            mix_labels = ["original_main"]
            voice_mix = None
            voice_side = None
            if voice_indices:
                # Multiple sequential voiceover units are concatenated in their
                # exact active project order before the single shared mix. A
                # voiceover is never looped and never overlaps the next unit.
                prepared_labels: list[str] = []
                for index in voice_indices:
                    label = f"vu{index}"
                    prepared_labels.append(f"[{label}]")
                    lines.append(
                        f"[{index}:a:0]aresample=48000:async=1:first_pts=0,"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{label}]"
                    )
                if len(prepared_labels) == 1:
                    voice_chain = prepared_labels[0]
                else:
                    joined_in = "".join(prepared_labels)
                    lines.append(
                        f"{joined_in}concat=n={len(prepared_labels)}:v=0:a=1[vvoice_all]"
                    )
                    voice_chain = "[vvoice_all]"
                voice_gain = _percent_gain(settings.voiceover_volume)
                lines.append(
                    f"{voice_chain}volume={_number(voice_gain)},atrim=duration={_number(program)},"
                    f"apad=pad_dur={_number(target + 0.25)},atrim=duration={_number(target)}[voice_pre]"
                )
                if music_index is not None and settings.ducking_enabled:
                    lines.append("[voice_pre]asplit=2[voice_mix][voice_side]")
                    voice_mix, voice_side = "voice_mix", "voice_side"
                else:
                    voice_mix = "voice_pre"
                mix_labels.append(voice_mix)
            if music_index is not None:
                music_gain = _percent_gain(settings.music_volume)
                lines.append(
                    f"[{music_index}:a:0]aresample=48000:async=1:first_pts=0,"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                    f"volume={_number(music_gain)},atrim=duration={_number(program)},"
                    f"apad=pad_dur={_number(target + 0.25)},atrim=duration={_number(target)}[music_pre]"
                )
                music_label = "music_pre"
                if voice_side is not None:
                    attack = max(1, min(2000, settings.ducking_attack_ms))
                    release = max(10, min(9000, settings.ducking_release_ms))
                    lines.append(
                        f"[music_pre][{voice_side}]sidechaincompress=threshold=0.025:ratio=8:"
                        f"attack={attack}:release={release}:makeup=1[ducked_music]"
                    )
                    music_label = "ducked_music"
                mix_labels.append(music_label)
            if len(mix_labels) > 1:
                joined = "".join(f"[{label}]" for label in mix_labels)
                lines.append(
                    f"{joined}amix=inputs={len(mix_labels)}:normalize=0:dropout_transition=0,"
                    f"alimiter=limit=0.95:attack=5:release=50[mixed_main]"
                )
                final_audio = "mixed_main"
            else:
                final_audio = mix_labels[0]

        if settings.normalize_audio:
            lines.append(
                f"[{final_audio}]loudnorm=I=-16:LRA=11:TP=-1.5:linear=true,"
                f"aresample=48000:async=1,atrim=duration={_number(resolved.expected_duration)}[aout]"
            )
        else:
            lines.append(
                f"[{final_audio}]aresample=48000:async=1,"
                f"atrim=duration={_number(resolved.expected_duration)}[aout]"
            )
        # Keep the graph as one argument without shell quoting or script-file
        # options. Python's subprocess list preserves this Unicode argument on
        # Windows, including its brackets, commas and embedded expressions.
        return ";".join(lines)

    def build(
        self,
        media: MediaSequence,
        settings: ExportSettings,
        resolved: ResolvedExport,
        output_path: Path,
    ) -> BuiltCommand:
        graph = self.build_filter_graph(media, settings, resolved)
        command = [self.ffmpeg_path, "-hide_banner", "-y"]
        for item in media:
            # 1.2.4: Erzeugte Quote-Karten werden im Graph generiert und
            # erhalten keinen -i-Input.
            if item.is_generated_quote:
                continue
            # Full-timeline looping is represented by repeated occurrences in
            # ``media``. Never stream-loop an individual final clip: doing so
            # produced the incorrect 1.2.0 behavior and skipped loop-boundary
            # transitions.
            command += ["-i", str(item.path)]
        voice_inputs = list(getattr(settings, "voiceover_paths", None) or [])
        if not voice_inputs and settings.voiceover_path:
            voice_inputs = [settings.voiceover_path]
        if settings.workflow_stage == "main" and voice_inputs:
            for voice_input in voice_inputs:
                command += ["-i", voice_input]
        if settings.workflow_stage == "main" and settings.music_path:
            command += ["-stream_loop", "-1", "-i", settings.music_path]
        if _watermark_active(settings):
            command += ["-loop", "1", "-i", settings.watermark_path]
        command += [
            "-filter_complex", graph,
            "-map", "[vout]", "-map", "[aout]",
            *encoder_arguments(resolved.encoder, resolved.crf, resolved.preset),
            "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            "-metadata:s:v:0", "rotate=0",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-max_muxing_queue_size", "4096",
            "-progress", "pipe:1", "-nostats",
            str(output_path),
        ]
        return BuiltCommand(command=command, filter_graph=graph)
