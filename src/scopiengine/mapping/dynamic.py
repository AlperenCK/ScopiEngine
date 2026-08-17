"""Dynamic mapping: infer field mappings from a document, on by default.

A new string field becomes ``text`` **and** gets a ``.keyword`` sub-field with
``ignore_above: 256`` — the same flattening trick :mod:`scopiengine.mapping.mapping`
uses for ``object``, just generating two dotted paths from one JSON key instead
of one. That is what lets a log field be full-text searched (via the ``text``
half) and aggregated or sorted on (via the ``.keyword`` half) without anyone
writing a mapping by hand — the entire point of dynamic mapping for log data,
where the schema is whatever today's log line happens to contain.
"""

from __future__ import annotations

from typing import Any

from scopiengine.mapping.fieldtypes import DEFAULT_IGNORE_ABOVE
from scopiengine.mapping.mapping import FieldMapping, Mapping, merge_mapping

__all__ = ["flatten_document", "infer_additions", "merge_dynamic"]


def flatten_document(doc: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested document to dotted leaf paths, the same way :func:`infer_additions` does.

    Kept in sync with :func:`_infer_from_doc` on purpose: whatever path that
    function would infer a type for, this function must produce the matching
    value under the identical dotted key, since
    :class:`~scopiengine.index.writer.IndexWriter` indexes exactly the paths this
    returns against exactly the mapping :func:`merge_dynamic` produced.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_document(value, path))
        else:
            out[path] = value
    return out


def _infer_scalar(value: Any) -> FieldMapping | None:
    """Infer a single :class:`FieldMapping` for one non-string, non-object leaf value."""
    if isinstance(value, bool):
        return FieldMapping(type="boolean")
    if isinstance(value, int):
        return FieldMapping(type="long")
    if isinstance(value, float):
        return FieldMapping(type="double")
    return None


def _infer_from_doc(
    doc: dict[str, Any],
    prefix: str,
    out: dict[str, FieldMapping],
    known: dict[str, FieldMapping],
) -> None:
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            _infer_from_doc(value, path, out, known)
            continue
        if path in known:
            # Already mapped — explicitly, or by an earlier document's dynamic
            # addition — so this value is never re-inferred against it. A
            # genuine type mismatch (an explicit `keyword` field fed a nested
            # object, say) surfaces later, at indexing time, as a MappingError
            # from scopiengine.mapping.fieldtypes instead of here.
            continue
        if isinstance(value, list):
            value = next((item for item in value if item is not None), None)
        if value is None:
            continue
        if isinstance(value, str):
            out[path] = FieldMapping(type="text")
            out[f"{path}.keyword"] = FieldMapping(type="keyword", ignore_above=DEFAULT_IGNORE_ABOVE)
            continue
        scalar = _infer_scalar(value)
        if scalar is not None:
            out[path] = scalar


def infer_additions(doc: dict[str, Any], *, known: Mapping | None = None) -> Mapping:
    """Infer field mappings for every leaf value in ``doc`` that ``known`` does not already map.

    Nested objects are flattened to dotted paths, exactly as
    :func:`scopiengine.mapping.mapping.parse_mapping` flattens an explicit
    ``object`` mapping. A value of ``None``, or a list whose every element is
    ``None`` (or that is empty), contributes nothing — there is no type to infer.
    A path already present in ``known`` is left alone entirely, including the
    ``.keyword`` companion a string value would otherwise get — an already-mapped
    field, however it came to be mapped, is never reinterpreted.
    """
    fields: dict[str, FieldMapping] = {}
    known_fields = known.fields if known is not None else {}
    _infer_from_doc(doc, "", fields, known_fields)
    return Mapping(fields=fields)


def merge_dynamic(mapping: Mapping, doc: dict[str, Any]) -> Mapping:
    """Extend ``mapping`` with any field ``doc`` introduces that it does not already have.

    Fields already present in ``mapping`` are left exactly as declared — dynamic
    mapping only ever adds fields, it never widens, narrows or re-derives an
    existing one's type.

    Raises:
        MappingError: Two distinct new fields introduced by ``doc`` collide on
            the same dotted path with different inferred types (an ``object``
            field also present as a scalar, for instance).
    """
    return merge_mapping(mapping, infer_additions(doc, known=mapping))
