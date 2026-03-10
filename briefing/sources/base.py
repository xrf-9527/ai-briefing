"""Protocol definition for source adapters.

Enables static type checking without requiring changes to existing adapters.
Each adapter module must expose a ``fetch(config) -> List[Dict[str, Any]]`` function.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class SourceAdapter(Protocol):
    """Structural protocol for source adapter modules."""

    def fetch(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch items from the configured source.

        Returns a list of standardized item dicts with keys:
            id, text, url, author, timestamp, metadata
        """
        ...
