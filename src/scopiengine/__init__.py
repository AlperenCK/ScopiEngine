"""ScopiEngine — search and analytics engine.

Search / Analyze / Discover.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "Alperen CK"
__license__ = "MIT"

#: Short product tagline, reused by the CLI and the REST root endpoint.
TAGLINE = "Search / Analyze / Discover"

#: Default TCP port for ``scopi serve``. Deliberately not 9200, so ScopiEngine
#: can run alongside an existing search cluster without a port clash.
DEFAULT_PORT = 9500

__all__ = ["DEFAULT_PORT", "TAGLINE", "__author__", "__license__", "__version__"]
