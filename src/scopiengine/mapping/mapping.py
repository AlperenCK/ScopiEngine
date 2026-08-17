"""Mapping validation and the ``object`` → dotted-path flattening.

A user-facing mapping nests ``object`` fields (``{"user": {"properties":
{"name": {"type": "keyword"}}}}``); everything downstream of here — the writer,
the searcher, dynamic mapping — works against the flattened form instead
(``{"user.name": FieldMapping(type="keyword")}``), so there is exactly one place
that understands nesting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scopiengine.errors import MappingError
from scopiengine.mapping.fieldtypes import FIELD_TYPES

__all__ = ["INDEX_OPTIONS", "FieldMapping", "Mapping", "merge_mapping", "parse_mapping"]

#: Valid values for a field's ``index_options``, from least to most detail kept.
INDEX_OPTIONS = ("docs", "freqs", "positions")


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """One leaf field's resolved settings, after ``object`` flattening.

    Attributes:
        type: One of :data:`~scopiengine.mapping.fieldtypes.FIELD_TYPES`.
        analyzer: Named analyzer used to index text into this field. Ignored
            for non-``text`` types. Defaults to ``"standard"`` for ``text``.
        search_analyzer: Named analyzer used to analyze query text against this
            field, when it should differ from ``analyzer`` (e.g. a synonym
            expansion applied only at query time). Falls back to ``analyzer``.
        index: Whether the field is searchable at all. A stored-only field
            (``index=False``) is still returned by a fetch but never matches a query.
        store: Whether the field's raw value is retrievable on its own, outside
            of the document's full stored source.
        index_options: How much detail the postings carry — ``"docs"`` (presence
            only), ``"freqs"`` (term frequency, no positions) or ``"positions"``
            (term frequency and positions, required for phrase queries).
        ignore_above: For ``keyword`` fields, values longer than this are stored
            but not indexed as a term (mirrors Elasticsearch's field of the same
            name). ``None`` means no limit.
    """

    type: str
    analyzer: str | None = None
    search_analyzer: str | None = None
    index: bool = True
    store: bool = False
    index_options: str = "positions"
    ignore_above: int | None = None

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise MappingError(f"unknown field type: {self.type!r}")
        if self.index_options not in INDEX_OPTIONS:
            raise MappingError(
                f"index_options must be one of {INDEX_OPTIONS}, got {self.index_options!r}"
            )

    def effective_search_analyzer(self) -> str | None:
        """The analyzer to use for query-time text, falling back to the index-time one."""
        return self.search_analyzer or self.analyzer

    def to_dict(self) -> dict[str, Any]:
        """Render as a plain JSON-serialisable dict, omitting unset optional keys."""
        data: dict[str, Any] = {"type": self.type}
        if self.analyzer is not None:
            data["analyzer"] = self.analyzer
        if self.search_analyzer is not None:
            data["search_analyzer"] = self.search_analyzer
        if not self.index:
            data["index"] = False
        if self.store:
            data["store"] = True
        if self.index_options != "positions":
            data["index_options"] = self.index_options
        if self.ignore_above is not None:
            data["ignore_above"] = self.ignore_above
        return data


@dataclass(frozen=True, slots=True)
class Mapping:
    """A whole index's field mapping, already flattened to dotted leaf paths.

    Attributes:
        fields: Dotted field path to its resolved :class:`FieldMapping`.
    """

    fields: dict[str, FieldMapping] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render as the flat ``{"properties": {...}}`` shape a backend stores."""
        return {"properties": {name: fm.to_dict() for name, fm in sorted(self.fields.items())}}


def _default_analyzer(field_type: str, declared: str | None) -> str | None:
    if declared is not None:
        return declared
    return "standard" if field_type == "text" else None


def _flatten(properties: dict[str, Any], prefix: str, out: dict[str, FieldMapping]) -> None:
    for name, spec in properties.items():
        if not isinstance(spec, dict) or "type" not in spec:
            raise MappingError(f"field {name!r} is missing a 'type'")
        path = f"{prefix}.{name}" if prefix else name
        field_type = spec["type"]
        if field_type == "object":
            nested = spec.get("properties", {})
            if not isinstance(nested, dict):
                raise MappingError(f"object field {path!r} must declare 'properties'")
            _flatten(nested, path, out)
            continue
        if field_type not in FIELD_TYPES:
            raise MappingError(f"field {path!r}: unknown type {field_type!r}")
        if path in out:
            raise MappingError(f"duplicate field path: {path!r}")
        out[path] = FieldMapping(
            type=field_type,
            analyzer=_default_analyzer(field_type, spec.get("analyzer")),
            search_analyzer=spec.get("search_analyzer"),
            index=spec.get("index", True),
            store=spec.get("store", False),
            index_options=spec.get("index_options", "positions"),
            ignore_above=spec.get("ignore_above"),
        )


def parse_mapping(raw: dict[str, Any]) -> Mapping:
    """Validate a user-supplied mapping and flatten it to dotted leaf paths.

    Args:
        raw: ``{"properties": {field_name: {"type": ..., ...}, ...}}``.

    Returns:
        The flattened, validated mapping.

    Raises:
        MappingError: A field is missing ``type``, declares an unknown type,
            an ``object`` field is missing ``properties``, two fields flatten
            to the same dotted path, or ``index_options`` is not recognised.
    """
    if not isinstance(raw, dict):
        raise MappingError("mapping must be a JSON object")
    properties = raw.get("properties", {})
    if not isinstance(properties, dict):
        raise MappingError("mapping 'properties' must be a JSON object")
    fields: dict[str, FieldMapping] = {}
    _flatten(properties, "", fields)
    return Mapping(fields=fields)


def merge_mapping(existing: Mapping, additions: Mapping) -> Mapping:
    """Merge ``additions`` into ``existing``, returning a new :class:`Mapping`.

    A field present in both must agree on ``type`` — this is the check that
    keeps a document from silently corrupting another document's field.

    Raises:
        MappingError: A field in both mappings declares conflicting types.
    """
    merged = dict(existing.fields)
    for path, incoming in additions.fields.items():
        current = merged.get(path)
        if current is not None and current.type != incoming.type:
            raise MappingError(
                f"field {path!r} is already mapped as {current.type!r}, "
                f"cannot also map it as {incoming.type!r}"
            )
        if current is None:
            merged[path] = incoming
    return Mapping(fields=merged)
