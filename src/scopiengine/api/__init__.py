"""The REST API: an Elasticsearch-shaped FastAPI app in front of
:class:`~scopiengine.engine.Engine`.
"""

from __future__ import annotations

from scopiengine.api.app import create_app

__all__ = ["create_app"]
