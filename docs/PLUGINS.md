# Plugins

ScopiEngine has four extension points, all load-bearing (nothing in the engine
is hidden behind a "real" internal path that a plugin cannot also take):

| Hook | Plugin defines | Purpose |
|---|---|---|
| `analyzer` | `register_analyzers(registry)` | Add tokenizers, filters, named analyzers. |
| `ingest_processor` | `register_ingest_processors() -> dict[str, IngestProcessor]` | Add named `process(doc, ctx) -> dict \| None` callables. |
| `storage_backend` | `register_storage_backends(register)` | Map a DSN scheme to a backend factory. |
| `event` | `register_events(bus)` | Subscribe to `on_index_created` / `on_documents_indexed` / `on_search`. |

A plugin implements zero or more of these — a module that only wants a custom
analyzer defines `register_analyzers` and nothing else.

The built-ins (`scopiengine.plugins.builtin.analyzers`,
`scopiengine.plugins.builtin.storage`) register through these exact same
functions. There is no privileged route: what a first-party module does, a
third-party one can do identically.

## Discovery

`scopiengine.plugins.registry.discover_plugins(settings)` loads, in order:

1. **Built-ins** — always first.
2. **Entry points** registered under the `scopiengine.plugins` group — the
   normal way an installed package announces itself (see the worked example
   below).
3. **Extra module paths** from `Settings.plugins`, which itself already merges
   the `SCOPI_PLUGINS=mod1,mod2` environment variable — nothing in the plugin
   loader re-reads the environment directly.

A plugin that raises while loading — on import, or from any hook function it
defines — is caught, logged, and recorded as failed; loading continues with
the next plugin. Set `strict_plugins` (CLI: `--strict-plugins`; env:
`SCOPI_STRICT_PLUGINS=true`; `Settings.strict_plugins`) to turn that same
failure into a `PluginError` that aborts `discover_plugins` instead.

```python
from scopiengine.settings import Settings
from scopiengine.plugins import discover_plugins

plugins = discover_plugins(Settings())
plugins.loaded    # -> tuple of module names that loaded cleanly
plugins.failed    # -> tuple of PluginLoadResult for anything that didn't
```

## The four hooks

### `analyzer`

```python
def register_analyzers(registry: AnalyzerRegistry) -> None:
    registry.register_tokenizer("my_tokenizer", my_tokenizer_fn)
    registry.register_filter("my_filter", my_filter_fn)
    registry.register_analyzer("my_analyzer", Analyzer(my_tokenizer_fn, (my_filter_fn,)))
```

A tokenizer is `(text: str) -> Iterator[Token]`; a filter is
`(tokens: Iterable[Token]) -> Iterator[Token]`. `Token(text, position)` is a
frozen dataclass. See `scopiengine.analysis.analyzer` for why a filter that
drops a token must not renumber the positions of the ones it keeps (phrase
query correctness depends on it).

### `ingest_processor`

```python
def register_ingest_processors() -> dict[str, IngestProcessor]:
    return {"my_processor": my_processor}

def my_processor(doc: dict, ctx: dict) -> dict | None:
    ...  # return the (possibly rewritten) document, or None to drop it
```

Only the contract is fixed in this release — PR 5 supplies the real
processors (grok/regex field extraction, renames, drops) and the ingest
pipeline that actually calls into `Engine.plugins.ingest_processors`.

### `storage_backend`

```python
def register_storage_backends(register: Callable[[str, BackendFactory], None]) -> None:
    register("myscheme", my_backend_factory)

def my_backend_factory(dsn: str, options: dict[str, object]) -> StorageBackend:
    return MyBackend(dsn)
```

`register` **is** `scopiengine.storage.factory.register_backend` — plugins
call it directly rather than going through a second registration mechanism.
A backend must implement the full `StorageBackend` abstract interface (see
`src/scopiengine/storage/base.py` and STORAGE_BACKENDS.md); the worked example
below (`DummyBackend`) is a complete, if deliberately non-durable, reference.

### `event`

```python
def register_events(bus: EventBus) -> None:
    bus.subscribe("on_index_created", lambda info: ...)
    bus.subscribe("on_documents_indexed", lambda index, doc_ids: ...)
    bus.subscribe("on_search", lambda index, query, hits: ...)
```

A subscriber that raises is logged and does not stop the event from reaching
the other subscribers, and never propagates back to whatever fired the event —
an observer's bug must never break ingestion or search.

## Writing and installing a plugin: the worked example

`examples/plugins/scopi_plugin_sample` is a real, installable package
exercising two of the four hooks:

- **`shout`** (`analyzer` hook) — the standard tokenizer followed by an
  uppercasing filter, the mirror image of the built-in `lowercase`.
- **`dummy://`** (`storage_backend` hook) — a complete, fully in-memory
  `StorageBackend` implementation, registered under the `dummy` DSN scheme.

Its layout:

```
examples/plugins/scopi_plugin_sample/
├── pyproject.toml                          # declares the entry point (below)
├── README.md
└── src/scopi_plugin_sample/
    ├── __init__.py
    ├── plugin.py                           # register_analyzers / register_storage_backends
    └── dummy_backend.py                    # DummyBackend(StorageBackend)
```

The entry point that makes discovery automatic:

```toml
[project.entry-points."scopiengine.plugins"]
scopi_plugin_sample = "scopi_plugin_sample.plugin"
```

Install it:

```bash
pip install -e examples/plugins/scopi_plugin_sample
```

Once installed, no further configuration is needed —
`discover_plugins(settings)` finds it via
`importlib.metadata.entry_points(group="scopiengine.plugins")` on every call.
`tests/integration/test_plugin_discovery.py` installs this exact package (or
skips if it isn't installed) and asserts the whole path end to end: the entry
point is visible, `discover_plugins` loads it, the `shout` analyzer produces
uppercased terms, the `dummy://` scheme resolves to a working backend, and an
`Engine` built against `dummy://...` can create an index, ingest documents and
search them — genuinely exercising entry-point discovery, not just calling the
plugin's functions by hand.
