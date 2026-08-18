"""The REST API: a FastAPI app exposing the same :class:`~scopiengine.engine.Engine`
the CLI drives, with Elasticsearch-shaped requests and responses.

One :class:`~scopiengine.engine.Engine` is opened once, at process startup (a
``lifespan`` handler, not a per-request ``open_storage`` call), and shared by
every request thereafter — see :func:`create_app`.

Every search and indexing endpoint below is a **synchronous** ``def``, on
purpose: Starlette runs a sync path function in its worker threadpool rather
than on the event loop, which is what keeps the synchronous storage layer
(SQLite/MS SQL, both blocking I/O) from ever stalling the loop that every
other connection's request also depends on.

Every :class:`~scopiengine.errors.ScopiError` raised anywhere below — by the
engine, the query compiler, or a handler itself — is caught by one exception
handler and rendered through :meth:`~scopiengine.errors.ScopiError.to_dict`,
so the response body and status code are the same ES-shaped envelope no
matter which layer raised.

Deliberate deviations from real Elasticsearch (see ``docs/QUERY_LANGUAGE.md``
and ``docs/USAGE.md`` for the full list): ``_cat/indices`` returns JSON, not a
plain-text table; ``GET _doc/{id}`` for a missing document is a genuine
``404`` rather than a ``200`` with ``"found": false``; and ``_analyze``'s
token objects carry no ``start_offset``/``end_offset`` (the analyzer layer
does not track them).

A minimal single-page UI (index picker, ScopiQL search box, results table)
ships alongside the API and is mounted at ``/_ui`` — plain static files
served by Starlette's ``StaticFiles``, calling the same JSON endpoints below
from the browser. There is no build step or bundler: the whole thing is
hand-written HTML/CSS/JS under ``api/static/``, in keeping with this
project's minimal-dependencies stance.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from scopiengine import TAGLINE, __version__
from scopiengine.engine import Engine
from scopiengine.errors import IngestError, InvalidQueryError, ScopiError, UnsupportedFeatureError
from scopiengine.ingest.jobs import IngestJobManager
from scopiengine.ingest.pipeline import IngestOptions
from scopiengine.plugins.registry import PluginLoadResult
from scopiengine.query.results import run_dsl, run_scopiql
from scopiengine.settings import Settings

__all__ = ["create_app", "execute_bulk", "parse_bulk_lines"]

#: The bundled single-page UI (index picker, ScopiQL search box, results
#: table). Mounted at ``/_ui`` — a path prefix, not an ES verb, so it can
#: never collide with an index name the way a top-level route could.
_STATIC_DIR = Path(__file__).parent / "static"

#: Bulk actions this build implements. ``update`` is a recognised but
#: unsupported action — named explicitly in the error rather than falling
#: into the generic "unknown action" branch, since it is a real ES action a
#: caller might reasonably expect.
_SUPPORTED_BULK_ACTIONS = frozenset({"index", "create", "delete"})


def _extract_mapping_and_settings(
    body: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Accept both ``{"mappings": {...}, "settings": {...}}`` and a bare mapping body."""
    if not body:
        return None, None
    mapping = body.get("mappings")
    if mapping is None and "properties" in body:
        mapping = body
    return mapping, body.get("settings")


def _not_found(kind: str, index: str, doc_id: str) -> JSONResponse:
    reason = f"document {doc_id!r} not found in index {index!r}"
    return JSONResponse(
        status_code=404,
        content={"error": {"type": f"{kind}_not_found", "reason": reason}, "status": 404},
    )


def _job_not_found(job_id: str) -> JSONResponse:
    reason = f"no ingest job found for id {job_id!r}"
    return JSONResponse(
        status_code=404,
        content={"error": {"type": "ingest_job_not_found", "reason": reason}, "status": 404},
    )


# -- bulk ----------------------------------------------------------------


