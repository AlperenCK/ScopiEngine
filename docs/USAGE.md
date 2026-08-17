# Usage

Install first — see [docs/INSTALL.md](INSTALL.md).

## The two ways to drive the engine

ScopiEngine has one core and two front doors:

- **The CLI** runs the engine in process. No server needed, one code path.
- **The REST API** (`scopi serve`) exposes the same engine over HTTP on port 9500,
  with Elasticsearch-shaped requests and responses.

Both read the same configuration, so a storage DSN set once works for both. Every
`scopi` command below is one process: a document indexed with `scopi doc put` is
refreshed (flushed to a searchable segment) before that process exits, precisely
so a following `scopi search` in a fresh process — the only way the CLI can be
used — sees it. `scopi serve` behaves the way Elasticsearch does instead: refresh
happens only on an explicit `_refresh` call (or `scopi index refresh`), since one
long-running server process can share an unflushed buffer across requests.

## Global options

Every command accepts these before the subcommand:

| Option | Meaning |
|---|---|
| `-s`, `--storage` | Storage DSN (`SCOPI_STORAGE`) |
| `-c`, `--config` | JSON config file (`SCOPI_CONFIG`) |
| `--json` | Emit JSON instead of a table |
| `-v`, `--verbose` | Log at DEBUG |
| `-q`, `--quiet` | Log errors only |
| `--strict-plugins` | Fail startup on a plugin error |

```bash
scopi version
scopi info --json
scopi --help
```

## Command groups

The remaining groups arrive with the layers they drive, and each is documented
where that layer is explained:

