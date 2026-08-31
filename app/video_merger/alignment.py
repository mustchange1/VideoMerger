from __future__ import annotations

import hashlib
import json
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
_CACHE_SCHEMA = 2


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
            )
            result.hard_breaks = [
                sum(item[1] for item in units[:unit_index]) + pause * unit_index
                for unit_index in range(1, len(units))
                if pause > 1e-9
            ]
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

    def align(self, script: str, audio_path: Path, language: str = "German") -> AlignmentResult:
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
            result = self.align_from_recognized(script, recognized, detected)
            self.last_timings["forced_mapping_seconds"] = time.perf_counter() - mapping_started
            self._cache_write("alignments", alignment_key, {
                "words": [asdict(word) for word in result.words],
                "language": result.language, "method": result.method,
                "compatibility": result.compatibility,
                "average_confidence": result.average_confidence,
                "warnings": result.warnings,
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
    ) -> AlignmentResult:
        spans = script_word_spans(script)
        asr = [word for word in recognized if _normalize(word.text)]
        if not spans:
            raise VideoMergerError("Das Skript enthält keine ausrichtbaren Wörter.")
        if not asr:
            raise VideoMergerError("Im Voiceover wurden keine gesprochenen Wörter mit Zeitstempeln erkannt.")
        script_norm = [_normalize(token) for token, _start, _end in spans]
        asr_norm = [_normalize(word.text) for word in asr]
        matcher = SequenceMatcher(a=script_norm, b=asr_norm, autojunk=False)
        mapping: dict[int, int] = {}
        for block in matcher.get_matching_blocks():
            for offset in range(block.size):
                mapping[block.a + offset] = block.b + offset
        compatibility = matcher.ratio()

        starts: list[float | None] = [None] * len(spans)
        ends: list[float | None] = [None] * len(spans)
        confidences = [0.0] * len(spans)
        for script_index, asr_index in mapping.items():
            starts[script_index] = max(0.0, asr[asr_index].start)
            ends[script_index] = max(asr[asr_index].start + 0.02, asr[asr_index].end)
            confidences[script_index] = asr[asr_index].confidence

        # Only unmatched lexical runs are constrained between neighboring real
        # acoustic anchors. Whole-script equal division and character-count
        # estimates are never used.
        index = 0
        while index < len(spans):
            if starts[index] is not None:
                index += 1
                continue
            first = index
            while index < len(spans) and starts[index] is None:
                index += 1
            last = index - 1
            left_end = ends[first - 1] if first > 0 and ends[first - 1] is not None else None
            right_start = starts[index] if index < len(spans) and starts[index] is not None else None
            count = last - first + 1
            if left_end is None and right_start is not None:
                run_start = max(0.0, right_start - 0.28 * count)
                run_end = right_start
            elif right_start is None and left_end is not None:
                run_start = left_end
                run_end = left_end + 0.28 * count
            elif left_end is not None and right_start is not None:
                run_start, run_end = left_end, max(left_end + 0.04 * count, right_start)
            else:
                run_start, run_end = asr[0].start, asr[-1].end
            step = max(0.04, (run_end - run_start) / count)
            for offset, word_index in enumerate(range(first, last + 1)):
                starts[word_index] = run_start + offset * step
                ends[word_index] = min(run_end, run_start + (offset + 1) * step)
                confidences[word_index] = 0.0

        words: list[WordTiming] = []
        cursor = 0.0
        for idx, ((token, char_start, char_end), start, end) in enumerate(zip(spans, starts, ends)):
            actual_start = max(cursor, float(start or 0.0))
            actual_end = max(actual_start + 0.02, float(end or actual_start + 0.08))
            words.append(WordTiming(
                text=token, start=actual_start, end=actual_end,
                confidence=confidences[idx], script_start=char_start, script_end=char_end,
            ))
            cursor = actual_start + 0.001

        warnings: list[str] = []
        unmatched = len(spans) - len(mapping)
        if compatibility < 0.72:
            warnings.append(
                "The supplied script and voiceover appear to differ. Subtitle synchronization may be inaccurate."
            )
        if unmatched:
            warnings.append(
                f"Subtitle alignment warning: {unmatched} script word(s) could not be confidently matched "
                "and were constrained between neighboring acoustic timestamps."
            )
        large_gaps = [
            right.start - left.end for left, right in zip(words, words[1:])
            if right.start - left.end > 5.0
        ]
        if large_gaps:
            warnings.append(
                f"Subtitle alignment warning: {len(large_gaps)} unusually large speech gap(s) detected."
            )
        average = sum(word.confidence for word in words) / len(words)
        return AlignmentResult(
            words=words,
            language=detected_language,
            method=f"faster-whisper/{self.model_name} word timestamps + script-forced mapping",
            compatibility=compatibility,
            average_confidence=average,
            warnings=warnings,
        )