def parse_bulk_lines(raw: bytes) -> list[tuple[str, dict[str, Any], dict[str, Any] | None]]:
    """Parse ES NDJSON bulk syntax into ``(action, meta, source)`` triples.

    Raises:
        InvalidQueryError: A line is not valid JSON, an action object does not
            have exactly one key, or an ``index``/``create`` action is
            missing its source line.
        UnsupportedFeatureError: The action name is not ``index``/``create``/``delete``.
    """
    text = raw.decode("utf-8")
    lines = [line for line in text.split("\n") if line.strip()]
    parsed: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    i = 0
    while i < len(lines):
        try:
            action_line = json.loads(lines[i])
        except json.JSONDecodeError as exc:
            raise InvalidQueryError(f"bulk line {i + 1} is not valid JSON: {exc}") from exc
        if not isinstance(action_line, dict) or len(action_line) != 1:
            raise InvalidQueryError(
                f"bulk line {i + 1} must be an action object with exactly one key "
                "('index', 'create', or 'delete')"
            )
        ((action, meta),) = action_line.items()
        if action not in _SUPPORTED_BULK_ACTIONS:
            raise UnsupportedFeatureError(f"unsupported bulk action: {action!r}")
        if not isinstance(meta, dict):
            raise InvalidQueryError(f"bulk line {i + 1}: action metadata must be a JSON object")
        i += 1
        source: dict[str, Any] | None = None
        if action in ("index", "create"):
            if i >= len(lines):
                raise InvalidQueryError(
                    f"bulk action on line {i} ('{action}') is missing its source line"
                )
            try:
                source = json.loads(lines[i])
            except json.JSONDecodeError as exc:
                raise InvalidQueryError(f"bulk line {i + 1} is not valid JSON: {exc}") from exc
            i += 1
        parsed.append((action, meta, source))
    return parsed


def execute_bulk(engine: Engine, default_index: str | None, raw: bytes) -> dict[str, Any]:
    started = time.perf_counter()
    actions = parse_bulk_lines(raw)
    items: list[dict[str, Any]] = []
    errors = False
    for action, meta, source in actions:
        index_name = meta.get("_index") or default_index
        doc_id = meta.get("_id")
        item: dict[str, Any]
        try:
            if index_name is None:
                raise InvalidQueryError(
                    "bulk action is missing '_index' and no default index was given"
                )
            if action in ("index", "create"):
                ids = engine.index_documents(
                    index_name, [source or {}], ids=[doc_id] if doc_id else None
                )
                item = {
                    "_index": index_name,
                    "_id": ids[0],
                    "status": 201,
                    "result": "created",
                }
            else:  # delete
                if not doc_id:
                    raise InvalidQueryError("bulk 'delete' action requires '_id'")
                found = engine.delete_document(index_name, doc_id)
                if found:
                    item = {
                        "_index": index_name,
                        "_id": doc_id,
                        "status": 200,
                        "result": "deleted",
                    }
                else:
                    errors = True
                    item = {
                        "_index": index_name,
                        "_id": doc_id,
                        "status": 404,
                        "result": "not_found",
                    }
        except ScopiError as exc:
            errors = True
            item = {
                "_index": index_name,
                "_id": doc_id,
                "status": exc.status,
                "error": exc.to_dict()["error"],
            }
        items.append({action: item})
    took_ms = (time.perf_counter() - started) * 1000
    return {"took": round(took_ms), "errors": errors, "items": items}


