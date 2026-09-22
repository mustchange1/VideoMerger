"""Phase 31: Smart Visual Hybrid - semantic matching & visual plan engine.

Strictly additive, opt-in feature (spec sections 4-47). When disabled
(default) nothing here runs and every historical render/cache identity stays
byte-identical.

Pipeline (stages A-E of the spec):

A. Timing reuses the canonical project data - script sentences and the
   voiceover-driven program duration. No second ASR pipeline.
B. Sentences are grouped into semantic slots (short same-topic sentences
   merge into one slot; cadence is configurable).
C. Slots are matched against a cached media index of the configured smart
   visual folders (folder name = category, optional sidecar metadata).
D. Local generation is used only according to the configured strategy and
   only through the provider abstraction in :mod:`image_generation`.
E. The selected media are inserted as genuine Phase-30-style timeline
   elements by the existing renderer - there is no second renderer.

The semantic layer is a deterministic local lexical matcher (stopword
removal, light suffix stripping, a small synonym concept map, cosine
similarity). It is the spec's "embedding or equivalent" local matching and
needs no internet and no ML dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

from .image_timeline import (
    ImageTimelineProfile,
    make_image_media,
    probe_image_size,
)

# ---------------------------------------------------------------------------
# Constants & normalization
# ---------------------------------------------------------------------------
SMART_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SMART_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

THRESHOLD_PRESETS = {"low": 0.35, "medium": 0.50, "high": 0.65}

STYLE_CHOICES = (
    "realistic",
    "cinematic",
    "editorial",
    "documentary",
    "conceptual",
    "minimal",
    "custom",
)

GENERATION_STRATEGIES = (
    "only_when_no_match",
    "every_2nd",
    "every_3rd",
    "every_4th",
    "random_25",
    "random_50",
    "custom_percent",
    "always",
)

CADENCE_CHOICES = ("adaptive", "every_1", "every_2", "every_3", "every_4")

SOURCE_PRIORITIES = ("video_first", "image_first", "best_match", "balanced")

MIN_SLOT_SECONDS = 1.0
MAX_SLOT_SECONDS = 8.0


def normalize_source_priority(value: object) -> str:
    text = str(value or "balanced").strip().casefold()
    return text if text in SOURCE_PRIORITIES else "balanced"


def normalize_threshold_mode(value: object) -> str:
    text = str(value or "medium").strip().casefold()
    return text if text in THRESHOLD_PRESETS or text == "custom" else "medium"


def clamp_threshold(value: object) -> float:
    try:
        return max(0.05, min(0.95, float(value)))
    except (TypeError, ValueError):
        return 0.50


def smart_visual_threshold(mode: str, custom: float) -> float:
    if mode == "custom":
        return clamp_threshold(custom)
    return THRESHOLD_PRESETS.get(mode, 0.50)


def normalize_generation_strategy(value: object) -> str:
    text = str(value or "only_when_no_match").strip().casefold()
    return text if text in GENERATION_STRATEGIES else "only_when_no_match"


def clamp_generation_percent(value: object) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 25


def clamp_repetition_window(value: object) -> int:
    try:
        return max(0, min(10, int(float(value))))
    except (TypeError, ValueError):
        return 3


def normalize_smart_visual_style(value: object) -> str:
    text = str(value or "cinematic").strip().casefold()
    return text if text in STYLE_CHOICES else "cinematic"


def normalize_smart_visual_cadence(value: object) -> str:
    text = str(value or "adaptive").strip().casefold()
    return text if text in CADENCE_CHOICES else "adaptive"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SmartVisualProfile:
    enabled: bool = False
    folders: tuple[str, ...] = ()
    source_priority: str = "balanced"
    threshold_mode: str = "medium"
    threshold_custom: float = 0.50
    generation_strategy: str = "only_when_no_match"
    generation_percent: int = 25
    repetition_window: int = 3
    style: str = "cinematic"
    style_custom: str = ""
    cadence: str = "adaptive"

    @property
    def active(self) -> bool:
        """Enabled AND at least one source folder configured."""
        return bool(self.enabled) and any(str(folder).strip() for folder in self.folders)

    @property
    def threshold(self) -> float:
        return smart_visual_threshold(self.threshold_mode, self.threshold_custom)


def smart_visual_profile_from_settings(settings: object) -> SmartVisualProfile:
    """Resolve the canonical per-job smart visual fields.

    ``long_form_settings()``/``short_settings()`` map each profile's own
    fields onto the canonical ones BEFORE this call, so Long-Form and Shorts
    can never leak into each other.
    """
    folders = tuple(
        str(folder).strip()
        for folder in (getattr(settings, "smart_visual_folders", None) or [])
        if str(folder).strip()
    )
    return SmartVisualProfile(
        enabled=bool(getattr(settings, "smart_visual_enabled", False)),
        folders=folders,
        source_priority=normalize_source_priority(getattr(settings, "smart_visual_source_priority", "balanced")),
        threshold_mode=normalize_threshold_mode(getattr(settings, "smart_visual_threshold_mode", "medium")),
        threshold_custom=clamp_threshold(getattr(settings, "smart_visual_threshold_custom", 0.5)),
        generation_strategy=normalize_generation_strategy(
            getattr(settings, "smart_visual_generation_strategy", "only_when_no_match")
        ),
        generation_percent=clamp_generation_percent(getattr(settings, "smart_visual_generation_percent", 25)),
        repetition_window=clamp_repetition_window(getattr(settings, "smart_visual_repetition_window", 3)),
        style=normalize_smart_visual_style(getattr(settings, "smart_visual_style", "cinematic")),
        style_custom=str(getattr(settings, "smart_visual_style_custom", "") or ""),
        cadence=normalize_smart_visual_cadence(getattr(settings, "smart_visual_cadence", "adaptive")),
    )


# ---------------------------------------------------------------------------
# Semantic layer (deterministic local lexical matching)
# ---------------------------------------------------------------------------
STOPWORDS = frozenset("""
a an and are as at be but by for from has have he her his i if in into is it
its of on or our she so that the their them then there these they this to was
we were what when which who will with you your not no do does did can could
should would may might must also very just about over under between after
before during while how why all any each more most other some such only own
same than too up down out off again once here now because through against
der die das und oder aber ein eine einen einem einer ist sind war waren wird
werden hat haben hatte mit von zu zur zum auf aus bei nach für im in am an
als auch wie wenn dann dass sie er es ihr ihre ich wir uns unser euch euer
diese dieser dieses kein keine nicht nur noch schon sehr zum über unter
""".split())

#: Small local concept map: canonical concept -> surface words. Words are
#: matched after stopword removal/suffix stripping; both English and German
#: surface forms resolve to the same concept vector dimension.
SYNONYM_CONCEPTS: dict[str, tuple[str, ...]] = {
    "city": ("city", "cities", "town", "street", "urban", "downtown", "skyline", "stadt", "strasse", "gasse"),
    "nature": ("nature", "forest", "tree", "trees", "leaf", "plant", "wald", "baum", "natur", "wiese"),
    "mountain": ("mountain", "mountains", "peak", "alps", "hill", "berg", "gipfel", "alp"),
    "water": ("water", "river", "lake", "sea", "ocean", "wave", "fluss", "see", "meer", "wasser", "welle"),
    "sky": ("sky", "cloud", "clouds", "sunset", "sunrise", "star", "stars", "himmel", "wolke", "sonnenuntergang"),
    "weather": ("weather", "rain", "snow", "storm", "wind", "regen", "schnee", "sturm", "wetter"),
    "people": ("people", "person", "man", "woman", "child", "children", "family", "crowd", "mensch", "familie", "kind"),
    "work": ("work", "office", "job", "factory", "worker", "arbeit", "büro", "fabrik"),
    "technology": ("technology", "computer", "software", "robot", "digital", "tech", "technologie", "computer"),
    "science": ("science", "research", "laboratory", "experiment", "wissenschaft", "forschung", "labor"),
    "history": ("history", "ancient", "castle", "ruin", "museum", "geschichte", "burg", "museum"),
    "food": ("food", "meal", "cooking", "kitchen", "bread", "essen", "küche", "kochen", "brot"),
    "travel": ("travel", "journey", "airport", "train", "car", "road", "reise", "zug", "strasse", "auto"),
    "money": ("money", "finance", "bank", "invest", "market", "geld", "bank", "markt"),
    "health": ("health", "doctor", "hospital", "medical", "sport", "fitness", "gesundheit", "arzt", "sport"),
    "music": ("music", "song", "concert", "instrument", "guitar", "musik", "lied", "konzert", "gitarre"),
    "art": ("art", "painting", "artist", "gallery", "design", "kunst", "gemälde", "galerie"),
    "animal": ("animal", "animals", "bird", "dog", "cat", "wildlife", "tier", "vogel", "hund", "katze"),
    "night": ("night", "evening", "dark", "moon", "nacht", "abend", "mond"),
    "war": ("war", "battle", "soldier", "military", "krieg", "schlacht", "soldat"),
    "peace": ("peace", "calm", "quiet", "frieden", "ruhe"),
    "education": ("school", "university", "student", "teacher", "learn", "schule", "student", "lernen"),
    "energy": ("energy", "solar", "wind", "power", "electricity", "energie", "strom", "sonne"),
    "space": ("space", "planet", "rocket", "galaxy", "universe", "planet", "rakete", "weltall"),
    "ocean_life": ("fish", "coral", "whale", "dolphin", "fisch", "wal", "delfin", "koralle"),
    "fire": ("fire", "flame", "burn", "volcano", "feuer", "flamme", "vulkan"),
    "ice": ("ice", "glacier", "arctic", "winter", "cold", "eis", "gletscher", "winter", "kalt"),
    "time": ("time", "clock", "calendar", "year", "decade", "zeit", "uhr", "jahr"),
    "book": ("book", "library", "read", "literature", "buch", "bibliothek", "lesen"),
    "problem": ("problem", "crisis", "issue", "challenge", "problem", "krise", "herausforderung"),
}


_SUFFIX_RULES = (
    ("tion", 6), ("sion", 6), ("ment", 6), ("ness", 6), ("heit", 6), ("keit", 6),
    ("ung", 5), ("lich", 6), ("ing", 5), ("er", 5), ("en", 5), ("es", 5), ("s", 4),
)


def tokenize_text(text: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[\w]+", str(text or ""), flags=re.UNICODE)]


def strip_suffix(token: str) -> str:
    for suffix, min_len in _SUFFIX_RULES:
        if len(token) > min_len and token.endswith(suffix):
            return token[: -len(suffix)]
    return token



def _build_word_to_concept() -> dict[str, str]:
    """Surface word -> concept, including stripped variants.

    Keywords pass through :func:`strip_suffix` before matching, so both the
    raw and the stripped form of every synonym must resolve to the same
    concept dimension ("cities" and "citi" both mean ``city``).
    """
    mapping: dict[str, str] = {}
    for concept, words in SYNONYM_CONCEPTS.items():
        for word in words:
            mapping[word] = concept
            mapping[strip_suffix(word)] = concept
    return mapping


_WORD_TO_CONCEPT = _build_word_to_concept()


def content_tokens(text: str) -> list[str]:
    """Lowercased, stopword-free, lightly stemmed tokens."""
    result: list[str] = []
    for token in tokenize_text(text):
        if token in STOPWORDS or len(token) < 2:
            continue
        result.append(strip_suffix(token))
    return result


def concept_vector(text_or_tokens) -> Counter:
    """Bag-of-concepts vector; synonym surface forms collapse to concepts."""
    if isinstance(text_or_tokens, list):
        tokens = list(text_or_tokens)
    else:
        tokens = [
            token for token in tokenize_text(text_or_tokens)
            if token not in STOPWORDS and len(token) >= 2
        ]
    vector: Counter = Counter()
    for token in tokens:
        concept = _WORD_TO_CONCEPT.get(token) or _WORD_TO_CONCEPT.get(strip_suffix(token))
        vector[concept or strip_suffix(token)] += 1
    return vector


def cosine_similarity(left: Counter, right: Counter) -> float:
    if not left or not right:
        return 0.0
    overlap = set(left) & set(right)
    dot = sum(left[key] * right[key] for key in overlap)
    norm_left = math.sqrt(sum(value * value for value in left.values()))
    norm_right = math.sqrt(sum(value * value for value in right.values()))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_left * norm_right)))


def extract_keywords(text: str, top_n: int = 4) -> list[str]:
    """Most distinctive content tokens (longest first, then frequency)."""
    counts = Counter(content_tokens(text))
    ranked = sorted(counts.items(), key=lambda item: (-len(item[0]), -item[1], item[0]))
    return [token for token, _count in ranked[:top_n]]


def split_script_sentences(text: str) -> list[str]:
    """Split script text into clean sentences (punctuation + line breaks)."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    sentences: list[str] = []
    for part in parts:
        sentence = re.sub(r"\s+", " ", part).strip(" \t-–—•*\"'")
        if len(sentence) >= 3:
            sentences.append(sentence)
    return sentences


