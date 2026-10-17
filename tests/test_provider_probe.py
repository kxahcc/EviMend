"""Balance observations are validated without sending inference or exposing keys."""

from __future__ import annotations

from typing import Any

import pytest

from dfx_agent.provider_probe import cny_balance


@pytest.mark.parametrize("value", ["20.00", "0", "19.987654"])
def test_cny_balance(value: str) -> None:
    assert cny_balance({"is_available": True, "balance_infos": [
        {"currency": "USD", "total_balance": "1"}, {"currency": "CNY", "total_balance": value}]}) == value


@pytest.mark.parametrize("response", [None, {}, {"is_available": False},
    {"is_available": True, "balance_infos": []},
    *[{"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": value}]}
      for value in (None, 20, "NaN", "Infinity", "-1", "invalid")],
    {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "1"}] * 2}])
def test_balance_fails_closed(response: Any) -> None:
    with pytest.raises(ValueError, match="provider_"):
        cny_balance(response)
