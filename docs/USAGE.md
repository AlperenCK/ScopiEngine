# Usage

Install first — see [docs/INSTALL.md](INSTALL.md).

## The two ways to drive the engine

ScopiEngine has one core and two front doors:

- **The CLI** runs the engine in process. No server needed, one code path.
- **The REST API** (`scopi serve`) exposes the same engine over HTTP on port 9500,
  with Elasticsearch-shaped requests and responses.

Both read the same configuration, so a storage DSN set once works for both.

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
| `scopi index` | Create, list, inspect, merge and delete indices | [ARCHITECTURE.md](ARCHITECTURE.md) |
| `scopi doc` / `scopi bulk` | Write and read documents | [ARCHITECTURE.md](ARCHITECTURE.md) |
| `scopi search` / `scopi analyze` | Query with ScopiQL or the JSON DSL | [QUERY_LANGUAGE.md](QUERY_LANGUAGE.md) |
| `scopi ingest` | Stream large log files in, with resume | [INGEST.md](INGEST.md) |
| `scopi plugin` | Inspect discovered plugins | [PLUGINS.md](PLUGINS.md) |
| `scopi serve` | Run the REST API | this document |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | A ScopiEngine error — the message names the error type |
| `2` | Bad command-line usage |
| `130` | Interrupted with Ctrl-C |

Ingestion treats `130` as a clean stop: the current batch and its checkpoint are
flushed before exit, so `--resume` continues exactly where it left off.
