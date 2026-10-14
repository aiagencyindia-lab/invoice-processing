"""v2 Transaction Trust layer.

Engines read the Evidence Graph and write cited findings; they never write
each other's tables, reach the model only through `decide()`, and external
systems only through `providers`. See
docs/superpowers/specs/2026-10-14-v2-trust-layer-design.md §3.1.
"""
