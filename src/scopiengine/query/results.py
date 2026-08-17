"""The Elasticsearch-shaped response envelope, and the two entry points — one
per query language — that build it end to end: run the AST, fetch the page's
source documents, optionally re-sort/project/aggregate, and render.

``run_scopiql`` and ``run_dsl`` are what :mod:`scopiengine.api` and the
``scopi search`` CLI command both call, so the CLI (no server) and the REST
API produce byte-identical envelopes for the same query.

Envelope shape
--------------
::

    {
      "took": <ms, rounded>,
      "timed_out": false,
      "hits": {
        "total": {"value": <int>, "relation": "eq"},
        "max_score": <float | null>,
        "hits": [{"_index": ..., "_id": ..., "_score": ..., "_source": {...}}, ...]
      },
      "aggregations": {"<name>": {"buckets": [...], ...}},   # only when requested
      "scopi": {
        "query": {...},           # the compiled AST, rendered as JSON (ast_to_dict)
        "segments_touched": <int>,   # live segments the search scanned
        "took_ms": <float, unrounded>
      }
    }

``hits.total.value`` is always the true match count (see
:class:`scopiengine.index.searcher.SearchResult`), never the page length.

Known limitations
------------------
``sort`` (ScopiQL's ``| sort`` stage, or the DSL's ``sort``) only **reorders
the page** the relevance search already selected (bounded by ``size``/
``limit``) — it is not a full index-wide field sort executed before
truncation, the way Elasticsearch's is. For a field with more matches than
the page size, narrow with a filter (a range on ``@timestamp``, for example)
rather than relying on ``sort`` + a large ``size`` to see "the most recent N"
correctly. This is documented again, with the reasoning, in
``docs/QUERY_LANGUAGE.md``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from scopiengine.index.reader import IndexReader
from scopiengine.index.searcher import Hit, iter_matches
from scopiengine.mapping.mapping import Mapping
from scopiengine.query.ast import Bool, Exists, MatchAll, Phrase, Prefix, Query, Range, Term
from scopiengine.query.dsl import TermsAgg, compile_dsl
from scopiengine.query.scopiql import SortSpec
from scopiengine.query.scopiql import parse as parse_scopiql

if TYPE_CHECKING:
    # Deferred to a type-checking-only import: `Engine` sits above this module
    # in the dependency graph (it builds on the query layer, not the other way
    # around), and `scopiengine.index.manager` — which `Engine` itself pulls
    # in — imports `scopiengine.query.ast`, which imports this package. A
    # module-level `from scopiengine.engine import Engine` here would close
    # that into a real circular import; `from __future__ import annotations`
    # (top of file) means every annotation below is a string at runtime, so
    # this guarded import is all a type checker needs.
    from scopiengine.engine import Engine

__all__ = ["ast_to_dict", "run_dsl", "run_scopiql", "terms_aggregation"]


def ast_to_dict(node: Query) -> dict[str, Any]:
    """Render a query AST node as a JSON-friendly nested dict.

    Used for the ``scopi.query`` debug block in every search response, and by
    ``scopi search --explain``.
    """
    if isinstance(node, MatchAll):
        return {"match_all": {}}
    if isinstance(node, Term):
        return {"term": {"field": node.field, "value": node.value, "boost": node.boost}}
    if isinstance(node, Phrase):
        return {
            "phrase": {
                "field": node.field,
                "terms": list(node.terms),
                "slop": node.slop,
                "boost": node.boost,
            }
        }
    if isinstance(node, Prefix):
        return {"prefix": {"field": node.field, "value": node.value, "boost": node.boost}}
    if isinstance(node, Range):
        return {
            "range": {
                "field": node.field,
                "gte": node.gte,
                "gt": node.gt,
                "lte": node.lte,
                "lt": node.lt,
                "boost": node.boost,
            }
        }
    if isinstance(node, Exists):
        return {"exists": {"field": node.field}}
    if isinstance(node, Bool):
        return {
            "bool": {
                "must": [ast_to_dict(q) for q in node.must],
                "should": [ast_to_dict(q) for q in node.should],
                "must_not": [ast_to_dict(q) for q in node.must_not],
                "filter": [ast_to_dict(q) for q in node.filter],
            }
        }
    raise TypeError(f"unrecognised query node: {node!r}")  # pragma: no cover - exhaustive above


def _extract_field(doc: dict[str, Any], field: str) -> Any:
    """Look up a dotted field path in a (possibly nested) document.

    A field named ``foo.keyword`` looks up ``foo`` instead — the document's
    stored source never has the dynamic-mapping ``.keyword`` sub-field as an
    actual JSON key, only the mapping does. A multi-valued (list) leaf yields
    its first element, matching how a single "value" is expected for sorting
    and bucketing.
    """
    path = field[: -len(".keyword")] if field.endswith(".keyword") else field
    node: Any = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, list):
        return node[0] if node else None
    return node


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _project_source(doc: dict[str, Any], source: bool | tuple[str, ...]) -> dict[str, Any] | None:
    """Filter a document's source per a DSL ``_source``/ScopiQL ``fields`` spec.

    ``True`` (the default) keeps the whole document; ``False`` omits
    ``_source`` from the hit entirely (returns ``None``); a tuple projects
    just those field paths, rebuilding their nesting.
    """
    if source is False:
        return None
    if source is True:
        return doc
    projected: dict[str, Any] = {}
    for path in source:
        value = _extract_field(doc, path)
        if value is None:
            continue
        _set_nested(projected, path, value)
    return projected


def _apply_sort(
    hits: list[Hit], docs_by_ord: dict[int, dict[str, Any]], specs: tuple[SortSpec, ...]
) -> list[Hit]:
    """Reorder an already-fetched page of hits by ``specs`` — see the module
    docstring's "Known limitations" for why this is a page-local sort.

    Applies stably from the last spec to the first, so an earlier spec breaks
    ties left by a later one — the usual way to implement a multi-key sort
    with a single-key ``sort()``. Within one spec, documents missing that
    field sort after every document that has it, in both ascending and
    descending order.
    """
    ordered = list(hits)
    for spec in reversed(specs):

        def key(hit: Hit, spec: SortSpec = spec) -> Any:
            if spec.field == "_score":
                return hit.score
            return _extract_field(docs_by_ord.get(hit.doc_ord, {}), spec.field)

        present = [h for h in ordered if key(h) is not None]
        missing = [h for h in ordered if key(h) is None]
        present.sort(key=key, reverse=spec.descending)
        ordered = present + missing
    return ordered


def terms_aggregation(
    engine: Engine,
    index: str,
    mapping: Mapping,
    query: Query,
    agg: TermsAgg,
    *,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Run one ``terms`` aggregation: bucket every match by ``agg.field``'s value, counted.

    Streams every match (:func:`scopiengine.index.searcher.iter_matches`,
    never a ranked top-N page) and fetches stored source in bounded batches —
    memory scales with the number of *distinct* bucket keys plus one batch,
    never with the number of matches.

    Returns:
        An Elasticsearch-shaped terms aggregation result: ``buckets`` (top
        ``agg.size`` by count, ties broken by key), ``sum_other_doc_count``
        for everything past that cut, and ``doc_count_error_upper_bound``
        (always ``0`` — every match is actually counted, not sampled).
    """
    reader = IndexReader(engine.storage, index, mapping)
    counts: dict[str, int] = {}

    def flush(batch: list[int]) -> None:
        if not batch:
            return
        for stored in engine.storage.get_documents(index, batch):
            value = _extract_field(json.loads(stored.source), agg.field)
            if value is None:
                continue
            key = str(value)
            counts[key] = counts.get(key, 0) + 1

    batch: list[int] = []
    for hit in iter_matches(reader, query):
        batch.append(hit.doc_ord)
        if len(batch) >= batch_size:
            flush(batch)
            batch = []
    flush(batch)

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    buckets = ranked[: agg.size]
    other = sum(count for _key, count in ranked[agg.size :])
    return {
        "doc_count_error_upper_bound": 0,
        "sum_other_doc_count": other,
        "buckets": [{"key": key, "doc_count": count} for key, count in buckets],
    }


