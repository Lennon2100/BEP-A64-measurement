"""Shared readers for the routed IPv6 prefix frame."""

import csv
import ipaddress
import os
from collections import Counter


def project_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_immediate_parent_map(prefixes):
    """Return child -> nearest covering prefix for a canonical prefix set."""
    prefix_set = set(prefixes)
    parents = {}
    for prefix in prefix_set:
        ancestor = prefix
        while ancestor.prefixlen > 0:
            ancestor = ancestor.supernet()
            if ancestor in prefix_set:
                parents[prefix] = ancestor
                break
    return parents


def load_exclusions(path):
    exclusions = []
    if not path:
        return exclusions
    with open(path, encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, 1):
            text = line.split("#", 1)[0].strip()
            if not text:
                continue
            try:
                prefix = ipaddress.ip_network(text, strict=True)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid exclusion {text!r}"
                ) from exc
            if prefix.version != 6:
                raise ValueError(f"{path}:{line_number}: non-IPv6 exclusion {prefix}")
            exclusions.append(prefix)
    return exclusions


def _data_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        for line_number, fields in enumerate(csv.reader(fh), 1):
            if not fields or not any(field.strip() for field in fields):
                continue
            first = fields[0].strip()
            if first.startswith("#") or first.lower() == "prefix":
                continue
            if len(fields) != 3:
                raise ValueError(
                    f"{path}:{line_number}: expected 3 CSV fields, got {len(fields)}"
                )
            yield line_number, tuple(field.strip() for field in fields)


def _prefix_value(path, line_number, text):
    try:
        prefix = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ValueError(f"{path}:{line_number}: invalid prefix {text!r}") from exc
    if prefix.version != 6:
        raise ValueError(f"{path}:{line_number}: non-IPv6 prefix {prefix}")
    return prefix


def _origin_values(path, line_number, count_text, origins_text):
    try:
        declared_count = int(count_text)
    except ValueError as exc:
        raise ValueError(
            f"{path}:{line_number}: invalid origin count {count_text!r}"
        ) from exc
    origins = tuple(dict.fromkeys(origin for origin in origins_text.split("|") if origin))
    if declared_count != len(origins):
        raise ValueError(
            f"{path}:{line_number}: origin count {declared_count} "
            f"does not match {len(origins)} origin values"
        )
    return declared_count, origins


def _exclusion_reason(prefix, exclusions):
    if prefix.prefixlen == 0:
        return "default_route"
    if prefix.prefixlen > 64:
        return "longer_than_64"
    containing = next(
        (exclusion for exclusion in exclusions if prefix.subnet_of(exclusion)), None
    )
    if containing is not None:
        return f"configured:{containing}"
    partial = next(
        (exclusion for exclusion in exclusions if exclusion.subnet_of(prefix)), None
    )
    if partial is not None:
        raise ValueError(
            f"input prefix {prefix} contains configured exclusion {partial}; "
            "partial-prefix subtraction is unsupported"
        )
    return None


def load_rows(path, exclusions):
    rows = {}
    excluded = Counter()
    for line_number, fields in _data_rows(path):
        prefix_text, count_text, origins_text = fields
        prefix = _prefix_value(path, line_number, prefix_text)
        declared_count, origins = _origin_values(
            path, line_number, count_text, origins_text
        )
        if prefix in rows:
            raise ValueError(f"{path}:{line_number}: duplicate prefix {prefix}")
        reason = _exclusion_reason(prefix, exclusions)
        if reason is not None:
            excluded[reason] += 1
            continue
        rows[prefix] = {"origin_count": declared_count, "origins": origins}
    if not rows:
        raise ValueError("input contains no usable IPv6 prefixes at /64 or shorter")
    return rows, excluded


def root_for(prefix, parents, cache):
    path = []
    current = prefix
    while current in parents:
        path.append(current)
        current = parents[current]
    for descendant in path:
        cache[descendant] = current
    cache[prefix] = current
    return current
