from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable

from .errors import VideoMergerError
from .models import AlignmentResult, WordTiming
from .paths import project_root

_WORD_RE = re.compile(r"[\wÄÖÜäöüß]+(?:[’'][\wÄÖÜäöüß]+)*", re.UNICODE)
# Mapping semantics include retained fallback words; invalidate older caches that
# intentionally omitted unmatched script words.
_CACHE_SCHEMA = 3


@dataclass(slots=True)
class RecognizedWord:
    text: str
    start: float
    end: float
    confidence: float = 1.0


def _normalize(text: str) -> str:
    return re.sub(r"[^\wäöüß']+", "", text.casefold().replace("’", "'"), flags=re.UNICODE)


def script_word_spans(script: str) -> list[tuple[str, int, int]]:
    """Return exact script word spans, extending through attached punctuation."""
    matches = list(_WORD_RE.finditer(script))
    result: list[tuple[str, int, int]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(script)
        end = match.end()
        while end < next_start and not script[end].isspace():
            end += 1
        result.append((script[match.start():end], match.start(), end))
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class LocalWordAligner:
    """Local acoustic word timing mapped onto authoritative script wording.

    faster-whisper runs on the assigned voiceover only; video clips are never
    passed to ASR. Acoustic word boundaries drive timing while the exact script
    drives displayed spelling, punctuation and case. Two persistent caches are
    deliberately separated: transcription depends on voice/model/language,
    while script mapping also depends on the exact script. A style change uses
    the canonical alignment cache and cannot rerun speech recognition.
    """

    _models: dict[tuple[str, str, str], object] = {}
    _model_lock = threading.Lock()
    _fingerprints: dict[tuple[str, int, int], str] = {}
    _fingerprint_lock = threading.Lock()
    _cache_lock = threading.Lock()

    def __init__(
        self,
        model_name: str = "small",
        recognizer: Callable[[Path, str | None], tuple[list[RecognizedWord], str]] | None = None,
        cache_dir: Path | None = None,
        use_cache: bool = True,
    ):
        self.model_name = model_name
        self._recognizer = recognizer
        self.cache_dir = cache_dir or (project_root() / "cache" / "alignment")
        # Injected recognizers are deterministic test/extension hooks. Avoid
        # allowing their synthetic output to contaminate the production cache.
        self.use_cache = bool(use_cache and recognizer is None)
        self.last_timings: dict[str, float | str | bool] = {}

    def _audio_fingerprint(self, audio_path: Path) -> str:
        path = audio_path.expanduser().resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        with self._fingerprint_lock:
            cached = self._fingerprints.get(key)
        if cached:
            return cached
        value = _sha256_file(path)
        with self._fingerprint_lock:
            self._fingerprints[key] = value
        return value

    def _cache_read(self, category: str, key: str) -> dict | None:
        if not self.use_cache:
            return None
        path = self.cache_dir / category / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") != _CACHE_SCHEMA:
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def _cache_write(self, category: str, key: str, data: dict) -> None:
        if not self.use_cache:
            return
        path = self.cache_dir / category / f"{key}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{threading.get_ident()}.tmp")
            temporary.write_text(
                json.dumps({"schema": _CACHE_SCHEMA, **data}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            with self._cache_lock:
                temporary.replace(path)
        except OSError:
            # A cache is an optimization, never a prerequisite for subtitles.
            try:
                temporary.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass

    def _model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VideoMergerError(
                "Lokale Wortausrichtung ist nicht installiert. Bitte setup_windows.ps1 erneut ausführen."
            ) from exc
        key = (self.model_name, "cpu", "int8")
        with self._model_lock:
            model = self._models.get(key)
            if model is None:
                started = time.perf_counter()
                try:
                    model = WhisperModel(
                        self.model_name, device="cpu", compute_type="int8",
                        cpu_threads=max(2, min(8, os.cpu_count() or 4)), num_workers=1,
                    )
                except Exception as exc:
                    raise VideoMergerError(
                        f"Das lokale Whisper-Modell '{self.model_name}' konnte nicht geladen werden: {exc}"
                    ) from exc
                self._models[key] = model
                self.last_timings["model_loading_seconds"] = time.perf_counter() - started
            else:
                self.last_timings["model_loading_seconds"] = 0.0
        return model

    def _recognize(self, audio_path: Path, language: str | None) -> tuple[list[RecognizedWord], str]:
        if self._recognizer:
            started = time.perf_counter()
            result = self._recognizer(audio_path, language)
            self.last_timings["transcription_seconds"] = time.perf_counter() - started
            self.last_timings["model_loading_seconds"] = 0.0
            return result
        model = self._model()
        started = time.perf_counter()
        try:
            # beam_size=3 materially reduces CPU work from 1.2.0's beam 5 while
            # preserving robust local word timestamps. The supplied script is
            # still forced onto these acoustic boundaries afterwards.
            segments, info = model.transcribe(
                str(audio_path), language=language, beam_size=3,
                word_timestamps=True, vad_filter=True,
                condition_on_previous_text=True,
            )
            words: list[RecognizedWord] = []
            for segment in segments:
                for word in segment.words or []:
                    if word.start is None or word.end is None or not word.word.strip():
                        continue
                    words.append(RecognizedWord(
                        word.word.strip(), float(word.start), float(word.end),
                        float(getattr(word, "probability", 1.0) or 0.0),
                    ))
            self.last_timings["transcription_seconds"] = time.perf_counter() - started
            return words, str(getattr(info, "language", language or "auto"))
        except VideoMergerError:
            raise
        except Exception as exc:
            raise VideoMergerError(f"Lokale Voiceover-Ausrichtung fehlgeschlagen: {exc}") from exc

    @staticmethod
    def _result_from_cache(data: dict) -> AlignmentResult:
        return AlignmentResult(
            words=[WordTiming(**word) for word in data["words"]],
            language=str(data["language"]), method=str(data["method"]),
            compatibility=float(data["compatibility"]),
            average_confidence=float(data["average_confidence"]),
            warnings=[str(item) for item in data.get("warnings", [])],
            hard_breaks=[float(item) for item in data.get("hard_breaks", [])],
        )

    def _transcription_for(
        self, path: Path, language_code: str | None,
    ) -> tuple[list[RecognizedWord], str]:
        """Return (recognized words, detected language) using the cache."""
        audio_sha = self._audio_fingerprint(path) if self.use_cache else "custom-recognizer"
        transcription_key = _json_digest({
            "schema": _CACHE_SCHEMA, "audio_sha256": audio_sha,
            "model": self.model_name, "language": language_code,
        })
        transcription = self._cache_read("transcriptions", transcription_key)
        if transcription is not None:
            self.last_timings.update({"cache_hit": True, "cache_level": "transcription"})
            recognized = [RecognizedWord(**word) for word in transcription["words"]]
            return recognized, str(transcription["detected_language"])
        recognized, detected = self._recognize(path, language_code)
        self._cache_write("transcriptions", transcription_key, {
            "detected_language": detected,
            "words": [asdict(word) for word in recognized],
        })
        return recognized, detected

    def recognize(self, audio_path: Path, language: str = "German") -> tuple[list[RecognizedWord], str]:
        """Transcribe one voiceover file and return its acoustic word stream.

        Used by the multi-voiceover single-global-script mode, where each unit
        is transcribed once (and cached) and the supplied global script is then
        forced onto the concatenated acoustic timeline.
        """
        started_total = time.perf_counter()
        self.last_timings = {
            "model_loading_seconds": 0.0,
            "transcription_seconds": 0.0,
            "forced_mapping_seconds": 0.0,
            "cache_hit": False,
            "cache_level": "none",
        }
        language_code = {"German": "de", "English": "en", "Auto": None}.get(language)
        if language not in {"German", "English", "Auto"}:
            raise VideoMergerError(f"Unbekannte Untertitelsprache: {language}")
        path = audio_path.expanduser().resolve()
        if not path.is_file() and self._recognizer is None:
            raise VideoMergerError(f"Voiceover für Wortausrichtung fehlt: {path}")
        try:
            words, detected = self._transcription_for(path, language_code)
            self.last_timings["total_alignment_seconds"] = time.perf_counter() - started_total
            return words, detected
        except VideoMergerError:
            raise
        except Exception as exc:
            raise VideoMergerError(f"Lokale Voiceover-Ausrichtung fehlgeschlagen: {exc}") from exc

    def align_global(
        self,
        script: str,
        units: Iterable[tuple[Path, float]],
        language: str = "German",
        inter_unit_pause: float = 0.7,
    ) -> AlignmentResult:
        """Map one script onto an ordered multi-voiceover acoustic timeline.

        Each source transcription remains independently cacheable because
        faster-whisper accepts files, but the script mapping itself happens
        exactly once over the logical concatenated timeline. The cache key
        includes the ordered source identities, their durations, and the real
        inter-unit pause, so a pause/order change cannot reuse stale timestamps.
        """
        started_total = time.perf_counter()
        self.last_timings = {
            "model_loading_seconds": 0.0,
            "transcription_seconds": 0.0,
            "forced_mapping_seconds": 0.0,
            "cache_hit": False,
            "cache_level": "none",
        }
        language_code = {"German": "de", "English": "en", "Auto": None}.get(language)
        if language not in {"German", "English", "Auto"}:
            raise VideoMergerError(f"Unbekannte Untertitelsprache: {language}")
        units = [(Path(path).expanduser().resolve(), float(duration)) for path, duration in units]
        if not units:
            raise VideoMergerError("Für das globale Alignment wurde kein Voiceover übergeben.")
        pause = max(0.0, min(10.0, float(inter_unit_pause)))

        # Build the complete cache identity before ASR. A cache hit therefore
        # avoids both repeated recognition and repeated global script mapping.
        transcription_keys: list[str] = []
        for path, duration in units:
            if not path.is_file() and self._recognizer is None:
                raise VideoMergerError(f"Voiceover für Wortausrichtung fehlt: {path}")
            audio_sha = self._audio_fingerprint(path) if self.use_cache else "custom-recognizer"
            transcription_keys.append(_json_digest({
                "schema": _CACHE_SCHEMA,
                "audio_sha256": audio_sha,
                "model": self.model_name,
                "language": language_code,
            }))
        alignment_key = _json_digest({
            "schema": _CACHE_SCHEMA,
            "transcriptions": transcription_keys,
            "durations": [duration for _path, duration in units],
            "inter_unit_pause": pause,
            "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        })
        cached_alignment = self._cache_read("alignments", alignment_key)
        if cached_alignment is not None:
            self.last_timings.update({
                "cache_hit": True,
                "cache_level": "alignment",
                "total_alignment_seconds": time.perf_counter() - started_total,
            })
            return self._result_from_cache(cached_alignment)

        recognized_all: list[RecognizedWord] = []
        detected_languages: list[str] = []
        time_cursor = 0.0
        try:
            for unit_index, (path, duration) in enumerate(units):
                recognized, detected = self._transcription_for(path, language_code)
                recognized_all.extend(
                    RecognizedWord(
                        word.text,
                        word.start + time_cursor,
                        word.end + time_cursor,
                        word.confidence,
                    )
                    for word in recognized
                )
                detected_languages.append(detected)
                time_cursor += duration
                if unit_index < len(units) - 1:
                    time_cursor += pause
            mapping_started = time.perf_counter()
            result = self.align_from_recognized(
                script,
                recognized_all,
                detected_languages[0] if detected_languages else language,
                fallback_end=time_cursor,
            )
            pause_breaks = [
                sum(item[1] for item in units[:unit_index]) + pause * unit_index
                for unit_index in range(1, len(units))
                if pause > 1e-9
            ]
            result.hard_breaks = sorted(set(result.hard_breaks + pause_breaks))
            self.last_timings["forced_mapping_seconds"] = time.perf_counter() - mapping_started
            self._cache_write("alignments", alignment_key, {
                "words": [asdict(word) for word in result.words],
                "language": result.language,
                "method": result.method,
                "compatibility": result.compatibility,
                "average_confidence": result.average_confidence,
                "warnings": result.warnings,
                "hard_breaks": result.hard_breaks,
            })
            self.last_timings["total_alignment_seconds"] = time.perf_counter() - started_total
            return result
        except VideoMergerError:
            raise
        except Exception as exc:
            raise VideoMergerError(f"Lokale Voiceover-Ausrichtung fehlgeschlagen: {exc}") from exc

    def align(
        self,
        script: str,
        audio_path: Path,
        language: str = "German",
        *,
        fallback_end: float | None = None,
    ) -> AlignmentResult:
        started_total = time.perf_counter()
        self.last_timings = {
            "model_loading_seconds": 0.0,
            "transcription_seconds": 0.0,
            "forced_mapping_seconds": 0.0,
            "cache_hit": False,
            "cache_level": "none",
        }
        language_code = {"German": "de", "English": "en", "Auto": None}.get(language)
        if language not in {"German", "English", "Auto"}:
            raise VideoMergerError(f"Unbekannte Untertitelsprache: {language}")
        path = audio_path.expanduser().resolve()
        if not path.is_file() and self._recognizer is None:
            raise VideoMergerError(f"Voiceover für Wortausrichtung fehlt: {path}")

        try:
            fingerprint_started = time.perf_counter()
            audio_sha = self._audio_fingerprint(path) if self.use_cache else "custom-recognizer"
            self.last_timings["fingerprint_seconds"] = time.perf_counter() - fingerprint_started
            transcription_key = _json_digest({
                "schema": _CACHE_SCHEMA, "audio_sha256": audio_sha,
                "model": self.model_name, "language": language_code,
            })
            alignment_key = _json_digest({
                "transcription": transcription_key,
                "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                "fallback_end": fallback_end,
            })

            cached_alignment = self._cache_read("alignments", alignment_key)
            if cached_alignment is not None:
                result = self._result_from_cache(cached_alignment)
                self.last_timings.update({
                    "cache_hit": True, "cache_level": "alignment",
                    "total_alignment_seconds": time.perf_counter() - started_total,
                })
                return result

            recognized, detected = self._transcription_for(path, language_code)

            mapping_started = time.perf_counter()
            result = self.align_from_recognized(
                script, recognized, detected, fallback_end=fallback_end
            )
            self.last_timings["forced_mapping_seconds"] = time.perf_counter() - mapping_started
            self._cache_write("alignments", alignment_key, {
                "words": [asdict(word) for word in result.words],
                "language": result.language, "method": result.method,
                "compatibility": result.compatibility,
                "average_confidence": result.average_confidence,
                "warnings": result.warnings,
                "hard_breaks": result.hard_breaks,
            })
            self.last_timings["total_alignment_seconds"] = time.perf_counter() - started_total
            return result
        except VideoMergerError:
            raise
        except Exception as exc:
            raise VideoMergerError(f"Lokale Voiceover-Ausrichtung fehlgeschlagen: {exc}") from exc

    def align_from_recognized(
        self,
        script: str,
        recognized: Iterable[RecognizedWord],
        detected_language: str,
        *,
        fallback_end: float | None = None,
    ) -> AlignmentResult:
        """Map authoritative script words onto acoustic anchors without loss.

        SequenceMatcher supplies lexical correspondences, and reliable ASR
        boundaries are copied verbatim for those anchors. A script word that
        is absent, low-confidence, or has unusable ASR timing is *not* dropped:
        it receives a small bounded interval interpolated from neighboring
        acoustic anchors (or from the complete acoustic span). This keeps the
        script complete while making the uncertainty explicit in warnings and
        confidence values instead of silently creating a caption gap.
        """
        spans = script_word_spans(script)
        asr = [word for word in recognized if _normalize(word.text)]
        if not spans:
            return AlignmentResult(
                words=[], language=detected_language, method="no script words",
                compatibility=1.0, average_confidence=0.0,
                warnings=["The supplied script contains no alignable words."],
            )

        script_norm = [_normalize(token) for token, _start, _end in spans]
        asr_norm = [_normalize(word.text) for word in asr]
        matcher = SequenceMatcher(a=script_norm, b=asr_norm, autojunk=False)
        lexical_mapping: dict[int, int] = {}
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                lexical_mapping[block.a + offset] = block.b + offset
        compatibility = matcher.ratio()

        # A finite positive ASR interval is still a useful acoustic anchor at
        # confidence 0.0. Confidence expresses certainty; it must not decide
        # whether a real measured start/end is discarded.
        acoustic: dict[int, tuple[float, float, float]] = {}
        for index, word in enumerate(asr):
            try:
                start = float(word.start)
                end = float(word.end)
                confidence = float(word.confidence)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(start) and math.isfinite(end) and math.isfinite(confidence)):
                continue
            if end <= start:
                continue
            start = max(0.0, start)
            acoustic[index] = (start, max(start + 0.02, end), max(0.0, confidence))

        matched_by_script: dict[int, tuple[int, WordTiming]] = {}
        matched_asr_indexes: set[int] = set()
        for script_index, asr_index in lexical_mapping.items():
            measured = acoustic.get(asr_index)
            if measured is None:
                continue
            start, end, confidence = measured
            token, char_start, char_end = spans[script_index]
            word = WordTiming(
                text=token,
                start=start,
                end=end,
                confidence=confidence,
                script_start=char_start,
                script_end=char_end,
            )
            matched_by_script[script_index] = (asr_index, word)
            matched_asr_indexes.add(asr_index)

        default_fallback = 0.24
        minimum_fallback = 0.02
        try:
            bounded_fallback_end = (
                max(0.0, float(fallback_end))
                if fallback_end is not None and math.isfinite(float(fallback_end))
                else None
            )
        except (TypeError, ValueError):
            bounded_fallback_end = None
        word_by_script: dict[int, WordTiming] = {
            index: pair[1] for index, pair in matched_by_script.items()
        }

        def fallback_words(
            indexes: list[int], left: float, right: float | None,
        ) -> None:
            """Fill one monotone script gap with a bounded local interval."""
            if not indexes:
                return
            left = max(0.0, float(left))
            if right is not None:
                right = max(0.0, float(right))
            count = len(indexes)
            if right is not None and right > left + 1e-9:
                # Start immediately after the preceding anchor. The interval
                # is bounded by the next anchor and never invents a long wait.
                slot = min(default_fallback, max(minimum_fallback, (right - left) / count))
                for offset, script_index in enumerate(indexes):
                    start = left + offset * slot
                    end = min(right, start + slot)
                    if end <= start:
                        end = start + minimum_fallback
                    token, char_start, char_end = spans[script_index]
                    word_by_script[script_index] = WordTiming(
                        text=token, start=start, end=end, confidence=0.0,
                        script_start=char_start, script_end=char_end,
                    )
                return

            # There is no usable gap between anchors (or no right anchor).
            # Keep the fallback local: a 20 ms minimum is enough for a valid
            # cue and avoids placing a missing word many seconds away.
            if right is not None:
                # No measurable interval remains. Preserve the next real
                # acoustic anchor and backfill immediately before it rather
                # than pushing a fallback word past that anchor.
                for offset, script_index in enumerate(indexes):
                    end = right - (count - offset - 1) * minimum_fallback
                    start = max(0.0, end - minimum_fallback)
                    token, char_start, char_end = spans[script_index]
                    word_by_script[script_index] = WordTiming(
                        text=token, start=start, end=max(start + minimum_fallback, end), confidence=0.0,
                        script_start=char_start, script_end=char_end,
                    )
                return
            slot = default_fallback
            for offset, script_index in enumerate(indexes):
                start = left + offset * slot
                token, char_start, char_end = spans[script_index]
                word_by_script[script_index] = WordTiming(
                    text=token, start=start,
                    end=start + slot, confidence=0.0,
                    script_start=char_start, script_end=char_end,
                )

        anchors = sorted(
            ((script_index, asr_index, word)
             for script_index, (asr_index, word) in matched_by_script.items()),
            key=lambda item: item[0],
        )
        valid_times = list(acoustic.values())
        if not anchors:
            # With no lexical anchor, retain all script words over the real
            # acoustic span. If ASR has no usable timestamps, use a clearly
            # bounded speaking-rate fallback from time zero.
            if valid_times:
                acoustic_start = min(item[0] for item in valid_times)
                acoustic_end = max(item[1] for item in valid_times)
                if bounded_fallback_end is not None:
                    acoustic_start = min(acoustic_start, bounded_fallback_end)
                    acoustic_end = min(acoustic_end, bounded_fallback_end)
                span = max(0.0, acoustic_end - acoustic_start)
                step = span / len(spans) if span > 1e-9 else default_fallback
                for index, (token, char_start, char_end) in enumerate(spans):
                    start = acoustic_start + index * step
                    end = min(acoustic_end, start + min(default_fallback, max(minimum_fallback, step)))
                    if end <= start:
                        end = start + minimum_fallback
                    word_by_script[index] = WordTiming(
                        text=token, start=start, end=end, confidence=0.0,
                        script_start=char_start, script_end=char_end,
                    )
            else:
                fallback_words(list(range(len(spans))), 0.0, bounded_fallback_end)
        else:
            first_script, first_asr, first_word = anchors[0]
            leading = list(range(0, first_script))
            earlier = [item for index, item in acoustic.items() if index < first_asr]
            leading_left = (
                min(item[0] for item in earlier)
                if earlier else max(0.0, first_word.start - len(leading) * default_fallback)
            )
            fallback_words(leading, leading_left, first_word.start)

            for previous, current in zip(anchors, anchors[1:]):
                previous_script, _previous_asr, previous_word = previous
                current_script, _current_asr, current_word = current
                missing = list(range(previous_script + 1, current_script))
                fallback_words(missing, previous_word.end, current_word.start)

            last_script, last_asr, last_word = anchors[-1]
            trailing = list(range(last_script + 1, len(spans)))
            later = [item for index, item in acoustic.items() if index > last_asr]
            trailing_right = max((item[1] for item in later), default=None)
            if trailing_right is None and bounded_fallback_end is not None and bounded_fallback_end > last_word.end:
                trailing_right = bounded_fallback_end
            fallback_words(trailing, last_word.end, trailing_right)

        # The canonical result follows the authoritative script order. ASR
        # timestamps are retained for anchors; fallback intervals are local to
        # their neighboring anchors and are never silently omitted.
        words = [word_by_script[index] for index in range(len(spans))]
        unmatched_script = len(spans) - len(matched_by_script)
        unmatched_audio_indexes = set(range(len(asr))) - matched_asr_indexes
        unmatched_audio = len(unmatched_audio_indexes)
        warnings: list[str] = []
        if compatibility < 0.72:
            warnings.append(
                "The supplied script and voiceover appear to differ; bounded fallback timing was used for "
                "unmatched script words."
            )
        if not acoustic:
            warnings.append(
                "No acoustic words with usable timestamps were recognized; all script words were retained "
                "with bounded fallback timestamps."
            )
        elif unmatched_script:
            warnings.append(
                f"Subtitle alignment warning: {unmatched_script} script word(s) had no reliable lexical "
                "match and were retained with bounded fallback timestamps."
            )
        if unmatched_audio:
            warnings.append(
                f"Subtitle alignment warning: {unmatched_audio} spoken word(s) were not present in the "
                "authoritative script or were not reliably matched."
            )
        # Mark contiguous uncaptioned acoustic runs as boundaries. Fallback
        # words still cover these intervals; the boundary only prevents a cue
        # from being held across a real silent/uncertain region.
        acoustic_gaps: list[float] = []
        index = 0
        while index < len(asr):
            if index not in unmatched_audio_indexes or index not in acoustic:
                index += 1
                continue
            first = index
            while index < len(asr) and index in unmatched_audio_indexes:
                index += 1
            last = index - 1
            valid_run = [item for item in range(first, last + 1) if item in acoustic]
            if valid_run:
                acoustic_gaps.extend((acoustic[valid_run[0]][0], acoustic[valid_run[-1]][1]))
        for previous, current in zip(anchors, anchors[1:]):
            if current[0] != previous[0] + 1 or current[1] != previous[1] + 1:
                acoustic_gaps.append(max(0.0, current[2].start))
        large_gaps = [
            right.start - left.end for left, right in zip(words, words[1:])
            if right.start - left.end > 5.0
        ]
        if large_gaps:
            warnings.append(
                f"Subtitle alignment warning: {len(large_gaps)} unusually large speech gap(s) detected."
            )
        average = sum(word.confidence for word in words) / len(words) if words else 0.0
        fallback_used = unmatched_script > 0 or not acoustic
        return AlignmentResult(
            words=words,
            language=detected_language,
            method=(
                f"faster-whisper/{self.model_name} word timestamps + script mapping"
                + (" + bounded fallback timestamps" if fallback_used else "")
            ),
            compatibility=compatibility,
            average_confidence=average,
            warnings=warnings,
            hard_breaks=sorted(set(acoustic_gaps)),
        )
