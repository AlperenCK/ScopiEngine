# scopi-plugin-sample

A real, installable ScopiEngine plugin used as the worked example in
[`docs/PLUGINS.md`](../../../docs/PLUGINS.md) and exercised by
`tests/integration/test_plugin_discovery.py`, which `pip install -e`s this
package into the test environment so entry-point discovery is genuinely tested,
not just called by hand.

It registers two things, through the same hooks any plugin uses:

- **`shout`** — an `analyzer` hook: the standard tokenizer followed by an
  `UPPERCASE`-ing filter instead of the usual lowercasing one.
- **`dummy://`** — a `storage_backend` hook: a fully in-memory
  `StorageBackend` implementation (no persistence, no external dependency),
  registered under the `dummy` DSN scheme.

## Install

```
pip install -e examples/plugins/scopi_plugin_sample
```

Once installed, `scopiengine.plugins.registry.discover_plugins` finds it
automatically via the `scopiengine.plugins` entry-point group — no
`SCOPI_PLUGINS` configuration needed.
