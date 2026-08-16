"""The error hierarchy carries stable types and HTTP statuses."""

from __future__ import annotations

import pytest

from scopiengine.errors import (
    BackendNotAvailableError,
    IndexNotFoundError,
    InvalidQueryError,
    ScopiError,
    StorageError,
    UnsupportedFeatureError,
)


def test_every_error_is_a_scopi_error() -> None:
    assert issubclass(BackendNotAvailableError, StorageError)
    assert issubclass(StorageError, ScopiError)
    assert issubclass(UnsupportedFeatureError, InvalidQueryError)


def test_to_dict_renders_the_rest_envelope() -> None:
    payload = IndexNotFoundError("no such index [logs]").to_dict()
    assert payload == {
        "error": {"type": "index_not_found", "reason": "no such index [logs]"},
        "status": 404,
    }


def test_detail_is_merged_into_the_error_object() -> None:
    payload = UnsupportedFeatureError("unsupported key", detail={"key": "highlight"}).to_dict()
    assert payload["error"]["key"] == "highlight"
    assert payload["status"] == 400


def test_reason_is_the_exception_message() -> None:
    with pytest.raises(ScopiError, match="boom"):
        raise ScopiError("boom")


@pytest.mark.parametrize(
    "error,status",
    [
        (IndexNotFoundError("x"), 404),
        (InvalidQueryError("x"), 400),
        (BackendNotAvailableError("x"), 501),
        (StorageError("x"), 500),
    ],
)
def test_statuses_are_declared(error: ScopiError, status: int) -> None:
    assert error.status == status
