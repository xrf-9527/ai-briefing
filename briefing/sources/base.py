"""Type definition for source adapters.

Each adapter module must expose a ``fetch(config) -> List[Dict[str, Any]]`` callable.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List

# Type alias for adapter fetch functions (module-level, no self parameter)
FetchFn = Callable[[Dict[str, Any]], List[Dict[str, Any]]]
