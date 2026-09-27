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

#: Phase 32: explicit fallback policy when no media reaches the threshold.
#: ``generate_image`` IS the historical Phase-31 behavior (try generation,
#: then the best existing media, then skip); the other policies are new,
#: strictly opt-in choices.
FALLBACK_CHOICES = (
    "generate_image",
    "random_video",
    "random_image",
    "best_available",
    "skip",
)

#: Phase 32: per-slot diagnostics tags (plan preview + render log).
SLOT_MODE_MATCH = "MATCH"
SLOT_MODE_GENERATED = "GENERATED"
SLOT_MODE_FALLBACK_GENERATED = "FALLBACK_GENERATED"
SLOT_MODE_FALLBACK_RANDOM_VIDEO = "FALLBACK_RANDOM_VIDEO"
SLOT_MODE_FALLBACK_RANDOM_IMAGE = "FALLBACK_RANDOM_IMAGE"
SLOT_MODE_FALLBACK_BEST_AVAILABLE = "FALLBACK_BEST_AVAILABLE"
SLOT_MODE_SKIPPED = "SKIPPED"

#: Phase 33: per-slot selection source (Smart = semantic match, Random =
#: seeded pool draw). Shown by Analyze Timeline and carried in the records.
SLOT_SOURCE_SMART = "SMART"
SLOT_SOURCE_RANDOM = "RANDOM"

MIN_SLOT_SECONDS = 1.0
MAX_SLOT_SECONDS = 8.0

# ---------------------------------------------------------------------------
# Phase 33: selection modes & matching policy constants.
# Smart Visuals is a content-aware SELECTION + placement engine: it picks
# existing media from the configured pools and never generates anything.
# Every relevance-threshold/matching-policy value lives HERE (one place),
# so the UI never exposes score-tuning magic values.
# ---------------------------------------------------------------------------
SMART_MODE_SMART_MATCH = "smart_match"        # smart whenever possible
SMART_MODE_SMART_INSERTS = "smart_inserts"    # mostly random, strong matches only
SMART_MODE_RANDOM_ONLY = "random_only"        # baseline / debug, no matching
SMART_VISUAL_MODES = (
    SMART_MODE_SMART_MATCH,
    SMART_MODE_SMART_INSERTS,
    SMART_MODE_RANDOM_ONLY,
)

#: Relevance gate for Smart Match: the best candidate must reach this score
#: to count as a semantic match (below it the slot falls back to random).
SMART_MATCH_THRESHOLD = 0.50
#: Strong gate for Smart Inserts: only clearly relevant media is placed at
#: the selected insert opportunities; everything else stays random.
SMART_INSERT_STRONG_THRESHOLD = 0.60
#: Smart Match explores alternate good matches inside this top-K window when
#: the best candidate is already used or when Randomize explores variants.
SMART_MATCH_TOP_K = 5
#: Default share of insert opportunities that try a strong semantic match in
#: Smart Inserts mode (the UI exposes this one value as a percentage).
DEFAULT_SMART_INSERT_PERCENT = 25
#: Inserted image duration (seconds). One single configurable value.
DEFAULT_SMART_IMAGE_DURATION = 5.0
MIN_SMART_IMAGE_DURATION = 0.5
MAX_SMART_IMAGE_DURATION = 15.0


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


def normalize_fallback_policy(value: object) -> str:
    """Phase 32: canonical fallback policy (historical default preserved).

    Phase 33: the fallback POLICY is no longer part of the Smart Visual
    workflow (selection modes replaced it and nothing is ever generated);
    the normalizer and its vocabulary stay available so projects saved by
    older versions keep loading safely.
    """
    text = str(value or "generate_image").strip().casefold()
    return text if text in FALLBACK_CHOICES else "generate_image"


def normalize_smart_visual_mode(value: object) -> str:
    """Phase 33: canonical selection mode (default Smart Match)."""
    text = str(value or SMART_MODE_SMART_MATCH).strip().casefold()
    return text if text in SMART_VISUAL_MODES else SMART_MODE_SMART_MATCH