# ---------------------------------------------------------------------------
# Semantic slots (spec section 19: group short same-topic sentences)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SlotDraft:
    sentences: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.sentences)


def build_semantic_slots(sentences: list[str], cadence: str = "adaptive") -> list[SlotDraft]:
    """Group sentences into semantic slots.

    ``every_N`` forces exactly N sentences per slot. ``adaptive`` merges a
    sentence into the current slot when they share at least one content
    keyword; slots never exceed three sentences or ~220 characters, so one
    visual stays on screen for a bounded time.
    """
    cadence = normalize_smart_visual_cadence(cadence)
    if cadence != "adaptive":
        size = int(cadence.split("_", 1)[1])
        drafts: list[SlotDraft] = []
        for start in range(0, len(sentences), size):
            chunk = sentences[start:start + size]
            drafts.append(SlotDraft(sentences=list(chunk), keywords=extract_keywords(" ".join(chunk))))
        return drafts

    drafts = []
    current = SlotDraft()
    current_keywords: set[str] = set()
    for sentence in sentences:
        tokens = set(content_tokens(sentence))
        shares_topic = bool(tokens & current_keywords)
        too_long = (
            len(current.sentences) >= 3
            or len(current.text) + len(sentence) > 220
        )
        if current.sentences and (not shares_topic or too_long):
            drafts.append(current)
            current = SlotDraft()
            current_keywords = set()
        current.sentences.append(sentence)
        current_keywords |= tokens
    if current.sentences:
        drafts.append(current)
    for draft in drafts:
        draft.keywords = extract_keywords(draft.text)
    return drafts


