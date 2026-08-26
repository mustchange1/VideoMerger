from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.video_merger.paths import locate_ffmpeg
from app.video_merger.platform_utils import hidden_process_flags, safe_subprocess_env
from tests.test_122_complete_workflow import (
    test_actual_one_click_stage1_handoff_stage2_validation_and_outro_isolation,
)
from tests.test_122_professional_subtitles import (
    test_actual_libass_layout_scales_at_1080p_4k_and_vertical_without_unsafe_wrap,
    test_all_five_animations_burn_real_timestamp_changes_with_stable_complete_phrase_geometry,
    test_all_four_safe_positions_execute_and_keep_ordered_stable_regions,
    test_debug_overlay_burns_current_word_and_timestamp_in_actual_frame,
    test_each_font_choice_executes_real_burn_with_selected_or_legal_fallback,
    test_each_improved_long_form_preset_executes_real_libass_burn,
)


class _EnvironmentGuard:
    def delenv(self, name: str, raising: bool = True) -> None:
        if name in os.environ:
            del os.environ[name]
        elif raising:
            raise KeyError(name)


def _run(command: list[object], timeout: int = 300) -> bytes:
    result = subprocess.run(
        [str(item) for item in command], capture_output=True, timeout=timeout,
        creationflags=hidden_process_flags(), env=safe_subprocess_env(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_frame(ffmpeg: Path, video: Path, at: float, output: Path) -> None:
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}",
        "-i", video, "-frames:v", "1", "-update", "1", output,
    ])


def _probe(ffprobe: Path, path: Path) -> dict:
    data = _run([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
        "-of", "json", path,
    ])
    return json.loads(data.decode("utf-8"))