def _render(
    engine: Engine,
    index: str,
    query: Query,
    *,
    size: int,
    from_: int,
    sort: tuple[SortSpec, ...],
    source: bool | tuple[str, ...],
    aggs: dict[str, TermsAgg],
    mapping: Mapping,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = engine.search(index, query, size=size, from_=from_)

    ords = [hit.doc_ord for hit in result.hits]
    stored = engine.storage.get_documents(index, ords) if ords else []
    by_ord = {doc.doc_ord: doc for doc in stored}
    docs_by_ord = {doc_ord: json.loads(doc.source) for doc_ord, doc in by_ord.items()}

    hits = _apply_sort(result.hits, docs_by_ord, sort) if sort else list(result.hits)

    hits_json: list[dict[str, Any]] = []
    for hit in hits:
        stored_doc = by_ord.get(hit.doc_ord)
        if stored_doc is None:
            continue
        entry: dict[str, Any] = {"_index": index, "_id": stored_doc.doc_id, "_score": hit.score}
        projected = _project_source(docs_by_ord[hit.doc_ord], source)
        if projected is not None:
            entry["_source"] = projected
        hits_json.append(entry)

    aggregations = (
        {
            name: terms_aggregation(engine, index, mapping, query, spec)
            for name, spec in aggs.items()
        }
        if aggs
        else None
    )

    took_ms = (time.perf_counter() - started) * 1000
    segments_touched = int(engine.stats(index).get("segment_count", 0))
    max_score = max((hit.score for hit in hits), default=None)

    response: dict[str, Any] = {
        "took": round(took_ms),
        "timed_out": False,
        "hits": {
            "total": {"value": result.total, "relation": "eq"},
            "max_score": max_score,
            "hits": hits_json,
        },
        "scopi": {
            "query": ast_to_dict(query),
            "segments_touched": segments_touched,
            "took_ms": round(took_ms, 3),
        },
    }
    if aggregations is not None:
        response["aggregations"] = aggregations
    return response


def run_scopiql(
    engine: Engine,
    index: str,
    text: str,
    *,
    size: int = 10,
    from_: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Parse and execute a ScopiQL query against ``index``, rendering the envelope.

    ``size``/``from_`` are the request's defaults; a ``| limit N`` pipeline
    stage overrides ``size`` (ScopiQL 1.0 has no offset stage, so ``from_``
    always comes from the caller).

    Raises:
        IndexNotFoundError: No index named ``index`` exists.
        InvalidQueryError: ``text`` is not valid ScopiQL.
        UnsupportedFeatureError: ``text`` uses a feature this build does not implement.
    """
    mapping = engine.get_mapping(index)
    parsed = parse_scopiql(text, mapping, engine.analyzers, now=now)
    effective_size = parsed.limit if parsed.limit is not None else size
    source: bool | tuple[str, ...] = parsed.fields if parsed.fields is not None else True
    aggs = {"stats": TermsAgg(field=parsed.stats.by)} if parsed.stats is not None else {}

    envelope = _render(
        engine,
        index,
        parsed.query,
        size=effective_size,
        from_=from_,
        sort=parsed.sort,
        source=source,
        aggs=aggs,
        mapping=mapping,
    )
    envelope["scopi"]["scopiql"] = {
        "sort": [{"field": s.field, "descending": s.descending} for s in parsed.sort],
        "limit": parsed.limit,
        "fields": list(parsed.fields) if parsed.fields is not None else None,
        "stats": {"by": parsed.stats.by} if parsed.stats is not None else None,
    }
    return envelope


def run_dsl(engine: Engine, index: str, body: dict[str, Any]) -> dict[str, Any]:
    """Compile and execute a JSON DSL request body against ``index``, rendering the envelope.

    Raises:
        IndexNotFoundError: No index named ``index`` exists.
        InvalidQueryError: ``body`` is malformed.
        UnsupportedFeatureError: ``body`` names a clause or option this build
            does not implement — the error names the exact key.
    """
    mapping = engine.get_mapping(index)
    parsed = compile_dsl(mapping, engine.analyzers, body)
    return _render(
        engine,
        index,
        parsed.query,
        size=parsed.size,
        from_=parsed.from_,
        sort=parsed.sort,
        source=parsed.source,
        aggs=parsed.aggs,
        mapping=mapping,
    )
