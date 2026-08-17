"""Plugin discovery and loading.

Three sources feed the same registry, in this order:

1. Built-ins (:mod:`scopiengine.plugins.builtin`) — always loaded first, through
   the exact same hook mechanism a third-party plugin uses.
2. Entry points registered under the ``scopiengine.plugins`` group (the normal
   way an installed package — like ``pip install``-ed
   ``examples/plugins/scopi_plugin_sample`` — announces itself).
3. Extra module paths from :attr:`scopiengine.settings.Settings.plugins`, which
   itself already merges the ``SCOPI_PLUGINS=mod1,mod2`` environment variable
   (see :mod:`scopiengine.settings`) — so nothing here re-reads the environment.

A plugin that raises while loading is logged and skipped, unless
``strict_plugins`` is set, in which case the failure becomes a startup error —
exactly :data:`~scopiengine.settings.Settings.strict_plugins`.
"""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.metadata import entry_points
from types import ModuleType

from scopiengine.analysis.registry import AnalyzerRegistry
from scopiengine.errors import PluginError
from scopiengine.plugins.hooks import EventBus
from scopiengine.plugins.spec import IngestProcessor
from scopiengine.settings import Settings
from scopiengine.storage.factory import register_backend

__all__ = ["PluginLoadResult", "PluginRegistry", "discover_plugins"]

_logger = logging.getLogger("scopiengine.plugins")

#: Built-in plugin modules, loaded before any external one.
_BUILTIN_MODULES: tuple[str, ...] = (
    "scopiengine.plugins.builtin.analyzers",
    "scopiengine.plugins.builtin.storage",
)

#: The ``importlib.metadata`` entry-point group third-party plugins register under.
ENTRY_POINT_GROUP = "scopiengine.plugins"


class PluginLoadResult:
    """Outcome of loading one plugin module.

    Attributes:
        name: The module path (or module's ``__name__``) that was loaded.
        ok: Whether every hook it implements ran without raising.
        error: The failure message, or ``None`` on success.
    """

    __slots__ = ("error", "name", "ok")

    def __init__(self, name: str, ok: bool, error: str | None = None) -> None:
        self.name = name
        self.ok = ok
        self.error = error

    def __repr__(self) -> str:
        return f"PluginLoadResult(name={self.name!r}, ok={self.ok}, error={self.error!r})"


class PluginRegistry:
    """Collects everything plugins register: analyzers, ingest processors, events.

    Storage backends register straight into
    :mod:`scopiengine.storage.factory`'s own registry (via
    :func:`~scopiengine.storage.factory.register_backend`), so there is nothing
    to collect for that hook here beyond having called it.

    Args:
        analyzers: Registry every ``analyzer`` hook mutates.
        strict: When ``True``, a raising plugin aborts loading instead of being
            skipped.
    """

    def __init__(self, *, analyzers: AnalyzerRegistry | None = None, strict: bool = False) -> None:
        self.analyzers = analyzers if analyzers is not None else AnalyzerRegistry()
        self.events = EventBus()
        self.ingest_processors: dict[str, IngestProcessor] = {}
        self.strict = strict
        self.results: list[PluginLoadResult] = []

    @property
    def loaded(self) -> tuple[str, ...]:
        """Names of every plugin that loaded successfully, in load order."""
        return tuple(r.name for r in self.results if r.ok)

    @property
    def failed(self) -> tuple[PluginLoadResult, ...]:
        """Every plugin that failed to load, in load order."""
        return tuple(r for r in self.results if not r.ok)

    def load(self, target: str | ModuleType) -> PluginLoadResult:
        """Load one plugin, by module path or an already-imported module.

        A raising plugin (import failure or a hook function itself raising) is
        caught, logged and recorded as failed — unless :attr:`strict` is set, in
        which case it is re-raised as a :class:`~scopiengine.errors.PluginError`.
        """
        name = target if isinstance(target, str) else target.__name__
        try:
            module = import_module(target) if isinstance(target, str) else target
            self._apply(module)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            _logger.error("plugin %r failed to load: %s", name, message)
            result = PluginLoadResult(name, ok=False, error=message)
            self.results.append(result)
            if self.strict:
                raise PluginError(f"plugin {name!r} failed to load: {message}") from exc
            return result
        result = PluginLoadResult(name, ok=True)
        self.results.append(result)
        return result

    def _apply(self, module: ModuleType) -> None:
        register_analyzers = getattr(module, "register_analyzers", None)
        if register_analyzers is not None:
            register_analyzers(self.analyzers)

        register_ingest_processors = getattr(module, "register_ingest_processors", None)
        if register_ingest_processors is not None:
            self.ingest_processors.update(register_ingest_processors())

        register_storage_backends = getattr(module, "register_storage_backends", None)
        if register_storage_backends is not None:
            register_storage_backends(register_backend)

        register_events = getattr(module, "register_events", None)
        if register_events is not None:
            register_events(self.events)


def discover_plugins(settings: Settings) -> PluginRegistry:
    """Build a :class:`PluginRegistry` and load built-ins, entry points and configured modules.

    Raises:
        PluginError: ``settings.strict_plugins`` is set and any plugin —
            built-in or external — fails to load.
    """
    registry = PluginRegistry(strict=settings.strict_plugins)

    for module_name in _BUILTIN_MODULES:
        registry.load(module_name)

    seen: set[str] = set(_BUILTIN_MODULES)
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = entry_point.load()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            _logger.error("plugin entry point %r failed to load: %s", entry_point.name, message)
            registry.results.append(PluginLoadResult(entry_point.name, ok=False, error=message))
            if registry.strict:
                raise PluginError(
                    f"plugin entry point {entry_point.name!r} failed to load: {message}"
                ) from exc
            continue
        module_name = getattr(module, "__name__", entry_point.value)
        if module_name in seen:
            continue
        seen.add(module_name)
        registry.load(module)

    for module_name in settings.plugins:
        if module_name in seen:
            continue
        seen.add(module_name)
        registry.load(module_name)

    return registry