def clamp_smart_image_duration(value: object) -> float:
    """Phase 33: the single inserted-image duration in seconds."""
    try:
        return max(MIN_SMART_IMAGE_DURATION, min(MAX_SMART_IMAGE_DURATION, float(value)))
    except (TypeError, ValueError):
        return DEFAULT_SMART_IMAGE_DURATION


def clamp_smart_insert_percent(value: object) -> int:
    """Phase 33: share of insert opportunities that try a strong match."""
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return DEFAULT_SMART_INSERT_PERCENT


def clamp_smart_visual_nonce(value: object) -> int:
    """Phase 33: Randomize counter; tolerant against any stored value."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


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
    # Phase 32: generation toggle + explicit fallback policy. The defaults
    # reproduce the exact Phase-31 behavior (generation allowed; below the
    # threshold try generation, then best existing media, then skip).
    # Phase 33: these fields are RETAINED for backward compatibility only -
    # the Smart Visual workflow no longer generates anything and the mode
    # below fully governs selection. Old projects keep loading unchanged.
    allow_generated: bool = True
    fallback_policy: str = "generate_image"
    # Phase 33: content-aware selection engine settings.
    #   smart_match    - best semantic match per region; random only when no
    #                    candidate reaches SMART_MATCH_THRESHOLD.
    #   smart_inserts  - mostly random; a strong semantic match is placed at
    #                    ~insert_percent of the insert opportunities.
    #   random_only    - seeded random pool draws, no semantic matching.
    mode: str = SMART_MODE_SMART_MATCH
    #: Inserted image duration in seconds (one single value, default 5.0).
    image_duration: float = DEFAULT_SMART_IMAGE_DURATION
    #: Smart Inserts only: percent of opportunities that try a strong match.
    insert_percent: int = DEFAULT_SMART_INSERT_PERCENT
    #: Randomize counter; changes the seeded assignment without touching the
    #: mode, the duration or the timeline itself.
    randomize_nonce: int = 0

    @property
    def active(self) -> bool:
        """Enabled AND at least one source folder configured."""
        return bool(self.enabled) and any(str(folder).strip() for folder in self.folders)

    @property
    def threshold(self) -> float:
        return smart_visual_threshold(self.threshold_mode, self.threshold_custom)

    @property
    def phase32_active(self) -> bool:
        """True when a Phase-32 setting deviates from the historical
        defaults - only then may identities gain extra keys."""
        return not self.allow_generated or self.fallback_policy != "generate_image"


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
        # Phase 32: generation toggle + explicit fallback policy.
        allow_generated=bool(getattr(settings, "smart_visual_allow_generated", True)),
        fallback_policy=normalize_fallback_policy(getattr(settings, "smart_visual_fallback", "generate_image")),
        # Phase 33: selection mode, image duration, insert frequency and the
        # Randomize counter. Missing keys (older projects) resolve to the
        # safe defaults: Smart Match / 5.0 s / 25 % / nonce 0.
        mode=normalize_smart_visual_mode(getattr(settings, "smart_visual_mode", SMART_MODE_SMART_MATCH)),
        image_duration=clamp_smart_image_duration(
            getattr(settings, "smart_visual_image_duration", DEFAULT_SMART_IMAGE_DURATION)
        ),
        insert_percent=clamp_smart_insert_percent(
            getattr(settings, "smart_visual_insert_percent", DEFAULT_SMART_INSERT_PERCENT)
        ),
        randomize_nonce=clamp_smart_visual_nonce(getattr(settings, "smart_visual_randomize_nonce", 0)),
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
    "mountain": ("mountain", "mountains", "peak", "alps", "hill", "berg", "gipfel", "alp",
                 "alpine", "summit", "ridge", "hike", "hiking", "trail"),
    "water": ("water", "river", "lake", "sea", "ocean", "wave", "fluss", "see", "meer", "wasser", "welle",
              "beach", "coast", "shore"),
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
    # Phase 33: the full tolerant metadata text of this asset (every string
    # value from its sidecar record - title, description, keywords, topic,
    # subject, entities, mood, environment, scene, ... - whatever schema the
    # folder uses). Feeds the semantic vector; nothing here is hardcoded to
    # one fixed schema.
    metadata_text: str = ""

    def concept_key(self) -> str:
        return " ".join(self.keywords) + " " + self.category + " " + self.metadata_text


#: Phase 33: sidecar keys whose values are "primary signals" - short,
#: keyword-like descriptors that also join the hard keyword-overlap bonus
#: (in addition to the semantic vector). Everything else still joins the
#: vector through ``metadata_text``; unknown keys are never rejected.
_SIDECAR_PRIMARY_KEYS = (
    "keywords", "tags", "title", "topic", "subject", "subjects",
    "entities", "objects", "scene", "scenes", "mood", "environment",
    "category",
)
#: Keys that only identify the file itself and never describe its content.
_SIDECAR_IGNORED_KEYS = ("file", "filename", "path")


def _sidecar_text_values(value: object) -> list[str]:
    """Flatten one tolerant sidecar value into plain text fragments."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            parts.extend(_sidecar_text_values(item))
        return parts
    if isinstance(value, dict):
        parts = []
        for nested in value.values():
            parts.extend(_sidecar_text_values(nested))
        return parts
    return [str(value)]


