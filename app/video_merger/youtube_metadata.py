"""1.3.0: Automatic YouTube title + description — local, free, unlimited.

Generated from the AUTHORITATIVE voiceover transcript/script whenever a final
video is successfully produced. Hard rules:

* FREE + LOCAL + UNLIMITED — no OpenAI/Claude/Gemini/paid API, no
  subscription, no per-video credits. The deterministic extractor is pure
  Python and always available offline.
* ACCURATE — every title/description element is extracted from the transcript
  itself (verbatim sentences or verbatim key phrases). Nothing is invented,
  no keyword stuffing, no fabricated numbers or claims.
* NATURAL — strong opening (the transcript's own first thought), a useful
  summary in the author's own words, the important themes as extracted key
  phrases, one natural CTA to follow the channel for more philosophical /
  spiritual / modern or topic-specific insights.
* LANGUAGE-MATCHED — German content produces German metadata, English
  content produces English metadata.
* NON-BLOCKING — a metadata problem is reported clearly and never prevents
  the video from rendering; the file simply is not written.

Optional polish: if a local Ollama daemon (https://ollama.com, running on the
user's own machine) is detected, it may rewrite the deterministic draft —
strictly validated (non-empty, sane lengths, JSON) and always with the same
transcript-only facts. If Ollama is absent, unreachable or produces anything
invalid, the deterministic local draft is used unchanged and the state is
logged. No remote/paid API is ever contacted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .platform_utils import safe_subprocess_env

GERMAN_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines",
    "einem", "einen", "und", "oder", "aber", "ist", "sind", "war", "waren",
    "wird", "werden", "wurde", "wurden", "hat", "haben", "hatte", "hatten",
    "nicht", "auch", "noch", "noch", "schon", "nur", "sich", "ihre", "ihren",
    "sein", "seine", "seinen", "sich", "sie", "er", "es", "wir", "ich", "du",
    "man", "für", "mit", "von", "vom", "zu", "zum", "zur", "auf", "aus", "bei",
    "nach", "vor", "über", "unter", "durch", "gegen", "ohne", "um", "als",
    "wie", "so", "da", "dass", "wenn", "dann", "denn", "doch", "ja", "nein",
    "mehr", "sehr", "ganz", "alle", "immer", "wieder", "etwas", " nichts",
    "können", "kann", "muss", "müssen", "soll", "sollen", "will", "wollen",
    "macht", "machen", "tut", "tun", "geht", "gehen", "kommt", "kommen",
    "gibt", "gab", "viel", "wenig", "hier", "da", "dieser", "diese", "dieses",
}
ENGLISH_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "can", "could", "should", "shall", "may", "might", "must",
    "not", "no", "nor", "so", "too", "very", "just", "only", "also", "than",
    "then", "there", "here", "when", "where", "why", "how", "what", "which",
    "who", "whom", "this", "that", "these", "those", "i", "you", "he", "she",
    "it", "we", "they", "them", "his", "her", "its", "our", "your", "their",
    "my", "me", "him", "us", "for", "with", "on", "at", "by", "to", "from",
    "of", "in", "into", "about", "over", "under", "again", "further", "once",
    "because", "as", "until", "while", "during", "before", "after", "up",
    "down", "out", "off", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "own", "same", "s", "t", "don", "now",
}
_GERMAN_MARKERS = ("ä", "ö", "ü", "ß", " der ", " die ", " und ", " nicht ",
                   " ist ", " wir ", " sich ", " auch ", " für ", " mit ", " dass ")
_SENTENCE_RE = re.compile(r"[^.!?…\n]+[.!?…]*")

TITLE_MIN = 20
TITLE_MAX = 95
DESCRIPTION_MAX = 4600


@dataclass(slots=True)
class YouTubeMetadata:
    title: str
    description: str
    language: str            # "German" | "English"
    generator: str           # "local-extractor" | "local-extractor+ollama"

    def render(self) -> str:
        return (
            f"TITLE: {self.title}\n"
            "\n"
            "DESCRIPTION:\n"
            f"{self.description}\n"
            "\n"
            f"LANGUAGE: {self.language}\n"
        )


def detect_language(text: str, preference: str = "Auto") -> str:
    """German content -> German metadata, English content -> English."""
    if preference in {"German", "English"}:
        return preference
    sample = (text or "")[:4000].lower()
    if not sample:
        return "German"
    german_hits = sum(1 for marker in _GERMAN_MARKERS if marker in f" {sample} ")
    words = re.findall(r"[a-zäöüß]+", sample)
    if not words:
        return "German"
    english_only = sum(1 for w in ("the", "and", "of", "to", "is") if f" {w} " in f" {sample} ")
    return "German" if german_hits >= max(1, english_only) else "English"


def _sentences(text: str) -> list[str]:
    result: list[str] = []
    for match in _SENTENCE_RE.finditer(text.replace("\r", "\n")):
        sentence = " ".join(match.group(0).split())
        if sentence and len(sentence) >= 8:
            result.append(sentence)
    return result


def _stopwords(language: str) -> set[str]:
    return GERMAN_STOPWORDS if language == "German" else ENGLISH_STOPWORDS


def _word_counts(sentences: list[str], language: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sentence in sentences:
        for word in re.findall(r"[\wäöüßÄÖÜ]+", sentence.lower()):
            if len(word) < 4 or word in _stopwords(language):
                continue
            counts[word] = counts.get(word, 0) + 1
    return counts


def _key_phrases(sentences: list[str], language: str, limit: int) -> list[str]:
    """Extract verbatim key phrases (2–4 content words) from the transcript."""
    counts = _word_counts(sentences, language)
    top = {word for word, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]}
    phrases: list[tuple[int, str]] = []
    for sentence in sentences:
        tokens = re.findall(r"[\wäöüßÄÖÜ]+", sentence)
        for size in (3, 2):
            index = 0
            while index + size <= len(tokens):
                window = tokens[index:index + size]
                lowered = [token.lower() for token in window]
                if all(token in _stopwords(language) or len(token) < 4 for token in lowered):
                    index += 1
                    continue
                if any(token in top for token in lowered):
                    score = sum(counts.get(token, 0) for token in lowered)
                    phrases.append((score, " ".join(window)))
                index += size if size == 2 else 1
    seen: set[str] = set()
    ordered: list[str] = []
    for _score, phrase in sorted(phrases, key=lambda pair: -pair[0]):
        key = phrase.lower()
        if key in seen or any(key in chosen or chosen in key for chosen in seen):
            continue
        seen.add(key)
        ordered.append(phrase)
        if len(ordered) >= limit:
            break
    return ordered


def _title_from(sentences: list[str]) -> str:
    for sentence in sentences[:4]:
        cleaned = sentence.strip().strip("\"'“”„").rstrip(".!?…").strip()
        if TITLE_MIN <= len(cleaned) <= TITLE_MAX:
            return cleaned
    if sentences:
        cleaned = sentences[0].strip().strip("\"'“”„").rstrip(".!?…").strip()
        if len(cleaned) > TITLE_MAX:
            cut = cleaned[:TITLE_MAX]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            cleaned = cut.rstrip(",;:–-")
        return cleaned
    return ""


def build_metadata(transcript: str, language: str) -> YouTubeMetadata:
    """Deterministic, transcript-only title + description (always available)."""
    text = " ".join((transcript or "").split())
    sentences = _sentences(text)
    if not sentences:
        raise ValueError("Das Voiceover-Transkript ist zu kurz für YouTube-Metadaten.")
    title = _title_from(sentences)

    # Strong opening: the transcript's own first thought (verbatim, bounded).
    opening = sentences[0]
    if len(opening) > 240:
        cut = opening[:240]
        opening = cut[: cut.rfind(" ")].rstrip(",;:–-") if " " in cut else cut

    # Useful summary: salient verbatim sentences spread across the transcript
    # (earlier sentences rank higher; duplicates by word overlap are skipped).
    counts = _word_counts(sentences, language)
    scored = []
    for index, sentence in enumerate(sentences[1:], start=1):
        words = [w for w in re.findall(r"[\wäöüßÄÖÜ]+", sentence.lower())
                 if len(w) >= 4 and w not in _stopwords(language)]
        if not words:
            continue
        score = sum(counts.get(w, 0) for w in words) / max(1, len(words))
        scored.append((score - index * 0.05, index, sentence))
    chosen: list[str] = []
    for _score, _index, sentence in sorted(scored, key=lambda item: (-item[0], item[1])):
        lower = " ".join(re.findall(r"[\wäöüßÄÖÜ]+", sentence.lower()))
        if any(lower in kept or kept in lower for kept in
               [" ".join(re.findall(r"[\wäöüßÄÖÜ]+", s.lower())) for s in chosen]):
            continue
        chosen.append(sentence)
        if len(chosen) >= 3:
            break

    # Important themes: verbatim key phrases from the transcript.
    themes = _key_phrases(sentences, language, 6)

    if language == "German":
        lines = [opening, ""]
        lines.append("Worum es in diesem Video geht:")
        if chosen:
            lines.extend(f"– {sentence}" for sentence in chosen)
        else:
            lines.append(f"– {sentences[1] if len(sentences) > 1 else sentences[0]}")
        if themes:
            lines += ["", "Themen & Momente:"]
            lines.extend(f"· {phrase}" for phrase in themes)
        lines += [
            "",
            "Das komplette Video lohnt sich – wenn solche Gedanken für dich "
            "wertvoll sind, folge dem Kanal für weitere philosophische, "
            "spirituelle und moderne Einblicke zu diesem Thema.",
        ]
    else:
        lines = [opening, ""]
        lines.append("What this video is about:")
        if chosen:
            lines.extend(f"- {sentence}" for sentence in chosen)
        else:
            lines.append(f"- {sentences[1] if len(sentences) > 1 else sentences[0]}")
        if themes:
            lines += ["", "Topics & moments:"]
            lines.extend(f"* {phrase}" for phrase in themes)
        lines += [
            "",
            "The full video is worth your time — if these ideas resonate with "
            "you, follow the channel for more philosophical, spiritual and "
            "modern insights on this topic.",
        ]
    description = "\n".join(lines).strip()
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX].rsplit("\n", 1)[0]
    return YouTubeMetadata(title=title, description=description,
                           language=language, generator="local-extractor")


# --------------------------------------------------------------------------- #
# Optional local Ollama polish (free, local, unlimited — never required)
# --------------------------------------------------------------------------- #


def _ollama_models(endpoint: str, timeout: float = 1.5) -> list[str]:
    try:
        with urlopen(Request(f"{endpoint}/api/tags"), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return [str(model.get("name", "")).split(":")[0]
                for model in payload.get("models", []) if model.get("name")]
    except (OSError, URLError, ValueError):
        return []


def _ollama_generate(endpoint: str, model: str, prompt: str, timeout: float) -> str:
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.4},
    }).encode("utf-8")
    request = Request(f"{endpoint}/api/generate", data=body,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return str(payload.get("response", ""))


def try_ollama_polish(metadata: YouTubeMetadata, transcript: str,
                      endpoint: str = "http://127.0.0.1:11434",
                      timeout: float = 45.0) -> YouTubeMetadata | None:
    """Ask a locally running Ollama to polish the draft. Never invents facts.

    Returns None (and the caller keeps the deterministic draft) when Ollama is
    unavailable or its answer fails validation.
    """
    models = _ollama_models(endpoint)
    if not models:
        return None
    model = next((name for name in models if name in {"llama3.1", "llama3", "qwen2.5", "mistral", "gemma2"}), models[0])
    language_note = "German" if metadata.language == "German" else "English"
    prompt = (
        "You improve YouTube metadata. Use ONLY facts that appear in the "
        f"transcript below. Answer in {language_note}. Return ONLY JSON with "
        'the keys "title" and "description". Title: one natural, honest line, '
        "max 90 characters. Description: 400-900 characters, natural human "
        "writing, accurate summary, the important themes, and one short natural "
        "call to follow the channel for more philosophical/spiritual or "
        "topic-specific insights. No invented facts, no keyword stuffing.\n\n"
        f"TRANSCRIPT:\n{transcript[:6000]}\n\n"
        f"DRAFT TITLE: {metadata.title}\nDRAFT DESCRIPTION:\n{metadata.description}\n"
    )
    try:
        raw = _ollama_generate(endpoint, model, prompt, timeout)
    except (OSError, URLError, ValueError):
        return None
    try:
        payload = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except (ValueError, IndexError):
        return None
    title = str(payload.get("title", "")).strip()
    description = str(payload.get("description", "")).strip()
    if not (10 <= len(title) <= 100) or len(description) < 120:
        return None
    return YouTubeMetadata(title=title, description=description[:DESCRIPTION_MAX],
                           language=metadata.language,
                           generator=f"local-extractor+ollama({model})")


def generate_youtube_metadata_file(
    transcript: str,
    output_path: Path,
    language_preference: str = "Auto",
    log=lambda message: None,
    use_ollama: bool = True,
) -> Path:
    """Create the <FinalVideo>_YouTube.txt file. Raises on empty transcripts.

    Never blocks rendering by itself: callers wrap this in a try/except and
    report the problem instead of failing the video (see MainProjectEngine).
    """
    language = detect_language(transcript, language_preference)
    metadata = build_metadata(transcript, language)
    if use_ollama:
        polished = None
        try:
            polished = try_ollama_polish(metadata, transcript)
        except Exception as exc:  # never let optional polish break anything
            log(f"YouTube metadata: lokale Ollama-Verbesserung fehlgeschlagen ({exc}); "
                "deterministischer lokaler Entwurf wird verwendet.")
        if polished is not None:
            metadata = polished
            log(f"YouTube metadata: lokal durch Ollama verbessert ({metadata.generator}).")
        else:
            log("YouTube metadata: Ollama nicht verfügbar – deterministischer lokaler "
                "Entwurf (frei, lokal, unbegrenzt) wird verwendet.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(metadata.render(), encoding="utf-8", newline="\n")
    log(f"YouTube-Metadaten erstellt: {output_path.name} (Sprache: {language})")
    return output_path
