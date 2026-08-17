"""Entry-point plugin discovery, genuinely exercised against an installed package.

``examples/plugins/scopi_plugin_sample`` is a real, ``pip install -e``-able
package (see its ``pyproject.toml``) registering under the
``scopiengine.plugins`` entry-point group. If it is not installed in this
environment, these tests skip rather than fail — installing it is a one-time
setup step (``pip install -e examples/plugins/scopi_plugin_sample``), not
something this suite does on every run.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from importlib.util import find_spec

import pytest

from scopiengine.engine import Engine
from scopiengine.plugins.registry import ENTRY_POINT_GROUP, discover_plugins
from scopiengine.settings import Settings

_installed = find_spec("scopi_plugin_sample") is not None

pytestmark = pytest.mark.skipif(
    not _installed,
    reason="examples/plugins/scopi_plugin_sample is not installed "
    "(pip install -e examples/plugins/scopi_plugin_sample)",
)


def test_sample_plugin_is_visible_as_an_entry_point() -> None:
    names = {ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert "scopi_plugin_sample" in names


def test_discover_plugins_loads_the_sample_plugin() -> None:
    registry = discover_plugins(Settings())
    assert "scopi_plugin_sample.plugin" in registry.loaded
    assert not registry.failed


def test_sample_plugin_analyzer_is_registered_and_usable() -> None:
    registry = discover_plugins(Settings())
    result = list(registry.analyzers.analyzer("shout").analyze("hello world"))
    assert result == [("HELLO", 0), ("WORLD", 1)]


def test_sample_plugin_storage_scheme_is_registered() -> None:
    discover_plugins(Settings())
    from scopiengine.storage.factory import available_schemes

    assert "dummy" in available_schemes()


def test_engine_end_to_end_against_the_dummy_backend() -> None:
    discover_plugins(Settings())
    settings = Settings(storage="dummy://example")
    with Engine.open(settings) as eng:
        eng.create_index("logs", {"properties": {"message": {"type": "text"}}})
        eng.index_documents("logs", [{"message": "hello from the dummy backend"}], ids=["1"])
        eng.refresh("logs")

        from scopiengine.query.ast import Term

        hits = eng.search("logs", Term("message", "dummy"))
        assert len(hits) == 1
        assert eng.get_document("logs", "1") == {"message": "hello from the dummy backend"}


def test_engine_end_to_end_using_the_shout_analyzer() -> None:
    discover_plugins(Settings())
    settings = Settings(storage="dummy://shout-example")
    with Engine.open(settings) as eng:
        eng.create_index("docs", {"properties": {"title": {"type": "text", "analyzer": "shout"}}})
        eng.index_documents("docs", [{"title": "quiet announcement"}], ids=["1"])
        eng.refresh("docs")

        from scopiengine.query.ast import Term

        # The shout analyzer uppercases at index time, so a lowercase term misses...
        assert eng.search("docs", Term("title", "quiet")) == []
        # ...and the uppercased term is what actually matches.
        assert len(eng.search("docs", Term("title", "QUIET"))) == 1