@dataclass(slots=True)
class TimedSlot:
    start: float
    end: float
    draft: SlotDraft


def assign_slot_times(drafts: list[SlotDraft], program_duration: float) -> list[TimedSlot]:
    """Map slots proportionally onto the program timeline.

    Slot weight is its spoken text length, so the visual plan follows the
    voiceover pacing derived from the canonical alignment (no second ASR).
    """
    duration = max(0.0, float(program_duration))
    weights = [max(1, len(draft.text)) for draft in drafts]
    total = sum(weights) or 1
    timed: list[TimedSlot] = []
    cursor = 0.0
    for draft, weight in zip(drafts, weights):
        fraction = weight / total
        start = cursor * duration
        cursor += fraction
        end = cursor * duration
        timed.append(TimedSlot(start=start, end=end, draft=draft))
    return timed


def generic_slot_drafts(program_duration: float, cadence: str = "adaptive") -> list[SlotDraft]:
    """Fallback when no script text exists: evenly spaced topic-neutral slots."""
    duration = max(0.0, float(program_duration))
    if duration <= 0:
        return []
    step = 6.0 if cadence == "adaptive" else 4.0 * max(1, int(cadence.split("_", 1)[1]))
    count = max(1, int(duration // step))
    drafts: list[SlotDraft] = []
    for index in range(count):
        drafts.append(SlotDraft(sentences=[f"Section {index + 1}"], keywords=[]))
    return drafts


# ---------------------------------------------------------------------------
# Media index (spec sections 7, 29, 30)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MediaIndexEntry:
    path: str
    kind: str                      # "image" | "video"
    category: str
    keywords: tuple[str, ...]
    title: str
    signature: str

    def concept_key(self) -> str:
        return " ".join(self.keywords) + " " + self.category


def _file_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}|{stat.st_mtime_ns}"


def load_sidecar_metadata(folder: Path) -> dict[str, dict]:
    """Optional lightweight sidecar metadata; missing files are fine.

    Supports ``smart_metadata.json`` ({file: {title, keywords, category}})
    and ``smart_metadata.csv`` (header: file,title,keywords,category with
    keywords separated by ``;``). All parsing is tolerant - a broken sidecar
    never breaks indexing.
    """
    metadata: dict[str, dict] = {}
    json_path = folder / "smart_metadata.json"
    if json_path.is_file():
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(raw, dict):
                for name, entry in raw.items():
                    if isinstance(entry, dict):
                        metadata[str(name).casefold()] = entry
        except (OSError, json.JSONDecodeError):
            pass
    csv_path = folder / "smart_metadata.csv"
    if csv_path.is_file():
        try:
            lines = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[1:]:
                cells = [cell.strip() for cell in line.split(",")]
                if len(cells) >= 2 and cells[0]:
                    entry = {"title": cells[1]}
                    if len(cells) >= 3 and cells[2]:
                        entry["keywords"] = [k.strip() for k in re.split(r"[;|]", cells[2]) if k.strip()]
                    if len(cells) >= 4 and cells[3]:
                        entry["category"] = cells[3]
                    metadata[cells[0].casefold()] = entry
        except OSError:
            pass
    return metadata


def scan_smart_visual_folders(folders: tuple[str, ...] | list[str]) -> list[MediaIndexEntry]:
    """Deterministic scan: folder name = category, sidecar metadata optional."""
    entries: list[MediaIndexEntry] = []
    seen: set[str] = set()
    for folder in folders:
        root = Path(str(folder).strip()).expanduser()
        if not root.is_dir():
            continue
        sidecar = load_sidecar_metadata(root)
        category = root.name.casefold() or "media"
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in SMART_IMAGE_EXTENSIONS | SMART_VIDEO_EXTENSIONS:
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            meta = sidecar.get(path.name.casefold(), {})
            try:
                signature = _file_signature(path)
            except OSError:
                continue
            title = str(meta.get("title", "") or "")
            keywords = [str(k).casefold() for k in (meta.get("keywords", []) or []) if str(k).strip()]
            keywords += content_tokens(title)
            keywords += content_tokens(path.stem.replace("_", " ").replace("-", " "))
            entry_category = str(meta.get("category", "") or "").casefold() or category
            ordered: list[str] = []
            for keyword in keywords:
                keyword = keyword.strip()
                if keyword and keyword not in ordered:
                    ordered.append(keyword)
            entries.append(
                MediaIndexEntry(
                    path=key,
                    kind="image" if path.suffix.casefold() in SMART_IMAGE_EXTENSIONS else "video",
                    category=entry_category,
                    keywords=tuple(ordered),
                    title=title,
                    signature=signature,
                )
            )
    return entries


@dataclass(slots=True)
class IndexStats:
    indexed: int = 0
    reused: int = 0
    removed: int = 0
    errors: int = 0
    categories: tuple[str, ...] = ()


def _index_cache_path(cache_dir: Path, folder: str) -> Path:
    digest = hashlib.sha256(str(Path(folder).expanduser().resolve()).encode("utf-8")).hexdigest()[:24]
    return Path(cache_dir) / f"folder_{digest}.json"


def build_media_index(
    folders: tuple[str, ...] | list[str],
    cache_dir: Path | str,
) -> tuple[list[MediaIndexEntry], IndexStats]:
    """Incremental media index with per-folder JSON caches.

    Unchanged files (same size+mtime signature) reuse their cached entry;
    only new/changed files are reprocessed (spec sections 29-30). Any cache
    problem falls back to a fresh in-memory scan - the project always stays
    renderable.
    """
    cache_dir = Path(cache_dir)
    stats = IndexStats()
    all_entries: list[MediaIndexEntry] = []
    for folder in folders:
        folder_text = str(folder).strip()
        if not folder_text:
            continue
        cache_path = _index_cache_path(cache_dir, folder_text)
        cached: dict[str, dict] = {}
        try:
            if cache_path.is_file():
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cached = {str(k): v for k, v in (raw.get("entries") or {}).items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            cached = {}
        try:
            fresh = scan_smart_visual_folders((folder_text,))
        except OSError:
            stats.errors += 1
            continue
        fresh_by_path = {entry.path: entry for entry in fresh}
        stats.removed += max(0, len(cached) - len(fresh_by_path))
        entries_for_cache: dict[str, dict] = {}
        for path, entry in fresh_by_path.items():
            cached_entry = cached.get(path)
            if cached_entry and cached_entry.get("signature") == entry.signature:
                stats.reused += 1
                restored = MediaIndexEntry(
                    path=entry.path,
                    kind=str(cached_entry.get("kind", entry.kind)),
                    category=str(cached_entry.get("category", entry.category)),
                    keywords=tuple(cached_entry.get("keywords", entry.keywords)),
                    title=str(cached_entry.get("title", entry.title)),
                    signature=entry.signature,
                )
                all_entries.append(restored)
                entries_for_cache[path] = cached_entry
            else:
                stats.indexed += 1
                all_entries.append(entry)
                entries_for_cache[path] = {
                    "signature": entry.signature,
                    "kind": entry.kind,
                    "category": entry.category,
                    "keywords": list(entry.keywords),
                    "title": entry.title,
                }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"folder": folder_text, "entries": entries_for_cache}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError:
            stats.errors += 1
    stats.categories = tuple(sorted({entry.category for entry in all_entries}))
    return all_entries, stats


# ---------------------------------------------------------------------------
# Scoring (spec sections 8-12)
# ---------------------------------------------------------------------------
def score_candidate(
    query_vector: Counter,
    query_keywords: list[str],
    entry: MediaIndexEntry,
    entry_vector: Counter,
    recent_paths: tuple[str, ...] = (),
) -> float:
    """Normalized 0..1 relevance score with repetition penalty.

    cosine(semantic) + keyword/tag bonus + category bonus, clamped to 0..1;
    recently used media are penalized instead of repeating back-to-back.
    """
    base = cosine_similarity(query_vector, entry_vector)
    if query_keywords:
        overlap = sum(1 for keyword in query_keywords if keyword in entry.keywords)
        tag_bonus = 0.15 * (overlap / min(4, len(query_keywords)))
    else:
        tag_bonus = 0.0
    category_bonus = 0.08 if entry.category and entry.category in set(query_keywords) else 0.0
    score = min(1.0, base + tag_bonus + category_bonus)
    if entry.path in recent_paths:
        position = len(recent_paths) - 1 - list(recent_paths).index(entry.path)
        score *= 0.45 if position == 0 else 0.70
    return round(score, 6)


# ---------------------------------------------------------------------------
# Generation strategy (spec sections 13, 16)
# ---------------------------------------------------------------------------
def strategy_wants_generation(strategy: str, percent: int, slot_number: int, rng: Random) -> bool:
    strategy = normalize_generation_strategy(strategy)
    if strategy == "always":
        return True
    if strategy == "only_when_no_match":
        return False
    if strategy == "every_2nd":
        return slot_number % 2 == 0
    if strategy == "every_3rd":
        return slot_number % 3 == 0
    if strategy == "every_4th":
        return slot_number % 4 == 0
    if strategy == "random_25":
        return rng.random() * 100.0 < 25.0
    if strategy == "random_50":
        return rng.random() * 100.0 < 50.0
    if strategy == "custom_percent":
        return rng.random() * 100.0 < float(max(0, min(100, int(percent))))
    return False


_STYLE_PHRASES = {
    "realistic": "realistic photographic style",
    "cinematic": "cinematic film still style",
    "editorial": "clean editorial illustration style",
    "documentary": "neutral documentary style",
    "conceptual": "abstract conceptual art style",
    "minimal": "minimal flat composition",
}


def build_generation_prompt(
    keywords: list[str],
    style: str,
    style_custom: str = "",
    width: int = 1280,
    height: int = 720,
) -> str:
    """Meaning-based prompt; always excludes text/logos/watermarks."""
    style = normalize_smart_visual_style(style)
    phrase = str(style_custom or "").strip() if style == "custom" else _STYLE_PHRASES.get(style, "")
    topic = ", ".join(keywords) if keywords else "general topic"
    orientation = "portrait orientation" if height > width else "landscape orientation"
    return (
        f"{topic}, {phrase}, {orientation}, atmospheric background plate, "
        "no text, no letters, no logo, no watermark"
    ).strip()


# ---------------------------------------------------------------------------
# Plan records
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SmartVisualSlot:
    start: float
    end: float
    topic: str
    sentence: str
    keywords: tuple[str, ...]
    selected_kind: str = ""            # image | video | generated | ""
    selected_path: str = ""
    selected_label: str = ""
    score: float = 0.0
    reason: str = ""
    generation_used: bool = False
    prompt: str = ""

    @property
    def clamped_duration(self) -> float:
        return max(MIN_SLOT_SECONDS, min(MAX_SLOT_SECONDS, float(self.end) - float(self.start)))


@dataclass(slots=True)
class SmartVisualPlan:
    enabled: bool = False
    slots: list[SmartVisualSlot] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    identity: str = ""

    @property
    def selected_count(self) -> int:
        return sum(1 for slot in self.slots if slot.selected_kind)

    def to_records(self) -> list[dict]:
        """Plain preview records (time/topic/selected/match/fallback)."""
        records: list[dict] = []
        for slot in self.slots:
            records.append(
                {
                    "time": f"{slot.start:.2f}-{slot.end:.2f}s",
                    "topic": slot.topic[:90],
                    "selected": slot.selected_label or "–",
                    "kind": slot.selected_kind or "none",
                    "match": f"{slot.score:.2f}",
                    "reason": slot.reason,
                    "generation_used": slot.generation_used,
                }
            )
        return records


def smart_plan_identity(
    profile: SmartVisualProfile,
    slots: list[SmartVisualSlot],
    geometry: tuple[int, int, float],
) -> str:
    """Deterministic Stage-1 identity of the smart visual plan."""
    payload = {
        "enabled": bool(profile.enabled),
        "priority": profile.source_priority,
        "threshold": round(profile.threshold, 4),
        "strategy": profile.generation_strategy,
        "percent": profile.generation_percent,
        "repetition": profile.repetition_window,
        "style": profile.style,
        "style_custom": profile.style_custom,
        "cadence": profile.cadence,
        "geometry": [int(geometry[0]), int(geometry[1]), round(float(geometry[2]), 6)],
        "slots": [
            {
                "start": round(slot.start, 4),
                "end": round(slot.end, 4),
                "kind": slot.selected_kind,
                "path": slot.selected_path,
                "score": round(slot.score, 6),
                "reason": slot.reason,
                "prompt": slot.prompt,
            }
            for slot in slots
            if slot.selected_kind
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Plan building (stages A-D)
# ---------------------------------------------------------------------------
def _probe_video_entry(path: str, ffprobe_path: str | Path) -> dict | None:
    """Cheap metadata probe of a video candidate; None when unreadable."""
    import subprocess

    from .platform_utils import hidden_process_flags, safe_subprocess_env

    try:
        completed = subprocess.run(
            [str(ffprobe_path), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=hidden_process_flags(), env=safe_subprocess_env(),
        )
        if completed.returncode != 0:
            return None
        data = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return None
    try:
        duration = float((data.get("format") or {}).get("duration") or float(video.get("duration") or 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return None
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": str(video.get("codec_name") or ""),
    }


def build_smart_visual_plan(
    *,
    profile: SmartVisualProfile,
    script_text: str,
    program_duration: float,
    width: int,
    height: int,
    fps: float,
    cache_dir: Path | str,
    ffprobe_path: str | Path,
    seed_parts: tuple = (),
    ffmpeg_path: str | Path | None = None,
    log=print,
) -> SmartVisualPlan:
    """Build the complete Smart Visual plan BEFORE any rendering happens."""
    from .image_generation import resolve_generation_provider

    plan = SmartVisualPlan(enabled=bool(profile.enabled))
    if not profile.active:
        return plan
    try:
        # Stage A: canonical timing data only (script + voiceover duration).
        sentences = split_script_sentences(script_text)
        drafts = build_semantic_slots(sentences, profile.cadence) if sentences else generic_slot_drafts(
            program_duration, profile.cadence
        )
        timed = assign_slot_times(drafts, program_duration)
        if not timed:
            plan.diagnostics.append("Keine Slots ableitbar (kein Skript, keine Programmdauer).")
            return plan

        # Stage C: cached media index of the configured folders.
        index_dir = Path(cache_dir) / "smart_visual_index"
        entries, stats = build_media_index(profile.folders, index_dir)
        plan.diagnostics.append(
            f"Medienindex: {len(entries)} Datei(en) ({stats.indexed} neu, {stats.reused} wiederverwendet, "
            f"{stats.errors} Fehler)."
        )
        entry_vectors = {entry.path: concept_vector(list(entry.keywords) + [entry.category]) for entry in entries}

        # Stage D: provider availability (no model init when disabled).
        provider, provider_notes = resolve_generation_provider(ffmpeg_path)
        plan.diagnostics.extend(provider_notes)

        generation_dir = Path(cache_dir) / "smart_visual_generated"
        seed_material = "|".join(str(part) for part in seed_parts) + f"|{width}x{height}@{round(float(fps), 6)}"
        rng = Random(int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:12], 16))
        threshold = profile.threshold
        recent: list[str] = []
        window = max(0, int(profile.repetition_window))

        for number, slot in enumerate(timed, start=1):
            query_vector = concept_vector(slot.draft.text)
            query_keywords = slot.draft.keywords
            candidates = []
            for entry in entries:
                score = score_candidate(
                    query_vector, query_keywords, entry, entry_vectors[entry.path],
                    recent_paths=tuple(recent[-window:]) if window else (),
                )
                candidates.append((score, entry))
            candidates.sort(key=lambda pair: (-pair[0], pair[1].path))

            visual = SmartVisualSlot(
                start=slot.start,
                end=slot.end,
                topic=" ".join(query_keywords[:4]) or "general",
                sentence=slot.draft.text[:200],
                keywords=tuple(query_keywords),
            )
            plan.slots.append(visual)

            wants_generation = strategy_wants_generation(
                profile.generation_strategy, profile.generation_percent, number, rng
            )
            prompt = build_generation_prompt(query_keywords, profile.style, profile.style_custom, width, height)
            visual.prompt = prompt

            def try_generate(reason: str) -> bool:
                if provider is None:
                    return False
                # The seed derives from the PROMPT, not the slot number: a
                # repeated sentence therefore hits the exact same cache key
                # and its generated visual is reused (spec section 46-E).
                prompt_seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16)
                result = provider.generate(
                    prompt, width, height,
                    style=profile.style, seed=prompt_seed, cache_dir=generation_dir,
                )
                plan.diagnostics.extend(result.diagnostics[:1])
                if result.path is None:
                    return False
                visual.selected_kind = "generated"
                visual.selected_path = str(result.path)
                visual.selected_label = f"Generated ({provider.name})"
                visual.reason = reason
                visual.generation_used = True
                return True

            matched = [(score, entry) for score, entry in candidates if score >= threshold]

            def pick_existing(pool: list[tuple[float, MediaIndexEntry]], reason: str) -> bool:
                if not pool:
                    return False
                preferred: list[tuple[float, MediaIndexEntry]]
                if profile.source_priority == "video_first":
                    preferred = [pair for pair in pool if pair[1].kind == "video"] or \
                                [pair for pair in pool if pair[1].kind == "image"] or pool
                elif profile.source_priority == "image_first":
                    preferred = [pair for pair in pool if pair[1].kind == "image"] or \
                                [pair for pair in pool if pair[1].kind == "video"] or pool
                elif profile.source_priority == "balanced":
                    best_score = pool[0][0]
                    ties = [pair for pair in pool if best_score - pair[0] <= 0.02]
                    preferred = [pair for pair in ties if pair[1].kind == "image"] or pool
                else:  # best_match
                    preferred = pool
                score, entry = preferred[0]
                visual.selected_kind = entry.kind
                visual.selected_path = entry.path
                visual.selected_label = f"{Path(entry.path).name} [{entry.kind}, {entry.category}]"
                visual.score = score
                visual.reason = reason
                return True

            if wants_generation and try_generate("generation_strategy"):
                pass
            elif matched and pick_existing(matched, "matched"):
                pass
            elif try_generate("below_threshold_generated"):
                pass
            elif pick_existing(candidates, "fallback_best_existing"):
                plan.diagnostics.append(
                    f"Slot {number}: kein Treffer über Schwelle {threshold:.2f} - bestes vorhandenes Medium als Fallback."
                )
            else:
                visual.reason = "skipped_no_media"
                plan.diagnostics.append(f"Slot {number}: keine Medien und keine Erzeugung - Slot übersprungen.")
                continue
            recent.append(visual.selected_path)
            if window and len(recent) > window:
                recent = recent[-window:]

        if any(slot.selected_kind for slot in plan.slots):
            plan.identity = smart_plan_identity(profile, plan.slots, (width, height, fps))
        log(
            f"Phase 31 Smart Visuals: {plan.selected_count}/{len(plan.slots)} Slot(s) belegt "
            f"(Schwelle {threshold:.2f}, Strategie {profile.generation_strategy})."
        )
    except Exception as exc:  # spec section 32: never break the render
        plan.diagnostics.append(f"Smart-Visual-Planung fehlgeschlagen, Fallback auf normale Auswahl: {exc}")
        plan.slots = []
        plan.identity = ""
        log(f"Phase 31 Smart Visuals: Planung übersprungen ({exc.__class__.__name__}).")
    return plan


# ---------------------------------------------------------------------------
# Plan application (stage E - reuses the Phase-30 timeline insertion path)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SmartVisualApplyResult:
    media: list = field(default_factory=list)
    inserted: list = field(default_factory=list)  # (position, path, duration, kind)
    identity: str = ""

    @property
    def count(self) -> int:
        return len(self.inserted)


def _make_smart_video_media(entry_path: str, duration: float, probe: dict, transition_type: str):
    """A silent, trimmed video timeline element (no original audio)."""
    from .models import AudioInfo, MediaInfo

    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    return MediaInfo(
        path=Path(entry_path),
        duration=float(duration),
        width=width,
        height=height,
        effective_width=width,
        effective_height=height,
        fps=30.0,
        fps_fraction="30/1",
        video_codec=str(probe.get("codec") or "h264"),
        pixel_format="yuv420p",
        sar="1:1",
        dar="",
        audio=AudioInfo(present=False, codec="", sample_rate=0, channels=0),
        source_duration=float(probe.get("duration") or duration),
        source_folder=str(Path(entry_path).parent),
        image_transition_type=transition_type,
        smart_visual_insertion=True,
    )


def apply_smart_visual_plan(
    render_media: list,
    plan: SmartVisualPlan,
    *,
    width: int,
    height: int,
    fps: float,
    transition_type: str,
    image_profile: ImageTimelineProfile,
    ffprobe_path: str | Path,
    log=print,
) -> SmartVisualApplyResult:
    """Insert the planned visuals into the fitted timeline.

    Image slots reuse the Phase-30 timeline-image element (cover framing,
    motion, TV effect, transitions). Video slots become silent trimmed
    clips. Slots colliding on the same boundary are skipped (spec section
    38); every failure path simply skips the slot, so the project always
    stays renderable.
    """
    result = SmartVisualApplyResult(media=list(render_media))
    if not plan.enabled or not plan.slots:
        return result
    items = list(render_media)
    if len(items) < 2:
        return result

    durations = [max(0.0, float(getattr(item, "duration", 0.0) or 0.0)) for item in items]
    cumulative: list[float] = []
    running = 0.0
    for duration in durations:
        running += duration
        cumulative.append(running)

    used_boundaries: set[int] = set()
    inserts: list[tuple[int, object, float, str]] = []
    probe_cache: dict[str, dict | None] = {}
    frame = 1.0 / max(1.0, float(fps))

    def _frame_grid(seconds: float, *, ceil: bool) -> float:
        """Snap a duration to whole frames so zoompan/trim/xfade frame math
        adds up exactly (a fractional remainder would drop frames and the
        transition chain would end short of the planned program)."""
        frames = int(math.ceil(seconds / frame)) if ceil else int(math.floor(seconds / frame))
        return round(max(1, frames) * frame, 6)

    for slot in plan.slots:
        if not slot.selected_kind:
            continue
        duration = _frame_grid(slot.clamped_duration, ceil=True)
        midpoint = (float(slot.start) + float(slot.end)) / 2.0
        # Find the item containing the slot midpoint, then place the visual at
        # whichever of the item's two edges is closest to the midpoint. When
        # that edge is already taken by another slot, the other edge is tried;
        # only when both are occupied is the slot skipped (spec section 38).
        containing = len(items) - 1
        for index, edge in enumerate(cumulative):
            if edge >= midpoint:
                containing = index
                break
        left_time = cumulative[containing - 1] if containing > 0 else 0.0
        right_time = cumulative[containing]
        candidates: list[tuple[float, int]] = []
        for boundary, when in ((containing, left_time), (containing + 1, right_time)):
            clamped = max(1, min(len(items) - 1, boundary))
            candidates.append((abs(when - midpoint), clamped))
        candidates.sort(key=lambda pair: (pair[0], pair[1]))
        boundary: int | None = None
        for _distance, candidate in candidates:
            if candidate not in used_boundaries:
                boundary = candidate
                break
        if boundary is None:
            slot.reason = (slot.reason + "+collision_skipped").strip("+")
            continue

        media_item = None
        if slot.selected_kind == "video":
            probed = probe_cache.get(slot.selected_path)
            if probed is None and slot.selected_path not in probe_cache:
                probe_cache[slot.selected_path] = _probe_video_entry(slot.selected_path, ffprobe_path)
                probed = probe_cache[slot.selected_path]
            if not probed:
                slot.reason = (slot.reason + "+video_unreadable_skipped").strip("+")
                continue
            duration = _frame_grid(
                min(duration, float(probed.get("duration") or duration)), ceil=False
            )
            if duration < MIN_SLOT_SECONDS:
                slot.reason = (slot.reason + "+video_too_short_skipped").strip("+")
                continue
            media_item = _make_smart_video_media(slot.selected_path, duration, probed, transition_type)
        else:
            path = Path(slot.selected_path)
            if not path.is_file():
                slot.reason = (slot.reason + "+file_missing_skipped").strip("+")
                continue
            media_item = make_image_media(
                path=path,
                duration=duration,
                width=width,
                height=height,
                fps=fps,
                size=probe_image_size(path, ffprobe_path),
                transition_type=transition_type,
                profile=image_profile,
            )
        used_boundaries.add(boundary)
        inserts.append((boundary, media_item, duration, slot.selected_kind))

    if not inserts:
        return result

    inserts.sort(key=lambda entry: entry[0])
    new_media: list = []
    cursor = 0
    for boundary, media_item, duration, kind in inserts:
        new_media.extend(items[cursor:boundary])
        new_media.append(media_item)
        result.inserted.append((boundary, Path(getattr(media_item, "path", Path(""))), duration, kind))
        cursor = boundary
    new_media.extend(items[cursor:])
    result.media = new_media
    result.identity = plan.identity
    log(
        f"Phase 31 Smart Visuals: {result.count} Visual(s) eingefügt "
        f"({sum(1 for item in result.inserted if item[3] == 'generated')} erzeugt)."
    )
    return result
