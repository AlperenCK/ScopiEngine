"""Plugin registration, isolation of a raising plugin, and strict_plugins behaviour."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import pytest

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.analysis.tokenizers import whitespace
from scopiengine.analysis.tokens import Token
from scopiengine.errors import PluginError
from scopiengine.plugins.registry import PluginRegistry, discover_plugins
from scopiengine.settings import Settings
from scopiengine.storage.factory import available_schemes


@pytest.fixture(autouse=True)
def _clean_sys_modules() -> Iterator[None]:
    """Remove any throwaway ``_test_plugin_*`` module this file installs, after each test."""
    yield
    for name in [n for n in sys.modules if n.startswith("_test_plugin_")]:
        del sys.modules[name]


def _install_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


# -- built-ins load through the ordinary hook path ----------------------------


def test_discover_plugins_loads_the_builtins() -> None:
    registry = discover_plugins(Settings())
    assert "scopiengine.plugins.builtin.analyzers" in registry.loaded
    assert "scopiengine.plugins.builtin.storage" in registry.loaded
    assert not registry.failed


def test_builtin_analyzer_plugin_registers_standard_analyzers() -> None:
    registry = discover_plugins(Settings())
    assert set(registry.analyzers.analyzer_names()) >= {"standard", "stop_en", "log"}


def test_builtin_storage_plugin_registers_sqlite_scheme() -> None:
    discover_plugins(Settings())
    assert "sqlite" in available_schemes()


# -- registering an analyzer hook ---------------------------------------------


def test_analyzer_hook_registers_a_custom_analyzer() -> None:
    def register_analyzers(registry: AnalyzerRegistry) -> None:
        registry.register_tokenizer("ws", whitespace)
        registry.register_filter(
            "upper", lambda tokens: (Token(t.text.upper(), t.position) for t in tokens)
        )
        from scopiengine.analysis.analyzer import Analyzer

        registry.register_analyzer("shouty", Analyzer(whitespace, (registry.filter("upper"),)))

    _install_module("_test_plugin_analyzer", register_analyzers=register_analyzers)
    plugins = PluginRegistry(strict=False)
    result = plugins.load("_test_plugin_analyzer")

    assert result.ok
    assert list(plugins.analyzers.analyzer("shouty").analyze("hi there")) == [
        ("HI", 0),
        ("THERE", 1),
    ]


# -- registering a storage_backend hook ---------------------------------------


def test_storage_backend_hook_registers_a_new_scheme() -> None:
    def factory(dsn: str, options: dict[str, object]) -> object:
        raise AssertionError("never actually called in this test")

    def register_storage_backends(register: object) -> None:
        register("_test_scheme", factory)  # type: ignore[operator]

    _install_module("_test_plugin_storage", register_storage_backends=register_storage_backends)
    plugins = PluginRegistry(strict=False)
    result = plugins.load("_test_plugin_storage")

    assert result.ok
    assert "_test_scheme" in available_schemes()
    # Clean up the process-global registry so other tests don't see it.
    import scopiengine.storage.factory as factory_module

    del factory_module._REGISTRY["_test_scheme"]


# -- registering an event hook ------------------------------------------------


def test_event_hook_subscribes_and_receives_events() -> None:
    received: list[tuple[str, ...]] = []

    def register_events(bus: object) -> None:
        bus.subscribe("on_index_created", lambda *args: received.append(args))  # type: ignore[attr-defined]

    _install_module("_test_plugin_events", register_events=register_events)
    plugins = PluginRegistry(strict=False)
    plugins.load("_test_plugin_events")

    plugins.events.fire("on_index_created", "logs")
    assert received == [("logs",)]


def test_event_bus_rejects_unknown_event_names() -> None:
    plugins = PluginRegistry()
    with pytest.raises(ValueError, match="unknown event"):
        plugins.events.subscribe("on_something_else", lambda: None)


# -- registering an ingest_processor hook -------------------------------------


def test_ingest_processor_hook_registers_processors() -> None:
    def process(doc: dict[str, object], ctx: dict[str, object]) -> dict[str, object] | None:
        return {**doc, "processed": True}

    def register_ingest_processors() -> dict[str, object]:
        return {"mark_processed": process}

    _install_module("_test_plugin_ingest", register_ingest_processors=register_ingest_processors)
    plugins = PluginRegistry(strict=False)
    plugins.load("_test_plugin_ingest")

    assert "mark_processed" in plugins.ingest_processors
    assert plugins.ingest_processors["mark_processed"]({"a": 1}, {}) == {
        "a": 1,
        "processed": True,
    }


# -- isolation: a raising plugin is logged and skipped ------------------------


def test_raising_plugin_is_isolated_by_default() -> None:
    def register_analyzers(registry: AnalyzerRegistry) -> None:
        raise RuntimeError("boom")

    _install_module("_test_plugin_boom", register_analyzers=register_analyzers)
    plugins = PluginRegistry(strict=False)
    result = plugins.load("_test_plugin_boom")

    assert not result.ok
    assert result.error is not None and "boom" in result.error
    assert plugins.failed == (result,)
    assert plugins.loaded == ()


def test_a_raising_plugin_does_not_prevent_other_plugins_from_loading() -> None:
    def register_analyzers_boom(registry: AnalyzerRegistry) -> None:
        raise RuntimeError("boom")

    def register_analyzers_ok(registry: AnalyzerRegistry) -> None:
        registry.register_tokenizer("ws2", whitespace)

    _install_module("_test_plugin_boom2", register_analyzers=register_analyzers_boom)
    _install_module("_test_plugin_ok", register_analyzers=register_analyzers_ok)

    plugins = PluginRegistry(strict=False)
    plugins.load("_test_plugin_boom2")
    plugins.load("_test_plugin_ok")

    assert "_test_plugin_ok" in plugins.loaded
    assert any(r.name == "_test_plugin_boom2" for r in plugins.failed)
    assert plugins.analyzers.tokenizer("ws2") is whitespace


def test_import_error_is_also_isolated() -> None:
    plugins = PluginRegistry(strict=False)
    result = plugins.load("_test_plugin_does_not_exist_at_all")
    assert not result.ok
    assert "ModuleNotFoundError" in (result.error or "")


# -- strict_plugins turns a raise into a startup failure ----------------------


def test_strict_plugins_raises_plugin_error_on_a_raising_plugin() -> None:
    def register_analyzers(registry: AnalyzerRegistry) -> None:
        raise RuntimeError("boom")

    _install_module("_test_plugin_strict_boom", register_analyzers=register_analyzers)
    plugins = PluginRegistry(strict=True)

    with pytest.raises(PluginError, match="boom"):
        plugins.load("_test_plugin_strict_boom")


def test_strict_plugins_setting_propagates_from_discover_plugins() -> None:
    def register_analyzers(registry: AnalyzerRegistry) -> None:
        raise RuntimeError("boom")

    _install_module("_test_plugin_strict_discover", register_analyzers=register_analyzers)
    settings = Settings(strict_plugins=True, plugins=("_test_plugin_strict_discover",))

    with pytest.raises(PluginError):
        discover_plugins(settings)


def test_non_strict_discover_plugins_collects_the_failure_instead_of_raising() -> None:
    def register_analyzers(registry: AnalyzerRegistry) -> None:
        raise RuntimeError("boom")

    _install_module("_test_plugin_nonstrict_discover", register_analyzers=register_analyzers)
    settings = Settings(strict_plugins=False, plugins=("_test_plugin_nonstrict_discover",))

    registry = discover_plugins(settings)
    assert any(r.name == "_test_plugin_nonstrict_discover" for r in registry.failed)
