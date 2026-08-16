# Installation

## Requirements

- Python 3.11 or 3.12
- No JVM, no external service — the default storage backend is SQLite, which ships with Python

## From PyPI

```bash
pip install scopiengine
scopi version
```

## From source

```bash
git clone https://github.com/AlperenCK/ScopiEngine.git
cd ScopiEngine
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
scopi version
```

On Windows the activation step is `.venv\Scripts\activate`.

## Optional extras

| Extra | Install | Brings |
|---|---|---|
| `mssql` | `pip install "scopiengine[mssql]"` | MS SQL Server storage backend (`pyodbc`) |
| `dev` | `pip install -e ".[dev]"` | pytest, ruff, mypy, build |

The `mssql` extra also needs the Microsoft ODBC driver on the host. On Debian and
Ubuntu that is the `msodbcsql18` package; see
[docs/STORAGE_BACKENDS.md](STORAGE_BACKENDS.md) for the driver and DSN details.

## Verifying the installation

```bash
scopi version          # 1.0.0
scopi info             # version, tagline and the resolved configuration
scopi --help           # available command groups
```

## Configuration

Settings resolve from four layers, each overriding the one before it:

1. Built-in defaults
2. A JSON config file passed with `--config` (or `SCOPI_CONFIG`)
3. `SCOPI_*` environment variables
4. Command-line options

Every setting has an environment variable made from its name: `storage` reads
`SCOPI_STORAGE`, `batch_size` reads `SCOPI_BATCH_SIZE`, and so on.

```bash
export SCOPI_STORAGE="sqlite:///./data/scopi.db"
export SCOPI_LOG_FORMAT=json          # text | json
scopi info
```

A config file is a flat JSON object using the same names:

```json
{
  "storage": "sqlite:///./data/scopi.db",
  "port": 9500,
  "log_level": "INFO",
  "batch_size": 5000
}
```

Unknown keys are rejected rather than ignored, so a typo surfaces immediately.

| Setting | Default | Meaning |
|---|---|---|
| `storage` | `sqlite:///./data/scopi.db` | Storage DSN, selects the backend |
| `host` | `127.0.0.1` | Bind interface for `scopi serve` |
| `port` | `9500` | Bind port for `scopi serve` |
| `log_level` | `INFO` | Root log level |
| `log_format` | `text` | `text` for humans, `json` for collectors |
| `plugins` | *(empty)* | Extra plugin modules, on top of entry-point discovery |
| `strict_plugins` | `false` | Fail startup on a plugin error instead of skipping it |
| `buffer_mb` | `64` | In-memory index buffer before a segment is flushed |
| `batch_size` | `5000` | Documents per ingestion batch |
| `batch_bytes` | `8388608` | Approximate bytes per ingestion batch |
| `flush_interval` | `2.0` | Seconds before a partial batch is flushed anyway |
| `queue_size` | `8` | Bounded queue depth between reader and indexer |
| `max_segments` | `10` | Live segment count that triggers an automatic merge |

Logs go to stderr, command output to stdout — so `scopi search --json ... | jq`
stays usable with logging turned on.

## Next steps

- [docs/USAGE.md](USAGE.md) — indexing, searching and serving
- [docs/INGEST.md](INGEST.md) — pulling large log files in
