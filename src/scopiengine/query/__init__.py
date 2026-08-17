"""The query AST, and the two parsers (ScopiQL, JSON DSL) that build it from user input.

Deliberately does *not* also re-export :mod:`scopiengine.query.results`: that
module builds on the index layer (:mod:`scopiengine.index.searcher` and
friends), and the index layer itself imports :mod:`scopiengine.query.ast` —
so eagerly importing ``results`` here, at package-init time, would close that
into a real circular import the moment anything imports so much as
``scopiengine.query.ast``. Import ``scopiengine.query.results`` directly
where it is actually needed (the REST API and the CLI's ``search`` command).
"""

from __future__ import annotations

from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Query, Range, Term
from scopiengine.query.dsl import DslQuery, TermsAgg, compile_dsl
from scopiengine.query.scopiql import ParsedQuery, SortSpec, StatsSpec
from scopiengine.query.scopiql import parse as parse_scopiql

__all__ = [
    "Bool",
    "DslQuery",
    "Exists",
    "MatchAll",
    "ParsedQuery",
    "Phrase",
    "Prefix",
    "Query",
    "Range",
    "SortSpec",
    "StatsSpec",
    "Term",
    "TermsAgg",
    "compile_dsl",
    "parse_scopiql",
]
