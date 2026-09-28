# Phase 34: Smart Visuals — Metadata-Driven Matching + Sentence-Anchored Timeline — Delivery

Focused refinement of the delivered Phase-33 Smart Visuals engine (`feb0aee`).
Phase 34 turns the **pre-generated per-asset JSON metadata** into the runtime
matching source of truth and anchors visual changes to **sentence/topic
boundaries**. No redesign of unrelated systems, no timeline-engine rewrite, no
changes to subtitles, ASR, audio timing, transitions, Shorts, rendering or the
typewriter. `main` is untouched; all work is a normal descendant of `feb0aee`
on `arena/01a091fe-videomerger`. No rebase, no force-push.

Hard rules honored:
* **Smart Visuals uses the pre-generated asset metadata as its runtime matching source.**
* **Smart Visuals does not generate images** (and never calls a vision model,
  Qwen-VL/Ollama or any cloud service at runtime).
* **Relevant visual assets begin at the appropriate sentence/topic boundary.**
* **Unused images and videos are not reused while valid alternatives remain.**
* **Existing audio, subtitle, transition, rendering, and synchronization behavior remains preserved.**

## 1. Architecture

```
script ──► per-sentence SentenceQuery (never the whole script at once)
                     │
pool folders ──► sidecar discovery ──► AssetMetadata (parsed once, cached)
                     │                        │
                     └────────► PreparedAsset (evidence items keep their
                                individual relevance_score values)
                                      │
                  weighted score_asset() per (sentence, asset)
                                      │
            uniqueness-aware global assignment + rare-asset reservation
                                      │
        sentence-anchored SmartVisualPlan ──► existing render pipeline
```

New module `app/video_merger/smart_metadata.py` (~800 lines): metadata
discovery/parsing, word-family normalization, sentence queries, weighted
scoring, explainable evidence. `smart_visuals.py` gained an **analyzer
scoring path** inside the existing `assign_smart_visual_selections()`
(three-path split — random_only / legacy lexical / analyzer-weighted — is
chosen per pool) plus the sentence-boundary duration tail; the Phase-33
lexical path is preserved verbatim for pools without analyzer metadata.

Runtime never re-analyzes assets visually, never generates media, never calls
a vision model. Missing metadata ⇒ existing Phase-33 capabilities unchanged.

## 2. Metadata JSON schema (as produced by the analyzer, consumed verbatim)

Matching-relevant fields (all optional; tolerantly discovered):

```json
{
  "keywords": [
    {"keyword": "meditation", "relevance_score": 100,
     "category": "primary subject", "literal_or_semantic": "literal",
     "focus_level": "high", "visible_evidence": "person meditating"}
  ],
  "visual_concepts": [{"concept": "calm", "relevance_score": 60}],
  "direct_uses": ["meditation session recording"],
  "strong_associations": ["wellness spa interior"],
  "contextual_uses": ["lifestyle healthy morning routine"],
  "metaphorical_uses": ["inner peace journey"],
  "search_phrases": ["person meditating"],
  "negative_matches": ["medical treatment"]
}
```

* Individual keyword scores are **preserved, never flattened** — the score of
  the strongest matching keyword dominates the result.
* Discovery: adjacent `<name>.analysis.json`, `_DeepImageAnalysis/json/`
  (including nested relative paths) and `auto` discovery inside the
  configured Smart Visual folders. No user configuration required.
* Identity = the resolved media file path; images and videos are first-class.

## 3. Sentence-anchored timeline (spec sections 5–9)

* One `SentenceQuery` per script sentence: the sentence itself is the primary
  query; the previous sentence's topic is inherited only as a **secondary**
  component (weight `CONTEXT_INHERIT_WEIGHT = 0.5`).
* **Sentence start = visual anchor.** Metadata-driven selections carry
  `sentence_anchored = True` and are inserted at the free timeline boundary
  nearest the sentence start (legacy Phase-33 slots keep midpoint placement).
* `smart_visual_image_duration` (default 5.0 s) is a **target, not an
  absolute**: when the next sentence selects a different topic, the current
  visual is shortened to end at that sentence boundary (min 0.5 s). When the
  next sentence still fits the current visual on its own merits, the image
  keeps its duration (no unnecessary churn).
* Voiceover, subtitles and audio timing are never moved — verified by the
  E2E test comparing the rendered `.srt` byte-for-byte with the baseline.

## 4. Scoring strategy (spec sections 10–15, 41–42)

All weights are centralized constants in `smart_metadata.py`:

| Evidence | Weight |
|---|---|
| Exact keyword / word-family (morphological) / synonym-concept | 1.00 / 0.90 / 0.78 |
| Phrase full / partial ≥50 % / anchor token of short use-phrase | 0.85 / 0.55 / 0.45 |
| Use class: direct / strong / contextual / metaphorical | 1.00 / 0.75 / 0.55 / 0.35 |
| Use-class base scores | 88 / 74 / 60 / 46 |
| Focus: high / medium / low | 1.0 / 0.7 / 0.5 |
| Literal / semantic keyword | 1.0 / 0.85 |
| Negative conflict | ×0.65 each, hard cap 0.25 × strongest evidence |

`final = strongest evidence value + diminishing bonuses (×0.5 decay, capped)
+ capped description support − negative penalties`, scaled 0..100 — **never a
plain average**. One `relevance_score:100` keyword beats many weak generic
keywords. Word families (meditate/meditating/meditation, philosopher/
philosophy/philosophical, concentration/concentrate) are matched via
`word_family()` computed from the original word forms on both query and asset
sides; no uncontrolled synonym expansion.

