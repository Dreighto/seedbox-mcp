from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from seedbox_mcp.schemas import CoercedInt, _coerce_int


def test_coerce_int_sci_notation_string() -> None:
    assert _coerce_int("5.01491855e+08") == 501491855


def test_coerce_int_plain_int() -> None:
    assert _coerce_int(501491855) == 501491855


def test_coerce_int_numeric_string() -> None:
    assert _coerce_int("501491855") == 501491855


def test_coerce_int_float() -> None:
    assert _coerce_int(501491855.0) == 501491855


def test_coerce_int_float_string() -> None:
    assert _coerce_int("501491855.0") == 501491855


def test_coerce_int_non_numeric_raises() -> None:
    with pytest.raises(ValueError):
        _coerce_int("not-a-number")


def test_coerced_int_type_adapter_validates_sci_notation_string() -> None:
    adapter = TypeAdapter(CoercedInt)
    assert adapter.validate_python("5.0e+08") == 500000000
