"""Sensors. Two tiers, one envelope.

`aggregate` reads the buckets weatherwatch already persists and adds no
retention at all. `edge` and `lifecycle` read the opt-in edge store and answer
the questions counters arithmetically cannot.
"""
