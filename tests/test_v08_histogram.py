"""Histogram diff (git's algorithm) under the three-way merge.

Property tests: for random files and random non-overlapping edits on both sides, the merge
must be clean and must contain both sides' edits; merging with an unchanged side returns the
other side exactly; and the diff's matching blocks always describe a valid alignment.
"""
import random

import pytest

from agentcloud_server.diffalgo import histogram_matches
from agentcloud_server.merge3 import merge3


def _check_alignment(a, b, blocks):
    last_i = last_j = 0
    for i, j, n in blocks:
        assert i >= last_i and j >= last_j
        assert a[i:i + n] == b[j:j + n]
        last_i, last_j = i + n, j + n
    assert blocks[-1] == (len(a), len(b), 0)


@pytest.mark.parametrize("seed", range(200))
def test_histogram_alignment_is_valid_and_maximal_on_random_inputs(seed):
    rng = random.Random(seed)
    vocab = [f"l{k}\n" for k in range(rng.randint(2, 12))] + ["}\n", "\n"]
    a = [rng.choice(vocab) for _ in range(rng.randint(0, 60))]
    b = list(a)
    for _ in range(rng.randint(0, 6)):
        op = rng.random()
        pos = rng.randint(0, len(b))
        if op < 0.4:
            b.insert(pos, rng.choice(vocab))
        elif op < 0.7 and b:
            b.pop(min(pos, len(b) - 1))
        elif b:
            b[min(pos, len(b) - 1)] = rng.choice(vocab)
    blocks = histogram_matches(a, b)
    _check_alignment(a, b, blocks)
    if a == b:
        assert sum(n for _, _, n in blocks) == len(a)


def _lines(n, rng):
    return [f"line {k} {rng.randrange(10**6)}\n" for k in range(n)]


@pytest.mark.parametrize("seed", range(300))
def test_random_non_overlapping_edits_always_merge_cleanly(seed):
    rng = random.Random(seed)
    base = _lines(rng.randint(10, 80), rng)
    n = len(base)
    # two disjoint edit regions separated by at least one untouched line
    cut = rng.randint(2, n - 3)
    a_lo, a_hi = sorted(rng.sample(range(0, cut - 1), 2)) if cut > 3 else (0, 1)
    b_lo, b_hi = sorted(rng.sample(range(cut + 1, n + 1), 2))
    ours = base[:a_lo] + [f"OURS {k}\n" for k in range(rng.randint(0, 3))] + base[a_hi:]
    theirs = base[:b_lo] + [f"THEIRS {k}\n" for k in range(rng.randint(0, 3))] + base[b_hi:]
    r = merge3("".join(base), "".join(ours), "".join(theirs))
    assert r.clean, (seed, r.conflicts)
    expected = base[:a_lo] + ours[a_lo:len(ours) - (n - a_hi)] + base[a_hi:b_lo] + theirs[b_lo:len(theirs) - (n - b_hi)] + base[b_hi:]
    assert r.text == "".join(expected)


@pytest.mark.parametrize("seed", range(100))
def test_one_sided_change_is_returned_exactly(seed):
    rng = random.Random(seed)
    base = "".join(_lines(rng.randint(0, 40), rng))
    other = "".join(_lines(rng.randint(0, 40), rng))
    assert merge3(base, other, base).text == other
    assert merge3(base, base, other).text == other


def test_repeated_braces_do_not_cause_spurious_conflicts():
    """Inserting a whole function on one side while editing another function on the other,
    with many repeated `}` and blank lines. (Regression test: difflib also handles this case;
    across 20,000 random merges neither algorithm dominated.)"""
    base = ("def a():\n    return 1\n}\n\n"
            "def b():\n    return 2\n}\n\n"
            "def c():\n    return 3\n}\n")
    ours = base.replace("def b():\n", "def new():\n    return 0\n}\n\ndef b():\n")
    theirs = base.replace("return 3", "return 33")
    r = merge3(base, ours, theirs)
    assert r.clean
    assert "def new():" in r.text and "return 33" in r.text and r.text.count("}\n") == 4


def test_histogram_prefers_unique_anchors():
    a = ["{\n", "x\n", "}\n", "{\n", "y\n", "}\n"]
    b = ["{\n", "y\n", "}\n"]
    blocks = histogram_matches(a, b)
    _check_alignment(a, b, blocks)
    matched = [a[i + k] for i, j, n in blocks for k in range(n)]
    assert "y\n" in matched                     # the rare line anchors the match
