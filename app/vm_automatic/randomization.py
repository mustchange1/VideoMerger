"""Per-job clip randomization for VM Automatic.

The eligible clip pool is resolved with the EXISTING VideoMerger rules
(``discover_videos`` + the persisted ``ProjectOrderStore`` active order +
``GeneratedOutputStore`` exclusions). VM Automatic then performs the
equivalent of the existing GUI "Randomize Order" action — the same unbiased
Fisher-Yates permutation from ``project_order.randomize_order`` — but the
result is JOB-LOCAL: it is stored on the job (with its seed) and is never
persisted back into the user's master project order. The user's saved
manual ordering therefore remains intact before, during and after the job.

Randomization applies to CLIP ORDER ONLY: folder rules, extension rules,
generated-output exclusion and the manual-order reconciliation are all
applied first (eligibility), and the shuffle never adds, removes or
reorders files outside the eligible pool.
"""
from __future__ import annotations

import random
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..video_merger.discovery import discover_videos
from ..video_merger.project_order import ProjectOrderStore, randomize_order


def new_seed() -> int:
    """Cryptographically sourced 32-bit seed for a new job."""
    return secrets.randbits(32)


@dataclass(frozen=True)
class JobClipOrder:
    paths: tuple[Path, ...]
    names: tuple[str, ...]
    seed: int
    pool_size: int


def resolve_eligible_pool(
    clip_pool_folder: Path | str,
    order_store: ProjectOrderStore,
    generated_paths: set[str],
) -> list[Path]:
    """The normal eligible VideoMerger clip pool for the configured folder.

    Reuses ``discover_videos`` unchanged: direct files only, supported
    extensions, temp/hidden/generated names excluded, the persisted active
    (natural/manual) order reconciled. This call behaves exactly like the
    GUI's "Analyze Inputs" discovery step.
    """
    return discover_videos(
        clip_pool_folder,
        order_store=order_store,
        excluded_paths=generated_paths,
    )


def randomize_pool(
    paths: list[Path],
    seed: int,
    log: Callable[[str], None] | None = None,
) -> JobClipOrder:
    """Deterministic, seed-replayable job-local shuffle of the pool.

    * same seed → same order (retries reuse the seed → same order);
    * a different seed → (with overwhelming probability) a different order;
    * if the first permutation happens to equal the original order and the
      pool has more than one clip, one fallback seed is used so a "new" job
      is visibly different whenever the pool allows it.
    """
    log = log or (lambda _message: None)
    by_name: dict[str, Path] = {}
    for path in paths:
        by_name.setdefault(path.name, path)
    names = list(by_name)
    if not names:
        return JobClipOrder((), (), int(seed), 0)
    if len(names) == 1:
        return JobClipOrder((by_name[names[0]],), (names[0],), int(seed), 1)

    effective_seed = int(seed)
    shuffled = randomize_order(names, rng=random.Random(effective_seed))
    if shuffled == names:
        effective_seed += 1
        shuffled = randomize_order(names, rng=random.Random(effective_seed))
        log(f"VM Automatic: Zufallsfolge glatt gleich der Ursprungsreihenfolge – Seed {effective_seed} verwandt.")
    return JobClipOrder(
        paths=tuple(by_name[name] for name in shuffled),
        names=tuple(shuffled),
        seed=effective_seed,
        pool_size=len(names),
    )
