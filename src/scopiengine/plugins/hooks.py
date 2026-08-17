"""The four load-bearing plugin hooks and the event bus the ``event`` hook subscribes to.

1. ``analyzer`` — a plugin module defines ``register_analyzers(registry)`` to add
   tokenizers, filters and named analyzers.
2. ``ingest_processor`` — a plugin module defines ``register_ingest_processors()``
   returning ``{name: IngestProcessor}``. ``scopiengine.plugins.builtin.processors``
   supplies the built-ins (``json_line``, ``regex_extract``, ``timestamp``); see
   ``docs/INGEST.md`` for the pipeline that calls into them.
3. ``storage_backend`` — a plugin module defines ``register_storage_backends(register)``,
   calling the given ``register`` (which *is*
   :func:`scopiengine.storage.factory.register_backend`) for each DSN scheme it adds.
4. ``event`` — a plugin module defines ``register_events(bus)``, subscribing
   callbacks on the :class:`EventBus` below.

All four are optional per plugin, and all four are exactly how the built-ins in
:mod:`scopiengine.plugins.builtin` register themselves too — there is no
privileged path a third-party plugin cannot also take.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

__all__ = ["HOOK_NAMES", "EventBus"]

_logger = logging.getLogger("scopiengine.plugins")

#: Every recognised hook function name, keyed by the hook it implements.
HOOK_NAMES: dict[str, str] = {
    "analyzer": "register_analyzers",
    "ingest_processor": "register_ingest_processors",
    "storage_backend": "register_storage_backends",
    "event": "register_events",
}

#: Event names the engine fires. Kept as a tuple (not an enum) so a plugin can
#: subscribe with a plain string and nothing more needs importing.
EVENT_NAMES: tuple[str, ...] = ("on_index_created", "on_documents_indexed", "on_search")


class EventBus:
    """Publish/subscribe hub for the engine's lifecycle events.

    A subscriber that raises is logged and does not stop the event from
    reaching the remaining subscribers, nor does it propagate to the caller
    that fired the event — an observer's bug must never break ingestion or search.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = {name: [] for name in EVENT_NAMES}

    def subscribe(self, event: str, callback: Callable[..., None]) -> None:
        """Register ``callback`` to run when ``event`` fires.

        Raises:
            ValueError: ``event`` is not one of :data:`EVENT_NAMES`.
        """
        if event not in self._subscribers:
            raise ValueError(f"unknown event: {event!r}; expected one of {EVENT_NAMES}")
        self._subscribers[event].append(callback)

    def fire(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        """Call every subscriber of ``event`` with the given arguments.

        Raises:
            ValueError: ``event`` is not one of :data:`EVENT_NAMES`.
        """
        if event not in self._subscribers:
            raise ValueError(f"unknown event: {event!r}; expected one of {EVENT_NAMES}")
        for callback in self._subscribers[event]:
            try:
                callback(*args, **kwargs)
            except Exception:
                _logger.exception("event subscriber for %r raised; continuing", event)