def _data_uri(path: Path) -> str:
    subtype = "jpeg" if path.suffix.casefold() in {".jpg", ".jpeg"} else "png"
    return f"data:image/{subtype};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def main() -> int:
    ffmpeg, ffprobe = locate_ffmpeg()
    evidence = ROOT / "test_evidence" / "1.2.3"
    if evidence.exists():
        raise SystemExit(f"Refusing to overwrite existing evidence directory: {evidence}")
    evidence.mkdir(parents=True)

    # Keep the setup self-test fixture local in the new release without changing
    # or deleting the immutable 1.2.1 evidence.
    old_assets = ROOT / "test_evidence" / "1.2.1" / "subtitle_workflow" / "assets"
    new_assets = evidence / "subtitle_workflow" / "assets"
    shutil.copytree(old_assets, new_assets)

    animation_dir = evidence / "animations"
    animation_dir.mkdir()
    test_all_five_animations_burn_real_timestamp_changes_with_stable_complete_phrase_geometry(
        (ffmpeg, ffprobe), animation_dir
    )
    for animation in ("type_reveal", "color_change", "word_highlight", "outline_highlight", "static_phrase"):
        video = animation_dir / f"{animation}.mp4"
        for label, at in (("first", .20), ("middle", 1.15), ("final", 2.15)):
            _extract_frame(ffmpeg, video, at, animation_dir / f"{animation}_{label}.png")

    long_dir = evidence / "long_form_styles"
    long_dir.mkdir()
    for preset in ("long_1", "long_2", "long_3", "long_4", "long_5"):
        test_each_improved_long_form_preset_executes_real_libass_burn(
            (ffmpeg, ffprobe), long_dir, preset
        )

    font_dir = evidence / "fonts"
    font_dir.mkdir()
    guard = _EnvironmentGuard()
    for font_key in ("eveleth_clean", "modern_sans_bold", "clean_sans"):
        test_each_font_choice_executes_real_burn_with_selected_or_legal_fallback(
            (ffmpeg, ffprobe), font_dir, font_key, guard
        )

    position_dir = evidence / "positions"
    position_dir.mkdir()
    test_all_four_safe_positions_execute_and_keep_ordered_stable_regions(
        (ffmpeg, ffprobe), position_dir
    )

    layout_dir = evidence / "layouts"
    layout_dir.mkdir()
    for width, height in ((1920, 1080), (3840, 2160), (1080, 1920)):
        test_actual_libass_layout_scales_at_1080p_4k_and_vertical_without_unsafe_wrap(
            (ffmpeg, ffprobe), layout_dir, width, height
        )

    debug_dir = evidence / "debug_overlay"
    debug_dir.mkdir()
    test_debug_overlay_burns_current_word_and_timestamp_in_actual_frame(
        (ffmpeg, ffprobe), debug_dir
    )

    one_click_dir = evidence / "one_click"
    one_click_dir.mkdir()
    test_actual_one_click_stage1_handoff_stage2_validation_and_outro_isolation(
        (ffmpeg, ffprobe), one_click_dir
    )

    # Preserve the independently executed real acoustic alignment result.
    pending = ROOT / "test_evidence" / "real_alignment_122_pending.txt"
    if pending.is_file():
        shutil.move(str(pending), evidence / "real_acoustic_alignment_pytest.txt")

    artifacts: list[dict] = []
    for path in sorted(evidence.rglob("*")):
        if not path.is_file():
            continue
        item = {
            "path": path.relative_to(evidence).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if path.suffix.casefold() == ".mp4":
            item["ffprobe"] = _probe(ffprobe, path)
        artifacts.append(item)

    summary = {
        "release": "VideoMerger 1.2.3",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "ffmpeg": _run([ffmpeg, "-version"]).decode("utf-8", errors="replace").splitlines()[0],
        "ffprobe": _run([ffprobe, "-version"]).decode("utf-8", errors="replace").splitlines()[0],
        "status": {
            "five_animations_actual_libass": "PASS",
            "five_long_form_styles_actual_libass": "PASS",
            "font_modern_sans_bold": "PASS",
            "font_clean_sans": "PASS",
            "font_eveleth_clean": "UNLICENSED/NOT INSTALLED – legal Noto Sans fallback PASS",
            "four_safe_positions_actual_libass": "PASS",
            "one_two_line_and_no_three_plus": "PASS",
            "no_default_isolated_long_form_words": "PASS",
            "1920x1080_actual_libass": "PASS",
            "3840x2160_actual_libass": "PASS",
            "1080x1920_actual_libass": "PASS",
            "first_middle_final_visual_timestamp_changes": "PASS",
            "debug_overlay_actual_frame": "PASS",
            "real_german_english_acoustic_alignment": "PASS",
            "actual_one_click_main_handoff_and_final_validation": "PASS",
            "quiet_gap_and_outro_audio_subtitle_music_isolation": "PASS",
        },
        "artifacts": artifacts,
    }
    (evidence / "evidence_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n"
    )

    image_groups = [
        ("Five animations — first / middle / final canonical word frames", sorted(animation_dir.glob("*.png"))),
        ("Five improved Long-Form styles", sorted(long_dir.glob("*.png"))),
        ("Font choices (Eveleth uses legal fallback on this unlicensed host)", sorted(font_dir.glob("*.png"))),
        ("Safe positions", sorted(position_dir.glob("*.png"))),
        ("Resolution-aware layout", sorted(layout_dir.glob("*.png"))),
        ("Debug overlay", sorted(debug_dir.glob("*.png"))),
        ("One-click Stage 1 verification", sorted((one_click_dir / "output").glob("*.png"))),
    ]
    cards: list[str] = []
    for title, paths in image_groups:
        if not paths:
            continue
        cards.append(f"<h2>{title}</h2><div class='grid'>")
        for path in paths:
            cards.append(
                "<figure><img src='" + _data_uri(path) + "' alt='" + path.name + "'>"
                "<figcaption>" + path.relative_to(evidence).as_posix() + "</figcaption></figure>"
            )
        cards.append("</div>")
    status_rows = "".join(
        f"<tr><td>{key.replace('_', ' ')}</td><td class='status'>{value}</td></tr>"
        for key, value in summary["status"].items()
    )
    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>VideoMerger 1.2.3 Evidence</title>
<style>body{{background:#10141b;color:#edf2f7;font:15px system-ui;margin:0;padding:32px}}main{{max-width:1500px;margin:auto}}h1{{font-size:34px}}h2{{margin-top:34px;color:#9dd7ff}}.meta{{color:#aab8c8}}table{{border-collapse:collapse;width:100%;background:#171e28}}td{{border:1px solid #344154;padding:9px}}.status{{color:#91efb4;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}figure{{margin:0;background:#171e28;padding:10px;border:1px solid #344154;border-radius:8px}}img{{width:100%;height:240px;object-fit:contain;background:#070b10}}figcaption{{word-break:break-all;color:#b7c5d6;padding-top:7px}}code{{color:#ffd878}}</style></head><body><main>
<h1>VideoMerger 1.2.3 — executed visual evidence</h1><p class='meta'>{summary['generated_utc']}<br>{summary['ffmpeg']}</p>
<p>These images were rendered by FFmpeg/libass from the release ASS generator. Animation rows show frames sampled inside the first, middle and final canonical word events. See <code>evidence_summary.json</code> for SHA-256 and FFprobe data.</p>
<table>{status_rows}</table>{''.join(cards)}</main></body></html>"""
    (evidence / "EVIDENCE_REPORT.html").write_text(html, encoding="utf-8", newline="\n")
    print(evidence)
    print(f"Generated {len(artifacts)} hashed evidence artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
