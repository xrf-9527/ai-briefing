"""Briefing package.

Avoid importing heavy submodules at package import time to prevent side-effects
(like logger configuration and filesystem writes) during test collection.
"""

__all__: list[str] = []
