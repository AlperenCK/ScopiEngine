"""The inverted index: writer, segment merge, BM25 scoring, reader and searcher."""

from __future__ import annotations

from scopiengine.index.manager import IndexManager
from scopiengine.index.searcher import Hit, SearchResult

__all__ = ["Hit", "IndexManager", "SearchResult"]