# -- app factory ---------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the REST API, wired to one :class:`~scopiengine.engine.Engine`.

    The engine opens on ASGI startup and closes on shutdown (a ``lifespan``
    handler) — never per request. Every route below reaches it through
    ``app.state.engine``.
    """
    resolved_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        opened = Engine.open(resolved_settings)
        app.state.engine = opened
        app.state.ingest_jobs = IngestJobManager(opened)
        try:
            yield
        finally:
            opened.close()

    app = FastAPI(title="ScopiEngine", version=__version__, lifespan=lifespan)
    app.state.settings = resolved_settings

    def engine() -> Engine:
        return app.state.engine  # type: ignore[no-any-return]

    @app.exception_handler(ScopiError)
    def _scopi_error_handler(_request: Any, exc: ScopiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    # -- root / health / cluster --------------------------------------------

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "name": "scopiengine",
            "cluster_name": "scopiengine",
            "tagline": TAGLINE,
            "version": {"number": __version__},
        }

    @app.get("/_health")
    def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/_cluster/health")
    def cluster_health() -> dict[str, Any]:
        indices = engine().list_indices()
        return {
            "cluster_name": "scopiengine",
            "status": "green",
            "timed_out": False,
            "number_of_indices": len(indices),
        }

    @app.get("/_cat/indices")
    def cat_indices() -> list[dict[str, Any]]:
        rows = []
        for info in engine().list_indices():
            stats = engine().stats(info.name)
            rows.append(
                {
                    "index": info.name,
                    "docs_count": info.doc_count,
                    "segment_count": stats.get("segment_count", 0),
                    "created_at": info.created_at,
                }
            )
        return rows

    @app.get("/_plugins")
    def plugins() -> dict[str, Any]:
        registry = engine().plugins
        failed: list[PluginLoadResult] = list(registry.failed)
        return {
            "loaded": list(registry.loaded),
            "failed": [{"name": r.name, "error": r.error} for r in failed],
        }

    # -- web UI ------------------------------------------------------------
    #
    # Registered here, ahead of the `/{index}` routes below: those match any
    # single path segment, so `/_ui` would otherwise be swallowed as a
    # (nonexistent) index name — Starlette matches routes in registration
    # order and takes the first one that fits. This makes `_ui` a reserved
    # name for GET the same way `_health`/`_bulk`/etc. already were: an index
    # literally named `_ui` can still be created, HEAD-checked and deleted,
    # but every `GET /_ui/...` is claimed by the static mount below rather
    # than reaching that index's own routes.

    @app.get("/_ui", include_in_schema=False)
    def ui_redirect() -> RedirectResponse:
        return RedirectResponse(url="/_ui/")

    app.mount("/_ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

    # -- indices --------------------------------------------------------------

    @app.put("/{index}")
    def create_index(index: str, body: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
        mapping, index_settings = _extract_mapping_and_settings(body)
        info = engine().create_index(index, mapping, index_settings)
        return JSONResponse(status_code=201, content={"acknowledged": True, "index": info.name})

    @app.get("/{index}")
    def get_index(index: str) -> dict[str, Any]:
        info = engine().get_index(index)
        return {
            info.name: {
                "mappings": info.mapping,
                "settings": info.settings,
                "doc_count": info.doc_count,
                "created_at": info.created_at,
            }
        }

    @app.head("/{index}")
    def index_exists(index: str) -> Response:
        return Response(status_code=200 if engine().index_exists(index) else 404)

    @app.delete("/{index}")
    def delete_index(index: str) -> dict[str, Any]:
        engine().delete_index(index)
        return {"acknowledged": True}

    @app.post("/{index}/_refresh")
    def refresh_index(index: str) -> dict[str, Any]:
        segment_id = engine().refresh(index)
        return {"_index": index, "flushed_segment": segment_id}

    @app.post("/{index}/_forcemerge")
    def forcemerge_index(index: str) -> dict[str, Any]:
        segment_id = engine().force_merge(index)
        return {"_index": index, "merged_segment": segment_id}

    @app.get("/{index}/_stats")
    def index_stats(index: str) -> dict[str, Any]:
        return {"_index": index, **engine().stats(index)}

    # -- documents -----------------------------------------------------------

    @app.post("/{index}/_doc")
    def index_doc_auto_id(index: str, body: dict[str, Any] = Body(...)) -> JSONResponse:
        ids = engine().index_documents(index, [body])
        return JSONResponse(
            status_code=201, content={"_index": index, "_id": ids[0], "result": "created"}
        )

    @app.put("/{index}/_doc/{doc_id}")
    def index_doc(index: str, doc_id: str, body: dict[str, Any] = Body(...)) -> JSONResponse:
        existed = engine().get_document(index, doc_id) is not None
        ids = engine().index_documents(index, [body], ids=[doc_id])
        status = 200 if existed else 201
        result = "updated" if existed else "created"
        return JSONResponse(
            status_code=status, content={"_index": index, "_id": ids[0], "result": result}
        )

    @app.get("/{index}/_doc/{doc_id}")
    def get_doc(index: str, doc_id: str) -> Response:
        doc = engine().get_document(index, doc_id)
        if doc is None:
            return _not_found("document", index, doc_id)
        return JSONResponse(content={"_index": index, "_id": doc_id, "found": True, "_source": doc})

    @app.delete("/{index}/_doc/{doc_id}")
    def delete_doc(index: str, doc_id: str) -> Response:
        found = engine().delete_document(index, doc_id)
        if not found:
            return _not_found("document", index, doc_id)
        return JSONResponse(content={"_index": index, "_id": doc_id, "result": "deleted"})

    # -- bulk ------------------------------------------------------------------

    @app.post("/_bulk")
    def bulk_no_index(
        payload: bytes = Body(..., media_type="application/x-ndjson"),
    ) -> dict[str, Any]:
        return execute_bulk(engine(), None, payload)

    @app.post("/{index}/_bulk")
    def bulk_with_index(
        index: str, payload: bytes = Body(..., media_type="application/x-ndjson")
    ) -> dict[str, Any]:
        return execute_bulk(engine(), index, payload)

    # -- search and analysis --------------------------------------------------

    @app.api_route("/{index}/_search", methods=["GET", "POST"])
    def search(
        index: str,
        q: str | None = None,
        size: int = 10,
        from_: int = Query(default=0, alias="from"),
        body: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        if q is not None:
            return run_scopiql(engine(), index, q, size=size, from_=from_)
        payload = dict(body or {})
        payload.setdefault("size", size)
        payload.setdefault("from", from_)
        return run_dsl(engine(), index, payload)

    @app.post("/{index}/_scopiql")
    def scopiql_search(index: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        text = body.get("query", "")
        if not isinstance(text, str):
            raise InvalidQueryError("'query' must be a string")
        size = body.get("size", 10)
        from_ = body.get("from", 0)
        if isinstance(size, bool) or not isinstance(size, int):
            raise InvalidQueryError("'size' must be an integer")
        if isinstance(from_, bool) or not isinstance(from_, int):
            raise InvalidQueryError("'from' must be an integer")
        return run_scopiql(engine(), index, text, size=size, from_=from_)

    @app.post("/{index}/_analyze")
    def analyze(index: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        del index  # analyzers are engine-global, not per-index — see the module docstring
        text = body.get("text")
        if not isinstance(text, str):
            raise InvalidQueryError("'text' must be a string")
        analyzer_name = body.get("analyzer", "standard")
        if not isinstance(analyzer_name, str):
            raise InvalidQueryError("'analyzer' must be a string")
        tokens = engine().analyze(text, analyzer=analyzer_name)
        return {"tokens": [{"token": term, "position": position} for term, position in tokens]}

    # -- ingestion -------------------------------------------------------------

    def ingest_jobs() -> IngestJobManager:
        return app.state.ingest_jobs  # type: ignore[no-any-return]

    @app.post("/_ingest/file")
    def start_ingest(body: dict[str, Any] = Body(...)) -> JSONResponse:
        path = body.get("path")
        index = body.get("index")
        if not isinstance(path, str) or not path:
            raise IngestError("'path' is required and must be a string")
        if not isinstance(index, str) or not index:
            raise IngestError("'index' is required and must be a string")

        from_mode = body.get("from")
        if from_mode is None:
            from_mode = "checkpoint" if body.get("resume") else "start"
        if from_mode not in ("start", "end", "checkpoint"):
            raise IngestError("'from' must be one of: start, end, checkpoint")
        id_mode = body.get("id_mode", "offset")
        if id_mode not in ("offset", "content", "uuid"):
            raise IngestError("'id_mode' must be one of: offset, content, uuid")

        options = IngestOptions(
            index=index,
            processors=tuple(body.get("processors") or ()),
            multiline_start=body.get("multiline_start"),
            follow=bool(body.get("follow", False)),
            from_mode=from_mode,  # type: ignore[arg-type]
            id_mode=id_mode,  # type: ignore[arg-type]
            batch_size=body.get("batch_size"),
            batch_bytes=body.get("batch_bytes"),
            flush_interval=body.get("flush_interval"),
            queue_size=body.get("queue_size"),
            regex_pattern=body.get("regex_pattern"),
        )
        job = ingest_jobs().start(path=path, index=index, options=options)
        return JSONResponse(status_code=202, content=job.to_dict())

    @app.get("/_ingest/jobs")
    def list_ingest_jobs() -> list[dict[str, Any]]:
        return [job.to_dict() for job in ingest_jobs().list()]

    @app.get("/_ingest/jobs/{job_id}")
    def get_ingest_job(job_id: str) -> Response:
        job = ingest_jobs().get(job_id)
        if job is None:
            return _job_not_found(job_id)
        return JSONResponse(content=job.to_dict())

    @app.delete("/_ingest/jobs/{job_id}")
    def stop_ingest_job(job_id: str) -> Response:
        job = ingest_jobs().stop(job_id)
        if job is None:
            return _job_not_found(job_id)
        return JSONResponse(content=job.to_dict())

    return app