def _file_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}|{stat.st_mtime_ns}"


def _sidecar_signature(folder: Path) -> str:
    """Phase 33: identity of the folder's metadata sidecars.

    Editing ``smart_metadata.json``/``smart_metadata.csv`` changes this
    signature, which invalidates the folder's index cache so enriched
    metadata is re-read without waiting for the media files to change.
    """
    parts: list[str] = []
    for name in ("smart_metadata.json", "smart_metadata.csv"):
        sidecar = folder / name
        try:
            if sidecar.is_file():
                stat = sidecar.stat()
                parts.append(f"{name}:{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            continue
    return ";".join(parts)


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
            if not isinstance(meta, dict):
                meta = {}
            try:
                signature = _file_signature(path)
            except OSError:
                continue
            title = str(meta.get("title", "") or "")
            keywords = [str(k).casefold() for k in (meta.get("keywords", []) or []) if str(k).strip()]
            keywords += content_tokens(title)
            keywords += content_tokens(path.stem.replace("_", " ").replace("-", " "))

            # Phase 33: tolerant metadata harvest. We do NOT assume one fixed
            # schema. Every string-like value in the sidecar record becomes
            # part of ``metadata_text`` (semantic vector); values under the
            # well-known primary keys additionally strengthen the hard
            # keyword-overlap signal. Unknown keys are still harvested into
            # ``metadata_text`` so richer schemas keep working.
            metadata_fragments: list[str] = []
            for raw_key, raw_value in meta.items():
                key_name = str(raw_key or "").strip().casefold()
                if not key_name or key_name in _SIDECAR_IGNORED_KEYS:
                    continue
                for fragment in _sidecar_text_values(raw_value):
                    text = fragment.strip()
                    if not text:
                        continue
                    metadata_fragments.append(text)
                    if key_name in _SIDECAR_PRIMARY_KEYS:
                        keywords.extend(content_tokens(text))
            metadata_text = " ".join(metadata_fragments)

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
                    metadata_text=metadata_text,
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
        cached_sidecar = ""
        try:
            if cache_path.is_file():
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cached = {str(k): v for k, v in (raw.get("entries") or {}).items() if isinstance(v, dict)}
                    cached_sidecar = str(raw.get("sidecar_signature") or "")
        except (OSError, json.JSONDecodeError):
            cached = {}
        try:
            fresh = scan_smart_visual_folders((folder_text,))
        except OSError:
            stats.errors += 1
            continue
        # Phase 33: editing the metadata sidecars changes the enrichment, so
        # a sidecar change invalidates the cached entries of this folder.
        sidecar_now = _sidecar_signature(Path(folder_text).expanduser())
        if cached_sidecar != sidecar_now:
            cached = {}
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
                    metadata_text=str(cached_entry.get("metadata_text", entry.metadata_text) or ""),
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
                    "metadata_text": entry.metadata_text,
                }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "folder": folder_text,
                        "sidecar_signature": sidecar_now,
                        "entries": entries_for_cache,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
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
    # Phase 32: explicit diagnostics tag (MATCH / GENERATED /
    # FALLBACK_GENERATED / FALLBACK_RANDOM_VIDEO / FALLBACK_RANDOM_IMAGE /
    # FALLBACK_BEST_AVAILABLE / SKIPPED). Empty until the slot is decided.
    fallback_mode: str = ""
    # Phase 33: SMART = placed by semantic matching, RANDOM = seeded pool
    # draw (fallback or mode-driven). Empty until the slot is decided.
    source_mode: str = ""
    # Phase 33: duration of the inserted image element in seconds (the
    # profile's single configurable image duration). 0.0 = legacy behavior
    # (derive the duration from the slot's context window).
    insert_duration: float = 0.0

    @property
    def clamped_duration(self) -> float:
        return max(MIN_SLOT_SECONDS, min(MAX_SLOT_SECONDS, float(self.end) - float(self.start)))

    @property
    def effective_insert_duration(self) -> float:
        """Phase 33: the real duration of the inserted element."""
        if self.insert_duration > 0:
            return max(MIN_SMART_IMAGE_DURATION, min(MAX_SMART_IMAGE_DURATION, float(self.insert_duration)))
        return self.clamped_duration


