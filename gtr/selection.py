"""Compact selections over canonical source ordinals for the replay guard."""

from collections.abc import Iterable

Intervals = tuple[tuple[int, int], ...]


def included_intervals(records: Iterable[dict]) -> Intervals | None:
    ranges: list[tuple[int, int]] = []
    previous = 0
    for record in records:
        if not record.get("included"):
            continue
        ordinal = record.get("source_ordinal")
        if type(ordinal) is not int or ordinal <= previous:
            return None
        if ranges and ordinal == previous + 1:
            first, _ = ranges[-1]
            ranges[-1] = (first, ordinal)
        else:
            ranges.append((ordinal, ordinal))
        previous = ordinal
    return tuple(ranges)


def intersection(left: Intervals, right: Intervals) -> Intervals:
    shared = []
    a, b = iter(left), iter(right)
    first, second = next(a, None), next(b, None)
    while first and second:
        start, stop = max(first[0], second[0]), min(first[1], second[1])
        if start <= stop:
            shared.append((start, stop))
        if first[1] <= second[1]:
            first = next(a, None)
        else:
            second = next(b, None)
    return tuple(shared)


def merge(ranges) -> Intervals:
    merged = []
    for start, stop in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            first, last = merged[-1]
            merged[-1] = (first, max(last, stop))
        else:
            merged.append((start, stop))
    return tuple(merged)


def count(ranges: Intervals) -> int:
    return sum(last - first + 1 for first, last in ranges)


def difference(left: Intervals, right: Intervals) -> Intervals:
    remaining = []
    cuts = iter(right)
    cut = next(cuts, None)
    for first, last in left:
        while cut and cut[0] <= last:
            if cut[0] > first:
                remaining.append((first, cut[0] - 1))
            first = max(first, cut[1] + 1)
            if cut[1] > last:
                break
            cut = next(cuts, None)
        if first <= last:
            remaining.append((first, last))
    return tuple(remaining)


def decode_intervals(ranges) -> Intervals:
    if not isinstance(ranges, (list, tuple)):
        raise TypeError
    result = []
    last = 0
    for first, stop in ranges:
        if type(first) is not int or type(stop) is not int or not last < first <= stop <= 2**31 - 1:
            raise ValueError
        result.append((first, stop))
        last = stop
    return merge(result)
