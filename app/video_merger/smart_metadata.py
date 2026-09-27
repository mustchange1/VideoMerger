"""Phase 34: metadata-driven matching for Smart Visuals.

The pre-generated JSON metadata of every image/video asset (produced by the
external asset analyzer) is the authoritative visual knowledge base. The
runtime NEVER analyzes pixels, NEVER generates images and NEVER calls a
vision model to decide which existing asset to use - it only consumes the
analyzer's JSON:

* ``keywords[].keyword`` + ``keywords[].relevance_score`` (0..100) are
  first-class ranking signals (they are NOT flattened into one string),
* ``visual_concepts`` / ``direct_uses`` / ``strong_associations`` /
  ``contextual_uses`` / ``metaphorical_uses`` / ``search_phrases`` add
  progressively weaker evidence (DIRECT > STRONG ASSOCIATION > CONTEXTUAL
  > METAPHORICAL > random fallback),
* ``negative_matches`` actively suppress false positives,
* ``visual_quality`` is a tie-breaker only - never a semantic signal.

Sidecar discovery is tolerant and automatic (spec section 3):

* adjacent: ``<folder>/<name>.jpg`` + ``<folder>/<name>.analysis.json``
* analyzer layout: ``<root>/_DeepImageAnalysis/json/<name>.analysis.json``
  (also honoring nested relative paths),
* asset identity is ALWAYS the resolved file path - two folders may legally
  contain the same filename.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .smart_visuals import (
    STOPWORDS,
    _WORD_TO_CONCEPT,
    content_tokens,
    strip_suffix,
    tokenize_text,
)

# ---------------------------------------------------------------------------
# Central weighted scoring model (spec section 11). These are the ONLY
# tuning constants of the Phase-34 matcher - no magic numbers elsewhere.
# ---------------------------------------------------------------------------
#: How a query token matches an evidence term (A-D).
MATCH_EXACT = 1.00
MATCH_MORPHOLOGICAL = 0.90
MATCH_SYNONYM = 0.78
MATCH_PHRASE = 0.85
MATCH_PHRASE_PARTIAL = 0.55
MATCH_PHRASE_ANCHOR = 0.45   # one anchor token of a short use-phrase

#: Analyzer evidence hierarchy (spec section 14): direct > strong
#: association > contextual > metaphorical. Weak classes are NEVER deleted -
#: they stay as progressively weaker evidence.
USE_CLASS_FACTORS = {
    "direct": 1.00,
    "strong_association": 0.75,
    "contextual": 0.55,
    "metaphorical": 0.35,
}
#: Fixed relevance given to a matched use/phrase suggestion (the analyzer
#: does not score these lists; the class factor above provides the ranking).
USE_CLASS_BASE_SCORES = {
    "direct": 88.0,
    "strong_association": 74.0,
    "contextual": 60.0,
    "metaphorical": 46.0,
}
SEARCH_PHRASE_BASE_SCORE = 66.0
CONCEPT_EVIDENCE_FACTOR = 0.92

#: Focus-level weighting (spec section 11 J).
FOCUS_FACTORS = {"primary": 1.00, "supporting": 0.70, "secondary": 0.50}
DEFAULT_FOCUS_FACTOR = 0.60

#: Literal vs semantic keyword evidence.
LITERAL_FACTORS = {"literal": 1.00, "semantic": 0.85}
DEFAULT_LITERAL_FACTOR = 0.90

#: Aggregation: the strongest relevant keyword dominates; additional
#: evidence adds small diminishing bonuses (never averaged away).
EVIDENCE_BONUS_RATE = 0.10
EVIDENCE_BONUS_CAP = 12.0
DESCRIPTION_SUPPORT_PER_TOKEN = 4.0
DESCRIPTION_SUPPORT_MAX = 15.0

#: Negative-match handling (spec section 15).
NEGATIVE_PENALTY_RATE = 0.35
NEGATIVE_CONFLICT_CAP = 0.25

#: The previous sentence's topic joins the query as a secondary component
#: (spec section 42: pronoun/continuation sentences).
CONTEXT_INHERIT_WEIGHT = 0.50

#: Semantic relevance comes first; quality only breaks near-ties (section 41).
QUALITY_TIEBREAK_WINDOW = 2.0
QUALITY_TIEBREAK_BONUS = 0.02

SCORE_SCALE = 100.0

#: Sidecar discovery (spec section 3).
ANALYSIS_SUFFIX = ".analysis.json"
DEEP_ANALYSIS_DIRNAME = "_DeepImageAnalysis"

#: Default relevance for keyword entries that carry no numeric score.
DEFAULT_KEYWORD_RELEVANCE = 55.0


# ---------------------------------------------------------------------------
# Data model (spec section 4 - both image and video assets normalize into
# this one internal representation)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class KeywordEvidence:
    term: str
    relevance: float = DEFAULT_KEYWORD_RELEVANCE
    category: str = ""
    literal: str = "literal"          # literal | semantic
    focus: str = "primary"            # primary | supporting | secondary
    visible_evidence: str = ""


@dataclass(slots=True)
class AssetMetadata:
    asset_path: str
    asset_type: str                   # "image" | "video"
    identity: str = ""
    description: str = ""
    keywords: list[KeywordEvidence] = field(default_factory=list)
    concepts: list[tuple[str, float]] = field(default_factory=list)
    uses: dict[str, tuple[str, ...]] = field(default_factory=dict)
    search_phrases: tuple[str, ...] = ()
    negative_matches: tuple[str, ...] = ()
    quality: float = -1.0             # 0..1 normalized; -1 = unknown
    source_metadata_path: str = ""
    analyzer_version: str = ""
    vision_model: str = ""

    @property
    def has_analyzer_metadata(self) -> bool:
        return bool(self.source_metadata_path)


@dataclass(slots=True)
class MatchEvidence:
    term: str
    relevance: float
    kind: str                         # keyword | concept | use_* | phrase | description
    focus: str
    literal: str
    match: str                        # exact | morphological | synonym | phrase
    matched_query: str
    value: float


@dataclass(slots=True)
class MatchResult:
    score: float = 0.0                # 0..100
    evidence: list[MatchEvidence] = field(default_factory=list)
    negative_conflicts: list[str] = field(default_factory=list)
    description_support: float = 0.0

    def strongest_terms(self, limit: int = 3) -> list[tuple[str, float]]:
        """Few strongest pieces of evidence for Analyze Timeline (section 22)."""
        return [(item.term, round(item.relevance, 1)) for item in self.evidence[:limit]]

    def explanation(self, limit: int = 3) -> str:
        """Human-readable evidence lines (spec section 40)."""
        lines = []
        for item in self.evidence[:limit]:
            lines.append(
                f"{item.term}: {item.relevance:.0f} x {item.kind.replace('use_', '')} "
                f"x {item.focus} [{item.match} \"{item.matched_query}\"]"
            )
        if self.negative_conflicts:
            lines.append("negative conflicts: " + "; ".join(sorted(set(self.negative_conflicts))))
        elif lines:
            lines.append("negative conflicts: none")
        return " | ".join(lines)


# ---------------------------------------------------------------------------
# Sidecar discovery (spec section 3)
# ---------------------------------------------------------------------------
def find_analysis_sidecar(asset_path: Path | str, folder_root: Path | str | None) -> Path | None:
    """Best corresponding analyzer sidecar for one asset, or None.

    Preference order: a sidecar directly beside the asset
    (``<stem>.analysis.json``), then the analyzer's central layout
    ``<root>/_DeepImageAnalysis/json/...`` (nested relative path first,
    then the bare filename). Never requires user configuration.
    """
    asset = Path(asset_path)
    stem = asset.with_suffix("").name
    candidates: list[Path] = [
        asset.parent / f"{stem}{ANALYSIS_SUFFIX}",
        asset.parent / f"{asset.name}{ANALYSIS_SUFFIX}",
    ]
    if folder_root is not None:
        root = Path(str(folder_root).strip()).expanduser()
        deep = root / DEEP_ANALYSIS_DIRNAME / "json"
        try:
            relative = asset.resolve().relative_to(root.resolve())
            candidates.append(deep / Path(str(relative) + ANALYSIS_SUFFIX))
        except (ValueError, OSError):
            pass
        candidates.append(deep / f"{stem}{ANALYSIS_SUFFIX}")
        candidates.append(deep / f"{asset.name}{ANALYSIS_SUFFIX}")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def analysis_signature(path: Path | None) -> str:
    """Cheap mtime+size identity used for cache invalidation (section 28)."""
    if path is None:
        return ""
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}|{stat.st_mtime_ns}"


# ---------------------------------------------------------------------------
# Parsing (tolerant - a broken sidecar never breaks indexing)
# ---------------------------------------------------------------------------
def _to_text_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            parts.extend(_to_text_list(item))
        return parts
    if isinstance(value, dict):
        parts = []
        for nested in value.values():
            parts.extend(_to_text_list(nested))
        return parts
    text = str(value).strip()
    return [text] if text else []


def _parse_keywords(raw: object) -> list[KeywordEvidence]:
    """Preserve individual numeric scores - never flatten (section 2)."""
    result: list[KeywordEvidence] = []
    if not isinstance(raw, (list, tuple)):
        return result
    for item in raw:
        if isinstance(item, str):
            term = item.strip()
            if term:
                result.append(KeywordEvidence(term=term))
            continue
        if not isinstance(item, dict):
            continue
        term = str(item.get("keyword") or item.get("term") or item.get("name") or "").strip()
        if not term:
            continue
        try:
            relevance = float(item.get("relevance_score", item.get("score", DEFAULT_KEYWORD_RELEVANCE)))
        except (TypeError, ValueError):
            relevance = DEFAULT_KEYWORD_RELEVANCE
        result.append(
            KeywordEvidence(
                term=term,
                relevance=max(0.0, min(100.0, relevance)),
                category=str(item.get("category") or ""),
                literal=str(item.get("literal_or_semantic") or "literal").casefold(),
                focus=str(item.get("focus_level") or "primary").casefold(),
                visible_evidence=str(item.get("visible_evidence") or ""),
            )
        )
    return result


def _parse_concepts(raw: object) -> list[tuple[str, float]]:
    result: list[tuple[str, float]] = []
    if not isinstance(raw, (list, tuple)):
        return result
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                result.append((item.strip(), DEFAULT_KEYWORD_RELEVANCE))
            continue
        if isinstance(item, dict):
            term = str(item.get("concept") or item.get("keyword") or item.get("name") or "").strip()
            if not term:
                continue
            try:
                score = float(item.get("relevance_score", item.get("score", DEFAULT_KEYWORD_RELEVANCE)))
            except (TypeError, ValueError):
                score = DEFAULT_KEYWORD_RELEVANCE
            result.append((term, max(0.0, min(100.0, score))))
    return result


def _parse_quality(raw: object) -> float:
    if raw is None:
        return -1.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))
    text = str(raw).strip().casefold()
    order = ("poor", "low", "fair", "medium", "good", "high", "excellent")
    for index, label in enumerate(order):
        if label in text:
            return (index + 1) / len(order)
    try:
        value = float(text)
        return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))
    except ValueError:
        return -1.0


def asset_metadata_from_dict(
    asset_path: str,
    asset_type: str,
    payload: dict,
    source_metadata_path: str = "",
) -> AssetMetadata:
    """Normalize one analyzer JSON payload into the internal representation."""
    if not isinstance(payload, dict):
        payload = {}
    uses: dict[str, tuple[str, ...]] = {}
    for key in ("direct_uses", "strong_associations", "contextual_uses", "metaphorical_uses"):
        uses[key] = tuple(_to_text_list(payload.get(key)))
    return AssetMetadata(
        asset_path=asset_path,
        asset_type=asset_type,
        identity=str(payload.get("identity") or ""),
        description=" ".join(_to_text_list(payload.get("detailed_description") or payload.get("description"))),
        keywords=_parse_keywords(payload.get("keywords")),
        concepts=_parse_concepts(payload.get("visual_concepts")),
        uses=uses,
        search_phrases=tuple(_to_text_list(payload.get("search_phrases"))),
        negative_matches=tuple(_to_text_list(payload.get("negative_matches"))),
        quality=_parse_quality(payload.get("visual_quality")),
        source_metadata_path=source_metadata_path,
        analyzer_version=str(payload.get("analyzer_version") or ""),
        vision_model=str(payload.get("vision_model") or ""),
    )


def load_asset_metadata(
    asset_path: Path | str,
    folder_root: Path | str | None = None,
    asset_type: str = "image",
) -> AssetMetadata | None:
    """Discover + parse the analyzer sidecar for one asset (None if absent)."""
    sidecar = find_analysis_sidecar(asset_path, folder_root)
    if sidecar is None:
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return asset_metadata_from_dict(str(asset_path), asset_type, payload, str(sidecar))


def analysis_payload_for_cache(
    asset_path: Path | str,
    folder_root: Path | str | None = None,
) -> tuple[dict, str]:
    """(raw payload, signature) for the index cache; ({}, "") when absent."""
    sidecar = find_analysis_sidecar(asset_path, folder_root)
    if sidecar is None:
        return {}, ""
    signature = analysis_signature(sidecar)
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}, signature
    return (payload if isinstance(payload, dict) else {}), signature


# ---------------------------------------------------------------------------
# Word-family normalization (spec section 12): meditate / meditating /
# meditation and philosopher / philosophy / philosophical must resolve into
# compatible conceptual families. Controlled local rules only - no
# uncontrolled synonym expansion.
# ---------------------------------------------------------------------------
_FAMILY_MIN_STEM = 3


def word_family(token: str) -> str:
    """Canonical word-family key for one lowercased token."""
    t = str(token or "").casefold()
    if len(t) < _FAMILY_MIN_STEM + 1:
        return t

    def _cut(suffix: str, append: str = "", min_stem: int = _FAMILY_MIN_STEM) -> str | None:
        if t.endswith(suffix) and len(t) - len(suffix) + len(append) >= min_stem:
            return t[: len(t) - len(suffix)] + append
        return None

    # Derivational noun/verb families first (first match wins).
    for suffix, append in (
        ("ations", "ate"), ("ation", "ate"), ("ating", "ate"), ("ated", "ate"),
        ("ates", "ate"),
        ("tions", "te"), ("tion", "te"),
    ):
        mapped = _cut(suffix, append)
        if mapped is not None:
            return mapped
    # Inflectional + derivational stripping. "ical"/"ic" keep a longer stem
    # guard so real words survive ("medical" must not collapse to "med").
    for suffix in (
        "ings", "ing", "ments", "ment", "ness",
        "ers", "er", "ors", "or", "ies", "ses", "xes", "zes", "ches", "shes",
        "es", "ed", "ly", "us",
    ):
        if suffix == "ies":
            mapped = _cut(suffix, "y")
        else:
            mapped = _cut(suffix)
        if mapped is not None:
            return mapped
    for suffix in ("ical", "ic"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 4:
            return t[: len(t) - len(suffix)]
    if not t.endswith("ss"):
        mapped = _cut("s")
        if mapped is not None:
            return mapped
    if not t.endswith("ey"):
        mapped = _cut("y")
        if mapped is not None:
            return mapped
    return t


def sentence_content_tokens(text: str) -> list[str]:
    """Lowercased, stopword-free tokens WITHOUT suffix stripping.

    The Phase-34 matcher derives word families from the ORIGINAL word forms
    (``word_family``); running the light suffix stemmer first would destroy
    derivational families (``meditation`` must stay ``meditation`` so it can
    meet ``meditate``/``meditating`` in one family).
    """
    result: list[str] = []
    for token in tokenize_text(text):
        if token in STOPWORDS or len(token) < 2:
            continue
        result.append(token)
    return result


def family_tokens(text: str) -> list[tuple[str, str]]:
    """(raw content token, word-family key) pairs for one text."""
    return [(token, word_family(token)) for token in sentence_content_tokens(text)]


def family_key_set(text: str) -> frozenset:
    """Word-family keys of one text (used for negative/description sets)."""
    return frozenset(family for _raw, family in family_tokens(text))


# ---------------------------------------------------------------------------
# Query representation (spec sections 5, 6, 42)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SentenceQuery:
    """The script sentence IS the query (never the whole script at once)."""
    sentence_index: int
    text: str
    tokens: tuple[str, ...] = ()              # primary content tokens
    context_tokens: tuple[str, ...] = ()      # previous sentence (secondary)

    @property
    def all_tokens(self) -> tuple[str, ...]:
        return self.tokens + self.context_tokens


def build_sentence_queries(sentences: list[str]) -> list[SentenceQuery]:
    """One independent query per sentence; continuation sentences inherit the
    previous topic as a SECONDARY component only (evaluated independently).

    Tokens are the raw content words (no suffix stripping) so word-family
    matching sees the original forms (spec sections 5, 6, 12, 42).
    """
    queries: list[SentenceQuery] = []
    previous: tuple[str, ...] = ()
    for index, sentence in enumerate(sentences):
        tokens = tuple(sentence_content_tokens(sentence))
        queries.append(
            SentenceQuery(
                sentence_index=index,
                text=sentence,
                tokens=tokens,
                context_tokens=previous,
            )
        )
        previous = tokens
    return queries


# ---------------------------------------------------------------------------
# Prepared assets (normalized ONCE per index load, section 27)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _EvidenceItem:
    label: str
    tokens: tuple                    # ((raw, word_family), ...) pairs
    relevance: float
    class_factor: float
    focus_factor: float
    literal_factor: float
    kind: str


@dataclass(slots=True)
class PreparedAsset:
    asset_path: str
    items: tuple[_EvidenceItem, ...]
    negative_token_sets: tuple[tuple[str, frozenset], ...]
    description_tokens: frozenset
    quality: float


def _token_forms(token: str) -> tuple[str, str, str, str]:
    """(raw, stripped, family, concept) forms used by the matcher.

    ``family`` is the Phase-34 word-family key (meditate/meditating/
    meditation -> one family); ``concept`` is the existing local synonym
    concept. Morphological matching runs on the family key, so analyzer
    keywords and script word forms meet even when the light suffix stemmer
    keeps them apart.
    """
    stripped = strip_suffix(token)
    family = word_family(token)
    concept = _WORD_TO_CONCEPT.get(token) or _WORD_TO_CONCEPT.get(stripped) or ""
    return token, stripped, family, concept


def prepare_asset(metadata: AssetMetadata) -> PreparedAsset:
    """Flatten one asset's analyzer metadata into matchable evidence items.

    Every evidence item keeps its tokens as ``(raw, word_family)`` pairs so
    the matcher can distinguish exact from morphological matches while the
    numeric ``relevance_score`` values stay attached to their keyword.
    """
    items: list[_EvidenceItem] = []
    for entry in metadata.keywords:
        tokens = tuple(family_tokens(entry.term))
        if not tokens:
            continue
        items.append(
            _EvidenceItem(
                label=entry.term,
                tokens=tokens,
                relevance=entry.relevance,
                class_factor=1.0,
                focus_factor=FOCUS_FACTORS.get(entry.focus, DEFAULT_FOCUS_FACTOR),
                literal_factor=LITERAL_FACTORS.get(entry.literal, DEFAULT_LITERAL_FACTOR),
                kind="keyword",
            )
        )
    for term, score in metadata.concepts:
        tokens = tuple(family_tokens(term))
        if not tokens:
            continue
        items.append(
            _EvidenceItem(
                label=term,
                tokens=tokens,
                relevance=score,
                class_factor=CONCEPT_EVIDENCE_FACTOR,
                focus_factor=1.0,
                literal_factor=DEFAULT_LITERAL_FACTOR,
                kind="concept",
            )
        )
    for raw_class, phrases in metadata.uses.items():
        # The analyzer schema stores ``direct_uses`` / ``strong_associations``
        # / ``contextual_uses`` / ``metaphorical_uses``; the factor tables
        # are keyed by the evidence class itself.
        class_name = str(raw_class).replace("_uses", "").replace("_associations", "_association")
        factor = USE_CLASS_FACTORS.get(class_name, 0.35)
        base = USE_CLASS_BASE_SCORES.get(class_name, 45.0)
        for phrase in phrases:
            tokens = tuple(family_tokens(phrase))
            if not tokens:
                continue
            items.append(
                _EvidenceItem(
                    label=phrase,
                    tokens=tokens,
                    relevance=base,
                    class_factor=factor,
                    focus_factor=1.0,
                    literal_factor=1.0,
                    kind=f"use_{class_name}",
                )
            )
    for phrase in metadata.search_phrases:
        tokens = tuple(family_tokens(phrase))
        if not tokens:
            continue
        items.append(
            _EvidenceItem(
                label=phrase,
                tokens=tokens,
                relevance=SEARCH_PHRASE_BASE_SCORE,
                class_factor=0.8,
                focus_factor=1.0,
                literal_factor=DEFAULT_LITERAL_FACTOR,
                kind="phrase",
            )
        )
    negatives: list[tuple[str, frozenset]] = []
    for phrase in metadata.negative_matches:
        tokens = family_key_set(phrase)
        if tokens:
            negatives.append((phrase, tokens))
    return PreparedAsset(
        asset_path=metadata.asset_path,
        items=tuple(items),
        negative_token_sets=tuple(negatives),
        description_tokens=family_key_set(metadata.description),
        quality=metadata.quality,
    )


def _query_token_index(query: SentenceQuery) -> dict:
    """Token forms for primary + context tokens.

    Indexed by raw, stripped AND word-family key so a single lookup answers
    exact vs morphological matching for any evidence token.
    """
    primary: dict[str, tuple[float, str]] = {}
    for token in query.tokens:
        raw, stripped, family, _concept = _token_forms(token)
        primary.setdefault(raw, (1.0, raw))
        primary.setdefault(stripped, (1.0, raw))
        primary.setdefault(family, (1.0, raw))
    context: dict[str, tuple[float, str]] = {}
    for token in query.context_tokens:
        raw, stripped, family, _concept = _token_forms(token)
        context.setdefault(raw, (CONTEXT_INHERIT_WEIGHT, raw))
        context.setdefault(stripped, (CONTEXT_INHERIT_WEIGHT, raw))
        context.setdefault(family, (CONTEXT_INHERIT_WEIGHT, raw))
    return {"primary": primary, "context": context}


def _match_item_tokens(
    item_tokens: tuple,
    query: SentenceQuery,
    index: dict,
) -> tuple[float, str, str]:
    """(match_factor, matched_query_token, match_kind) or (0, "", "").

    ``item_tokens`` is a tuple of ``(raw, word_family)`` pairs.
    """
    if len(item_tokens) == 1:
        raw, family = item_tokens[0]
        _r, stripped, item_family, concept = _token_forms(raw)
        for bucket_name in ("primary", "context"):
            bucket = index[bucket_name]
            # A. exact keyword match (identical surface form)
            if raw in bucket and bucket[raw][1] == raw:
                return MATCH_EXACT, bucket[raw][1], "exact"
        for bucket_name in ("primary", "context"):
            bucket = index[bucket_name]
            # B. normalized word-form / word-family match
            if item_family in bucket or stripped in bucket:
                key = item_family if item_family in bucket else stripped
                return MATCH_MORPHOLOGICAL, bucket[key][1], "morphological"
        # C. synonym / semantic-equivalent match
        if concept:
            for bucket_name in ("primary", "context"):
                tokens = query.tokens if bucket_name == "primary" else query.context_tokens
                for raw_token in tokens:
                    if _token_forms(raw_token)[3] == concept:
                        return MATCH_SYNONYM, raw_token, "synonym"
        return 0.0, "", ""

    # D. phrase match: count how many of the evidence tokens are present in
    # the query (exact or word-family match). Full coverage is a phrase
    # match; a clear majority still counts partially; a single anchor token
    # in a long use-phrase contributes weak support (sections 11 D, 14).
    matched_tokens: list[str] = []
    for raw, family in item_tokens:
        _r, stripped, item_family, concept = _token_forms(raw)
        found = ""
        bucket = index["primary"]
        if raw in bucket:
            found = bucket[raw][1]
        elif item_family in bucket:
            found = bucket[item_family][1]
        elif stripped in bucket:
            found = bucket[stripped][1]
        elif concept:
            for raw_token in query.tokens:
                if _token_forms(raw_token)[3] == concept:
                    found = raw_token
                    break
        if found:
            matched_tokens.append(found)
    if not matched_tokens:
        return 0.0, "", ""
    ratio = len(matched_tokens) / len(item_tokens)
    if ratio >= 0.999:
        return MATCH_PHRASE, " ".join(matched_tokens), "phrase"
    if ratio >= 0.5:
        return MATCH_PHRASE_PARTIAL, " ".join(matched_tokens), "phrase"
    if len(item_tokens) <= 4:
        return MATCH_PHRASE_ANCHOR, " ".join(matched_tokens), "phrase"
    return 0.0, "", ""


def _query_concepts(query: SentenceQuery) -> set[str]:
    concepts = set()
    for token in query.all_tokens:
        concept = _token_forms(token)[3]
        if concept:
            concepts.add(concept)
    return concepts


def score_asset(query: SentenceQuery, prepared: PreparedAsset) -> MatchResult:
    """Weighted 0..100 relevance for one sentence query vs one asset.

    final = strongest relevant evidence
          + diminishing bonuses for further evidence
          + capped description support
          - negative-match penalties
    One highly relevant keyword dominates many weak generic keywords - the
    score is never a plain average (spec section 11).
    """
    if not query.tokens and not query.context_tokens:
        return MatchResult()
    index = _query_token_index(query)

    matched: list[MatchEvidence] = []
    for item in prepared.items:
        factor, matched_query, kind = _match_item_tokens(item.tokens, query, index)
        if factor <= 0:
            continue
        # Sentence context is stronger than inherited paragraph context
        # (spec section 5): evidence matched only via the previous
        # sentence's topic carries the secondary weight.
        context_weight = 1.0 if _in_primary(item.tokens, query) else CONTEXT_INHERIT_WEIGHT
        value = (
            item.relevance
            * factor
            * item.class_factor
            * item.focus_factor
            * item.literal_factor
            * context_weight
        )
        matched.append(
            MatchEvidence(
                term=item.label,
                relevance=item.relevance,
                kind=item.kind,
                focus="primary" if context_weight >= 1.0 else "context",
                literal="literal" if factor >= MATCH_MORPHOLOGICAL else "semantic",
                match=kind,
                matched_query=matched_query,
                value=round(value, 3),
            )
        )
    matched.sort(key=lambda evidence: (-evidence.value, evidence.term))

    description_support = 0.0
    if matched and prepared.description_tokens:
        query_families = {word_family(token) for token in query.tokens}
        shared = len(prepared.description_tokens & query_families)
        description_support = min(
            DESCRIPTION_SUPPORT_MAX, DESCRIPTION_SUPPORT_PER_TOKEN * shared
        )

    score = 0.0
    if matched:
        score = matched[0].value
        bonus = 0.0
        decay = 1.0
        for extra in matched[1:]:
            decay *= 0.5
            bonus += extra.value * decay
        score += min(EVIDENCE_BONUS_CAP, EVIDENCE_BONUS_RATE * bonus * 10.0)
        score += description_support

    # Negative matches suppress false positives (spec section 15). The
    # negative token sets are word-family keys; a conflict exists when the
    # query literally shares the negative's words or when a negative word
    # resolves to a concept the query is clearly about ("medical treatment"
    # clashes with a health/wellness script).
    conflicts: list[str] = []
    if matched and prepared.negative_token_sets:
        query_concepts = _query_concepts(query)
        query_family_keys = {word_family(token) for token in query.all_tokens}
        for phrase, tokens in prepared.negative_token_sets:
            literal_overlap = len(tokens & query_family_keys) >= max(
                1, (len(tokens) + 1) // 2
            )
            concept_clash = False
            for token in tokens:
                concept = (
                    _WORD_TO_CONCEPT.get(token)
                    or _WORD_TO_CONCEPT.get(strip_suffix(token))
                    or ""
                )
                if concept and concept in query_concepts:
                    concept_clash = True
                    break
            if literal_overlap or concept_clash:
                conflicts.append(phrase)
    for _conflict in conflicts:
        score *= (1.0 - NEGATIVE_PENALTY_RATE)
    if len(conflicts) >= 1 and matched:
        # An explicit negative conflict can never be overridden by generic
        # positive keywords: cap the remaining score hard.
        cap = matched[0].value * NEGATIVE_CONFLICT_CAP
        score = min(score, cap + description_support)

    score = max(0.0, min(SCORE_SCALE, score))
    return MatchResult(
        score=round(score, 2),
        evidence=matched,
        negative_conflicts=conflicts,
        description_support=round(description_support, 2),
    )


def _in_primary(item_tokens: tuple, query: SentenceQuery) -> bool:
    """True when the evidence matches the sentence itself (not only the
    inherited previous-topic context)."""
    first_raw, first_family = item_tokens[0]
    for token in query.tokens:
        _raw, _stripped, family, _concept = _token_forms(token)
        if token == first_raw or family == first_family:
            return True
    return False


def compare_with_quality(
    left: tuple[float, float],
    right: tuple[float, float],
) -> int:
    """Semantic relevance first; quality breaks near-ties only (section 41).

    Each side is ``(score, quality_or_-1)``. Returns -1 when left wins.
    """
    left_score, left_quality = left
    right_score, right_quality = right
    if abs(left_score - right_score) > QUALITY_TIEBREAK_WINDOW:
        return -1 if left_score > right_score else 1
    if left_quality >= 0 and right_quality >= 0 and left_quality != right_quality:
        return -1 if left_quality > right_quality else 1
    return -1 if left_score >= right_score else 1


# ---------------------------------------------------------------------------
# Analyze Timeline helpers (spec sections 22, 40)
# ---------------------------------------------------------------------------
def format_matched_terms(result: MatchResult, limit: int = 3) -> str:
    """`meditation (100), mindfulness (92), mental clarity (82)`."""
    return ", ".join(f"{term} ({score:.0f})" for term, score in result.strongest_terms(limit))