@dataclass(slots=True)
class SmartVisualPlan:
    enabled: bool = False
    slots: list[SmartVisualSlot] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    identity: str = ""
    # Phase 32: image-rendering context shown in the plan preview. Uniform
    # for the whole plan; empty strings render as "project / none" labels.
    image_transition_label: str = ""
    image_effect_label: str = ""
    image_effect_intensity_label: str = ""

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
                    # Phase 32 preview extensions:
                    "fallback_mode": slot.fallback_mode
                    or ("MATCH" if slot.selected_kind else "SKIPPED"),
                    "duration": round(slot.clamped_duration, 2),
                    "image_transition": self.image_transition_label or "project",
                    "image_effect": self.image_effect_label or "none",
                    "image_effect_intensity": self.image_effect_intensity_label or "low",
                    # Phase 33 Analyze Timeline extensions: which engine
                    # picked the media, the real inserted duration and the
                    # local context the selection was matched against.
                    "source": slot.source_mode or ("SMART" if slot.selected_kind else "NONE"),
                    "insert_duration": round(slot.effective_insert_duration, 2),
                    "context": slot.sentence[:120],
                }
            )
        return records


def smart_plan_identity(
    profile: SmartVisualProfile,
    slots: list[SmartVisualSlot],
    geometry: tuple[int, int, float],
    phase32: dict | None = None,
    phase33: dict | None = None,
) -> str:
    """Deterministic Stage-1 identity of the smart visual plan."""
    slot_records: list[dict] = []
    for slot in slots:
        if not slot.selected_kind:
            continue
        record = {
            "start": round(slot.start, 4),
            "end": round(slot.end, 4),
            "kind": slot.selected_kind,
            "path": slot.selected_path,
            "score": round(slot.score, 6),
            "reason": slot.reason,
            "prompt": slot.prompt,
        }
        # Phase 32: the diagnostics tag joins the identity ONLY when the
        # fallback/generation settings deviate from the Phase-31 defaults,
        # so unchanged projects keep their exact plan identity.
        if phase32:
            record["fallback_mode"] = slot.fallback_mode
        slot_records.append(record)
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
        "slots": slot_records,
        # Phase 33: the selection engine identity. Smart Visuals is now a
        # pure selection engine (nothing is ever generated), so every active
        # plan carries the mode, the single image duration, the Smart
        # Inserts frequency and the Randomize nonce. This intentionally
        # invalidates pre-Phase-33 smart visual caches exactly once.
        "phase33": phase33
        or {
            "engine": "selection",
            "mode": profile.mode,
            "image_duration": round(float(profile.image_duration), 3),
            "insert_percent": int(profile.insert_percent),
            "nonce": int(profile.randomize_nonce),
        },
    }
    # Phase 32: fallback policy/generation toggle and the dedicated image
    # rendering settings extend the identity ONLY when they deviate from
    # the historical defaults, so unchanged projects keep their exact
    # Phase-31 plan identity and cache reuse.
    if phase32:
        payload["phase32"] = phase32
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


