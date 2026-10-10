"""Load-cell lane tracker: noisy weights in, whole-unit takes and put-backs out."""
import random

from shelf_station.station import LaneTracker


def feed(tracker, grams, n=10, noise=1.0, rng=random.Random(4)):
    out = 0
    for _ in range(n):
        out += tracker.add(grams + rng.uniform(-noise, noise))
    return out


def test_settles_then_counts_takes_and_returns():
    t = LaneTracker(unit_g=52)
    assert feed(t, 520) == 0          # baseline: 10 razors
    assert feed(t, 468) == 1          # one taken
    assert feed(t, 364) == 2          # two more taken
    assert feed(t, 416) == -1         # one put back


def test_ignores_jitter_and_a_hand_resting_briefly():
    t = LaneTracker(unit_g=160)
    feed(t, 1600)
    assert feed(t, 1610, noise=4) == 0              # small drift
    spikes = sum(t.add(g) for g in [1600, 1900, 1400, 1750, 1600])  # hand pressing on the shelf
    assert spikes == 0
    assert feed(t, 1600) == 0
