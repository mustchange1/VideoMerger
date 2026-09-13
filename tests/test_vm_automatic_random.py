"""Unit tests: VM Automatic per-job clip randomization.

Covered per spec §16/§17/§18/§42 / §44: new seed, deterministic replay,
eligible pool preserved, no consecutive duplicates, manual/folder
constraints preserved (the master ProjectOrderStore is never mutated).
"""
from __future__ import annotations

import random
from pathlib import Path

from app.vm_automatic.randomization import new_seed, randomize_pool, resolve_eligible_pool
from app.video_merger.project_order import ProjectOrderStore


def _pool(tmp_path: Path, count: int = 6) -> list[Path]:
    folder = tmp_path / "pool"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(1, count + 1):
        path = folder / f"{index:02d}.mp4"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def test_new_seed_is_fresh_32bit():
    seeds = {new_seed() for _ in range(50)}
    assert len(seeds) > 40  # no repeats in practice
    assert all(0 <= seed < 2**32 for seed in seeds)


def test_same_seed_gives_same_order(tmp_path):
    paths = _pool(tmp_path, 8)
    first = randomize_pool(paths, seed=424242)
    second = randomize_pool(paths, seed=424242)
    assert first.names == second.names
    assert first.seed == second.seed == 424242


def test_different_seeds_give_different_orders(tmp_path):
    paths = _pool(tmp_path, 8)
    orders = {randomize_pool(paths, seed=seed).names for seed in (1, 2, 3, 4)}
    assert len(orders) > 1  # with 8 clips, collisions are astronomically unlikely


def test_order_is_permutation_of_eligible_pool(tmp_path):
    paths = _pool(tmp_path, 7)
    result = randomize_pool(paths, seed=777)
    assert sorted(result.names) == sorted(p.name for p in paths)
    assert len(result.names) == len(set(result.names))  # no duplicates
    assert result.pool_size == 7


def test_no_consecutive_duplicates_in_render_order_and_loop(tmp_path):
    """A permutation of unique clips can never replay a clip adjacently –
    the same holds for the Full-Timeline Loop expansion (A B C A B C)."""
    paths = _pool(tmp_path, 5)
    for seed in (11, 22, 33):
        result = randomize_pool(paths, seed=seed)
        names = list(result.names)
        for left, right in zip(names, names[1:]):
            assert left != right
        # simulate the existing timeline loop expansion (repeat the sequence)
        looped = names + names
        for left, right in zip(looped, looped[1:]):
            assert left != right


def test_single_clip_pool_is_trivial(tmp_path):
    paths = _pool(tmp_path, 1)
    result = randomize_pool(paths, seed=1)
    assert result.names == (paths[0].name,)


def test_identity_shuffle_gets_fallback_seed(tmp_path, monkeypatch):
    """If the first Fisher-Yates pass happens to equal the source order,
    one fallback seed is used so the new job is visibly different."""
    paths = _pool(tmp_path, 4)
    real_random = random.Random

    class RiggedRng(real_random):
        def randrange(self, bound: int) -> int:  # identity permutation
            return bound - 1

    calls = []

    def fake_rng(seed):
        calls.append(seed)
        if seed == 999:
            return RiggedRng(seed)
        return real_random(seed)

    # monkeypatch the module-level random.Random used by randomize_pool
    import app.vm_automatic.randomization as module

    original = module.random
    class RandomModule:
        Random = staticmethod(fake_rng)
    module.random = RandomModule
    try:
        result = randomize_pool(paths, seed=999)
    finally:
        module.random = original
    assert result.seed == 1000  # fallback seed
    assert result.names != tuple(p.name for p in paths)


# ---------------------------------------------------------------------- #
# Eligible pool resolution + master-order preservation
# ---------------------------------------------------------------------- #
def test_resolve_eligible_pool_uses_existing_rules(tmp_path):
    folder = tmp_path / "pool"
    folder.mkdir()
    for name in ("01.mp4", "02.mp4", "03.mp4", "junk.tmp", ".hidden.mp4", "merged_x.mp4"):
        (folder / name).write_bytes(b"x")
    order_store = ProjectOrderStore(tmp_path / "order.json")
    pool = resolve_eligible_pool(folder, order_store, set())
    assert [p.name for p in pool] == ["01.mp4", "02.mp4", "03.mp4"]


def test_job_randomization_never_mutates_master_order(tmp_path):
    """The user's saved manual ordering must remain intact after a job is
    randomized (job-local execution state only)."""
    folder = tmp_path / "pool"
    folder.mkdir()
    for name in ("01.mp4", "02.mp4", "03.mp4", "04.mp4"):
        (folder / name).write_bytes(b"x")
    order_store = ProjectOrderStore(tmp_path / "order.json")
    pool = resolve_eligible_pool(folder, order_store, set())
    # simulate the user's manual order (persisted by the existing GUI)
    manual = [pool[2], pool[0], pool[3], pool[1]]
    order_store.set_active_order(folder, manual)
    order_after_manual = (tmp_path / "order.json").read_bytes()

    # the job resolves the pool and shuffles job-locally (in memory only)
    pool2 = resolve_eligible_pool(folder, order_store, set())
    assert [p.name for p in pool2] == [p.name for p in manual]  # manual order respected
    result = randomize_pool(pool2, seed=31337)
    assert len(result.names) == 4

    # the persisted master order store is byte-identical (never rewritten by the job)
    assert (tmp_path / "order.json").read_bytes() == order_after_manual
    # and the active order is still the user's manual sequence
    pool3 = resolve_eligible_pool(folder, order_store, set())
    assert [p.name for p in pool3] == [p.name for p in manual]


def test_generated_outputs_are_excluded_from_pool(tmp_path):
    folder = tmp_path / "pool"
    folder.mkdir()
    for name in ("01.mp4", "02.mp4"):
        (folder / name).write_bytes(b"x")
    (folder / "MainVideo_16x9.mp4").write_bytes(b"x")
    order_store = ProjectOrderStore(tmp_path / "order.json")
    generated = {str((folder / "MainVideo_16x9.mp4").resolve())}
    pool = resolve_eligible_pool(folder, order_store, generated)
    assert [p.name for p in pool] == ["01.mp4", "02.mp4"]
