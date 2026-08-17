"""Field mapping: types, order-preserving term encoding, validation and dynamic inference."""

from __future__ import annotations

from scopiengine.mapping.dynamic import infer_additions, merge_dynamic
from scopiengine.mapping.mapping import FieldMapping, Mapping, merge_mapping, parse_mapping

__all__ = [
    "FieldMapping",
    "Mapping",
    "infer_additions",
    "merge_dynamic",
    "merge_mapping",
    "parse_mapping",
]