* **Direct > strong > contextual > metaphorical** evidence ordering (weaker
  classes are never hard-deleted).
* `negative_matches` reduce the score and cap hard conflicts.
* `visual_quality` is a **tie-breaker only** inside a ±2.0 window
  (`compare_with_quality`).

## 5. Global assignment, uniqueness, reservation (spec sections 19–21)

* Uniqueness-aware selection in **all** modes: an asset is used at most once
  while unused assets remain; after pool exhaustion, reuse prefers the
  highest remaining relevance and avoids adjacent repeats.
* Greedy-with-reservation: a sentence that is the ONLY strong claimant for a
  rare asset reserves it; a competing earlier sentence with a near-equivalent
  alternative yields (verified by unit test Z with the spec-21 example).
* Randomize in Smart Match re-draws among semantically relevant candidates
  (top-K above threshold), never arbitrarily; Random Only changes genuinely.

## 6. Duration rules (spec section 8)

Adjacent analyzer-driven slots with different assets: the earlier image
shortens to `max(0.5 s, next.start − current.start)` **only when** the next
sentence scores above the mode gate for a *different* asset AND does not fit
the current asset on the next sentence's own tokens (bare query, no inherited
context — an echoed topic can never masquerade as the next sentence's subject).

## 7. Analyze / Randomize (spec sections 22–24)

Analyze Timeline shows per slot: timestamp, duration, type, filename, source
(Smart Match / Random Fallback / Random / Skipped), score, matched topic and
the **few strongest** evidence terms (not 50). The analyzed plan is the real
plan reused by render; Analyze triggers no render. Randomize honors
uniqueness and semantic relevance (Smart Match) or genuinely changes
(Random Only), seeded and reproducible.

## 8. Performance & cache (spec sections 27–28)

* Pool scan reuses the existing media index; analysis payloads attach to the
  cached index entries (`analysis_signature` = sidecar size|mtime), so a
  changed/added sidecar re-parses exactly that asset.
* Assets are prepared once per plan (`PreparedAsset`), scored against a
  precomputed top-K, then assigned globally.
* Large-pool unit test: **250 image + 250 video metadata records** are
  discovered, indexed once, assigned deterministically and fast
  (< a few seconds), with identical results across repeated runs.

## 9. Tests

### New Phase-34 suites
* `tests/test_phase34_metadata.py` — **37 unit tests** (spec 36/37/39):
  sidecar discovery (adjacent, `_DeepImageAnalysis`, nested, same-filename
  distinctness), weighted matching A–H (relevance dominance, morphological +
  synonym, phrase, direct>metaphorical, strong-assoc vs weak direct,
  negatives, focus weight), context inheritance I, segmentation/anchoring J–N
  (incl. safe shortening + no-churn), modes Q–T (incl. the random_only
  no-scoring trap), uniqueness U–W, Randomize X–Y, rare-asset reservation Z,
  Analyze transparency AA–AB, no-generation AC, cache AD–AE, 500-asset
  large pool, video+image normalization.
* `tests/test_phase34_e2e.py` — **2 E2E tests with real FFmpeg renders**:
  meditation/concentration/exercise script + analyzer sidecars vs one
  unrelated asset; plan anchoring/order/uniqueness/provenance; rendered
  output begins each sentence with its topic visual, voiceover + subtitles
  unchanged, safe durations, no duplication, nothing generated.

### Regression results (this branch vs Phase-34 baseline `feb0aee`)
| Suite | Baseline | Now |
|---|---|---|
| Full non-E2E | 1139 passed | **1176 passed / 0 failed** (1139 + 37 new) |
| Full E2E | 115 passed / 17 known failed / 6 skipped | **117 passed / 17 known failed / 6 skipped** (+2 new) |
| Phase-31/32/33 unit+GUI | 186 passed | 186 passed |
| Phase-31/32/33 E2E | 11 + 12 + 11 passed | 11 + 12 + 11 passed |

### Known pre-existing E2E failures (17, unchanged from baseline)
`test_transition_renders` ×5, `test_visual_effects` ×1,
`test_121_loop_behavior` ×1, `test_121_subtitle_workflow` ×2,
`test_123_intro_e2e` ×2, `test_130_windows_subtitle_paths` ×2,
`test_131_oneclick_reuse` ×1, `test_132_cross_dissolve_music` ×1,
`test_e2e_matrix` ×1, `test_main_workflow_e2e` ×1 — sandbox/environment
limitations present before Phase 34; not weakened, not deleted, listed here
to be compared rather than hidden.

## 10. Files

* `app/video_merger/smart_metadata.py` (new) — metadata engine.
* `app/video_merger/smart_visuals.py` — analyzer scoring path, sentence
  anchoring, duration tail, plan transparency fields.
* `app/video_merger/gui/main_window.py` — Analyze renderer shows source label
  + strongest matched terms (additive).
* `tests/test_phase34_metadata.py`, `tests/test_phase34_e2e.py` (new).

## 11. Git

Commits on `arena/01a091fe-videomerger`:
`75d186d` engine · `8c3a2f9` unit tests · `40026dc` E2E + sentence-anchored
insertion · `62abf2d` docs. Working tree clean; no force-push; `main`
untouched.
