"""Text analysis: tokenizers, filters and the analyzers built from them.

No stemming ships here — that arrives as the reference plugin in 1.1, and is the
whole reason :class:`~scopiengine.analysis.registry.AnalyzerRegistry` is a
registry rather than a fixed enum.
"""

from __future__ import annotations

from scopiengine.analysis.analyzer import Analyzer
from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.analysis.tokens import Token

__all__ = ["Analyzer", "AnalyzerRegistry", "Token"]
