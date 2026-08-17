"""First-party plugins, loaded first by :func:`scopiengine.plugins.registry.discover_plugins`.

They register through the exact same ``register_*`` hook functions a
third-party plugin implements — there is no privileged route for built-ins.
"""

from __future__ import annotations

__all__: list[str] = []