def _shared_signal_tokens(slot_keywords: tuple[str, ...], entry: MediaIndexEntry) -> tuple[str, ...]:
    """Phase 33: explainable overlap between the local timeline context and
    one asset's metadata (the "why was this selected" signals)."""
    entry_tokens = set(content_tokens(" ".join(entry.keywords)))
    entry_tokens.update(content_tokens(entry.category))
    entry_tokens.update(content_tokens(entry.metadata_text))
    shared: list[str] = []
    for token in slot_keywords:
        for concept in concept_vector([token]):
            if concept in entry_tokens and concept not in shared:
                shared.append(concept)
    return tuple(shared[:4])


def assign_smart_visual_selections(
    timed: list,
    entries: list[MediaIndexEntry],
    *,
    profile: SmartVisualProfile,
    rng: Random,
) -> list[SmartVisualSlot]:
    """Phase 33: content-aware, uniqueness-aware media assignment.

    Smart Visuals is a SELECTION engine: it only picks existing media from
    the configured pools and NEVER generates anything. The three modes:

    * ``smart_match``   - best semantic match per region; a seeded random
      pool draw only when nothing reaches SMART_MATCH_THRESHOLD.
    * ``smart_inserts`` - mostly random; only at ~insert_percent of the
      insert opportunities a strong match (SMART_INSERT_STRONG_THRESHOLD)
      is placed, everything else stays random.
    * ``random_only``   - seeded random draws, no semantic matching.

    Uniqueness: every asset is used at most once while unused pool assets
    remain; once the pool is exhausted an asset may repeat, but never
    back-to-back whenever an alternative exists. Diversity only ever breaks
    ties AFTER relevance and uniqueness.
    """
    slots: list[SmartVisualSlot] = []
    pool = list(entries)
    entry_vectors = {entry.path: concept_vector(entry.concept_key()) for entry in pool}
    used_counts: dict[str, int] = {}
    last_pick: str = ""
    image_duration = clamp_smart_image_duration(profile.image_duration)
    gate_images_first = True

    def _unused(candidate: MediaIndexEntry) -> bool:
        return used_counts.get(candidate.path, 0) == 0

    def _pick_random(reason: str) -> MediaIndexEntry | None:
        nonlocal last_pick
        if not pool:
            return None
        images = [entry for entry in pool if entry.kind == "image"]
        candidates = images if (gate_images_first and images) else pool
        fresh = [entry for entry in candidates if _unused(entry)]
        if not fresh:
            # Pool exhausted: reuse is allowed, but never the asset that was
            # just placed (avoid immediate back-to-back repeats).
            fresh = [entry for entry in candidates if entry.path != last_pick]
        if not fresh:
            fresh = list(candidates)
        chosen = rng.choice(sorted(fresh, key=lambda entry: entry.path))
        return chosen

    for slot in timed:
        visual = SmartVisualSlot(
            start=slot.start,
            end=slot.end,
            topic=" ".join(slot.draft.keywords[:4]) or "general",
            sentence=slot.draft.text[:200],
            keywords=tuple(slot.draft.keywords),
            insert_duration=image_duration,
        )
        slots.append(visual)

        chosen: MediaIndexEntry | None = None
        if profile.mode == SMART_MODE_RANDOM_ONLY:
            chosen = _pick_random("random_only_mode")
            if chosen is not None:
                visual.source_mode = SLOT_SOURCE_RANDOM
                visual.reason = "random_only_mode"
                visual.fallback_mode = SLOT_MODE_FALLBACK_RANDOM_IMAGE
        else:
            query_vector = concept_vector(slot.draft.text)
            scored = sorted(
                (
                    (
                        score_candidate(
                            query_vector, slot.draft.keywords, entry, entry_vectors[entry.path],
                        ),
                        entry,
                    )
                    for entry in pool
                ),
                key=lambda pair: (-pair[0], pair[1].path),
            )
            gate = (
                SMART_INSERT_STRONG_THRESHOLD
                if profile.mode == SMART_MODE_SMART_INSERTS
                else SMART_MATCH_THRESHOLD
            )
            attempt_smart = True
            if profile.mode == SMART_MODE_SMART_INSERTS:
                attempt_smart = rng.random() * 100.0 < float(profile.insert_percent)
            if attempt_smart:
                above = [(score, entry) for score, entry in scored if score >= gate]
                unused_above = [(score, entry) for score, entry in above if _unused(entry)]
                pool_exhausted = all(not _unused(entry) for entry in pool) if pool else True
                if unused_above:
                    if profile.mode == SMART_MODE_SMART_MATCH and profile.randomize_nonce:
                        # Randomize explores alternate GOOD matches: draw from
                        # the top-K still-relevant unused candidates.
                        top_k = unused_above[: max(1, SMART_MATCH_TOP_K)]
                        score, chosen = top_k[rng.randrange(len(top_k))]
                    else:
                        score, chosen = unused_above[0]
                    visual.source_mode = SLOT_SOURCE_SMART
                    visual.reason = "matched"
                    visual.fallback_mode = SLOT_MODE_MATCH
                elif above and pool_exhausted:
                    # EVERY pool asset was already used once - only now is a
                    # relevant asset reused, and never back-to-back whenever
                    # an alternative exists.
                    alternatives = [(score, entry) for score, entry in above if entry.path != last_pick]
                    score, chosen = (alternatives or above)[0]
                    visual.source_mode = SLOT_SOURCE_SMART
                    visual.reason = "matched_pool_exhausted_reuse"
                    visual.fallback_mode = SLOT_MODE_MATCH
                else:
                    chosen = None
                if chosen is not None:
                    visual.score = score
                    signals = _shared_signal_tokens(visual.keywords, chosen)
                    if signals:
                        visual.reason += ":" + "+".join(signals)
            if chosen is None:
                reason = (
                    "smart_insert_random_baseline"
                    if profile.mode == SMART_MODE_SMART_INSERTS and not attempt_smart
                    else "smart_fallback_random_no_relevant_match"
                )
                chosen = _pick_random(reason)
                if chosen is not None:
                    visual.source_mode = SLOT_SOURCE_RANDOM
                    visual.reason = reason
                    visual.fallback_mode = SLOT_MODE_FALLBACK_RANDOM_IMAGE

        if chosen is None:
            visual.reason = "skipped_no_media"
            visual.fallback_mode = SLOT_MODE_SKIPPED
            visual.source_mode = ""
            continue
        visual.selected_kind = chosen.kind
        visual.selected_path = chosen.path
        visual.selected_label = f"{Path(chosen.path).name} [{chosen.kind}, {chosen.category}]"
        used_counts[chosen.path] = used_counts.get(chosen.path, 0) + 1
        last_pick = chosen.path
    return slots


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
    image_transition_type: str = "project",
    image_transition_duration: float | None = None,
    image_visual_effect: str = "none",
    image_visual_effect_intensity: str = "low",
    log=print,
) -> SmartVisualPlan:
    """Build the complete Smart Visual plan BEFORE any rendering happens.

    Phase 33: Smart Visuals is a pure content-aware SELECTION + placement
    engine. It matches the local timeline context against the metadata of
    the existing pool media and never generates anything - there is no
    generation provider, no hidden generation fallback and no generation
    cache path left in this workflow. The ``image_*`` parameters describe
    the profile's dedicated image rendering (Phase 32) and feed the plan
    preview and identity.
    """
    from .image_timeline import (
        normalize_image_transition_choice,
        normalize_visual_effect,
        normalize_visual_effect_intensity,
    )

    image_transition_type = normalize_image_transition_choice(image_transition_type)
    image_visual_effect = normalize_visual_effect(image_visual_effect)
    image_visual_effect_intensity = normalize_visual_effect_intensity(image_visual_effect_intensity)

    plan = SmartVisualPlan(enabled=bool(profile.enabled))
    # Phase 32 preview labels (uniform for all slots of this plan).
    if image_transition_type == "project":
        plan.image_transition_label = "project"
    else:
        duration_text = f" ({image_transition_duration:.2f}s)" if image_transition_duration else ""
        plan.image_transition_label = f"{image_transition_type}{duration_text}"
    plan.image_effect_label = image_visual_effect
    plan.image_effect_intensity_label = image_visual_effect_intensity
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
        # Phase 33: Stage D/E is the content-aware SELECTION pass. No
        # generation provider is ever resolved here: Smart Visuals only
        # picks existing media from the configured pools. Weak matches fall
        # back to a seeded random pool draw - never to generation.
        seed_material = (
            "|".join(str(part) for part in seed_parts)
            + f"|{width}x{height}@{round(float(fps), 6)}"
            + f"|nonce={int(profile.randomize_nonce)}|mode={profile.mode}"
        )
        rng = Random(int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:12], 16))
        plan.slots = assign_smart_visual_selections(timed, entries, profile=profile, rng=rng)
        skipped = sum(1 for slot in plan.slots if not slot.selected_kind)
        smart_count = sum(1 for slot in plan.slots if slot.source_mode == SLOT_SOURCE_SMART)
        random_count = sum(1 for slot in plan.slots if slot.source_mode == SLOT_SOURCE_RANDOM)
        plan.diagnostics.append(
            f"Auswahlmodus {profile.mode}: {smart_count} smart, {random_count} zufällig, "
            f"{skipped} übersprungen (Bilddauer {clamp_smart_image_duration(profile.image_duration):.1f}s)."
        )
        # Phase 33: the legacy Phase-32 generation toggle / fallback policy
        # are INERT settings now (nothing is ever generated), so they must
        # not churn the plan identity. The dedicated image rendering still
        # joins only when it deviates from the project defaults AND at least
        # one image-type visual was selected (pure-video plans are
        # unaffected by image settings).
        phase32_identity: dict = {}
        has_image_visuals = any(
            slot.selected_kind in ("image", "generated") for slot in plan.slots
        )
        if has_image_visuals:
            if image_transition_type != "project" or image_transition_duration is not None:
                phase32_identity["image_transition_type"] = image_transition_type
                phase32_identity["image_transition_duration"] = image_transition_duration
            if image_visual_effect != "none":
                phase32_identity["image_visual_effect"] = image_visual_effect
                phase32_identity["image_visual_effect_intensity"] = image_visual_effect_intensity
        if any(slot.selected_kind for slot in plan.slots):
            plan.identity = smart_plan_identity(
                profile, plan.slots, (width, height, fps),
                phase32=phase32_identity or None,
            )
        log(
            f"Phase 33 Smart Visuals: {plan.selected_count}/{len(plan.slots)} Slot(s) belegt "
            f"(Modus {profile.mode}, Bilddauer {clamp_smart_image_duration(profile.image_duration):.1f}s, "
            f"Inserts {int(profile.insert_percent)}%)."
        )
    except Exception as exc:  # spec section 32: never break the render
        plan.diagnostics.append(f"Smart-Visual-Planung fehlgeschlagen, Fallback auf normale Auswahl: {exc}")
        plan.slots = []
        plan.identity = ""
        log(f"Phase 33 Smart Visuals: Planung übersprungen ({exc.__class__.__name__}).")
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
        # Phase 33: the inserted element uses the profile's single image
        # duration when the plan provides one; legacy plans (insert_duration
        # == 0) keep deriving the duration from the slot's context window.
        duration = _frame_grid(slot.effective_insert_duration, ceil=True)
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
        f"Phase 33 Smart Visuals: {result.count} Visual(s) eingefügt "
        f"({sum(1 for item in result.inserted if item[3] == 'image')} Bild(er), "
        f"{sum(1 for item in result.inserted if item[3] == 'video')} Video(s) - nichts erzeugt)."
    )
    return result