| Group | Purpose | Reference |
|---|---|---|
| `scopi storage` | Initialise and inspect the storage backend | [STORAGE_BACKENDS.md](STORAGE_BACKENDS.md) |
| `scopi index` | Create, list, inspect, refresh, merge and delete indices | [index lifecycle](#scopi-index) below, [ARCHITECTURE.md](ARCHITECTURE.md) |
| `scopi doc` / `scopi bulk` | Write and read documents | [documents](#scopi-doc-and-scopi-bulk) below |
| `scopi search` / `scopi analyze` | Query with ScopiQL or the JSON DSL | [search and analyze](#scopi-search-and-scopi-analyze) below, [QUERY_LANGUAGE.md](QUERY_LANGUAGE.md) |
| `scopi ingest` | Stream large log files in, with resume | [ingest](#scopi-ingest) below, [INGEST.md](INGEST.md) |
| `scopi plugin` | Inspect discovered plugins | [plugins](#scopi-plugin) below, [PLUGINS.md](PLUGINS.md) |
| `scopi serve` | Run the REST API | [the REST API](#the-rest-api) below |

## `scopi index`

```bash
scopi index create logs --mapping mapping.json --settings settings.json
scopi index list
scopi index show logs
scopi index stats logs
scopi index refresh logs
scopi index merge logs
scopi index delete logs
```

`--mapping` is a JSON file shaped `{"properties": {"field": {"type": "text", ...}}}`
(see [ARCHITECTURE.md](ARCHITECTURE.md) for field types and mapping options).
Any field a document introduces that the mapping doesn't cover is inferred
dynamically — a new string field becomes `text` plus a `.keyword` sub-field, a
new number becomes `long` or `double`, matching Elasticsearch's own default
behaviour. `create` fails with `index_already_exists` (exit code `1`) if the
name is taken.

## `scopi doc` and `scopi bulk`

```bash
scopi doc put logs '{"level": "ERROR", "message": "connection refused"}' --id 1
echo '{"level": "INFO"}' | scopi doc put logs -              # '-' reads stdin
scopi doc get logs 1
scopi doc delete logs 1

scopi bulk logs --file docs.ndjson
```

`docs.ndjson` uses Elasticsearch's bulk syntax — an action/meta line, then a
source line for `index`/`create` (nothing for `delete`):

```ndjson
{"index": {"_id": "1"}}
{"level": "ERROR", "service": "auth", "message": "connection refused"}
{"index": {"_id": "2"}}
{"level": "INFO", "service": "auth", "message": "all good"}
{"delete": {"_id": "3"}}
```

`scopi bulk` exits `1` if any action failed or a `delete` target was missing
(`--json` shows the full per-item breakdown, matching the REST `_bulk`
response's `items`/`errors` shape).

## `scopi search` and `scopi analyze`

```bash
scopi search logs 'level:ERROR AND service:auth'
scopi search logs 'status:>=500 | stats count() by service' --json
scopi search logs --dsl query.json --size 20
scopi search logs 'level:ERROR' --explain     # also prints the compiled AST and timings

scopi analyze --analyzer standard "Quick BROWN Fox"
```

`--dsl query.json` reads a JSON DSL request body from a file instead of
parsing a ScopiQL string — pass one or the other, not both. See
[QUERY_LANGUAGE.md](QUERY_LANGUAGE.md) for the full grammar, the DSL
compatibility matrix, and what `--explain`'s AST dump actually shows.

## `scopi ingest`

```bash
scopi index create logs
scopi ingest file /var/log/app.log --index logs --processor json_line
scopi ingest file /var/log/app.log --index logs --follow      # tail, survives rotation

# kill -9 (or Ctrl-C) at any point, then:
scopi ingest file /var/log/app.log --index logs --resume      # picks up exactly where it stopped

scopi ingest status                 # every checkpoint, newest first
scopi ingest reset <cp_key>          # delete one - its next run starts at byte 0
```

Streams a file in fixed-size chunks (never materialising it), checkpoints its
byte offset in the same storage transaction as the documents and segment it
just wrote, and resumes from exactly that offset after any interruption —
including a hard `kill -9`. `--follow` keeps polling for appended data and
survives log rotation (rename-then-recreate and truncate-in-place are handled
distinctly). See [INGEST.md](INGEST.md) for the full pipeline, the
single-transaction guarantee, checkpoint/rotation semantics, `--id-mode`,
tuning and honest throughput numbers.

## `scopi plugin`

```bash
scopi plugin list
scopi plugin list --json
```

Lists every plugin discovered at startup (built-ins, `importlib.metadata`
entry points, and `SCOPI_PLUGINS=mod1,mod2`) and whether it loaded — see
[PLUGINS.md](PLUGINS.md).

## The REST API

```bash
scopi serve                          # binds 127.0.0.1:9500 by default
scopi serve --host 0.0.0.0 --port 9500
```

One `Engine` opens at process startup and is shared by every request — never
reopened per request. Search and indexing endpoints are synchronous handlers
that Starlette runs in its worker threadpool, so the synchronous storage layer
never blocks the event loop other connections share.

| Method & path | Purpose |
|---|---|
| `GET /` | Name, version, tagline |
| `GET /_health` | Liveness |
| `GET /_cluster/health` | `status`, index count |
| `GET /_cat/indices` | Every index, summarised (JSON, not a text table — see below) |
| `PUT /{index}` | Create an index. Body: `{"mappings": {...}, "settings": {...}}` |
| `GET /{index}` | Mapping, settings, doc count |
| `HEAD /{index}` | `200`/`404` |
| `DELETE /{index}` | Delete an index |
| `POST /{index}/_refresh` | Flush the write buffer to a searchable segment |
| `POST /{index}/_forcemerge` | Merge every live segment into one |
| `GET /{index}/_stats` | Document, segment and buffer counts |
| `POST /{index}/_doc` | Index one document, auto id |
| `PUT /{index}/_doc/{id}` | Index (or upsert) one document by id |
| `GET /{index}/_doc/{id}` | Fetch one document |
| `DELETE /{index}/_doc/{id}` | Delete one document |
| `POST /_bulk`, `POST /{index}/_bulk` | ES NDJSON bulk (`index`/`create`/`delete`) |
| `GET`/`POST /{index}/_search` | `?q=...` for ScopiQL; a JSON body for the DSL |
| `POST /{index}/_scopiql` | ScopiQL via JSON body: `{"query": "...", "size": ..., "from": ...}` |
| `POST /{index}/_analyze` | `{"analyzer": "standard", "text": "..."}` |
| `GET /_plugins` | Loaded/failed plugins |

Every error — from the engine, the query compiler, or a handler itself —
renders as `{"error": {"type": ..., "reason": ...}, "status": ...}` at the
matching HTTP status, via `ScopiError.to_dict()`. There is exactly one
exception handler for the whole app; no endpoint has its own bespoke error
shape.

### Deliberate deviations from real Elasticsearch

- `_cat/indices` returns JSON (a list of objects), not Elasticsearch's default
  plain-text table — this project has no reason to parse or emit that format,
  and JSON composes better with `jq`/`scopi --json`.
- `GET _doc/{id}` for a missing document is a genuine `404`, not a `200` with
  `"found": false`.
- `_analyze`'s token objects carry `token` and `position` only — no
  `start_offset`/`end_offset`, since the analyzer layer doesn't track them.
- `_search`'s `sort` is a genuine index-wide sort, bounded by
  `max_sort_candidates` rather than by shard-local top-N behaviour — see
  [QUERY_LANGUAGE.md](QUERY_LANGUAGE.md#sort-is-a-real-index-wide-sort).

### Running it in Docker

```bash
docker compose up --build
```

Builds the image (`Dockerfile`: `python:3.11-slim`, non-root, `EXPOSE 9500`)
and starts one `scopiengine` service on `localhost:9500`, with a named volume
(`scopi-data`) for `/data` so the default `sqlite:////data/scopi.db` survives
a container recreate. `docker-compose.yml` also carries a commented-out MS SQL
Server profile (`docker compose --profile mssql up`) — see
[STORAGE_BACKENDS.md](STORAGE_BACKENDS.md) to switch `SCOPI_STORAGE` over to it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | A ScopiEngine error — the message names the error type |
| `2` | Bad command-line usage |
| `130` | Interrupted with Ctrl-C (every command except `scopi ingest file`) |

`scopi ingest file` is the one deliberate exception: it installs its own
`SIGINT`/`SIGTERM` handlers, so Ctrl-C (or `kill`, not `kill -9`) triggers a
clean stop rather than the usual `130` abort — the reader stops, the queue
drains, the in-flight batch and its checkpoint flush as one transaction, and
the process exits **`0`**, so `--resume` continues exactly where it left off.
See [INGEST.md](INGEST.md).
