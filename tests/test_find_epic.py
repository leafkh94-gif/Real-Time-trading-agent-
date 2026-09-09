"""
search_markets exists because a guessed epic fails silently: GER40 404s, the
backtest logs "received 0 bars", and the instrument looks configured while
contributing nothing.
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy.capital_feed import CapitalComFeed


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _feed(payload, calls):
    """A feed with the network and login replaced — search_markets only."""
    feed = CapitalComFeed.__new__(CapitalComFeed)
    feed._epic = "US500"
    feed._base = "https://example.invalid"

    def _request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return _Resp(payload)

    feed._request = _request
    return feed


def test_search_returns_the_fields_needed_to_configure_an_instrument():
    calls = []
    feed = _feed({"markets": [
        {"epic": "DE40", "instrumentName": "Germany 40",
         "instrumentType": "INDICES", "marketStatus": "TRADEABLE"},
    ]}, calls)

    assert feed.search_markets("dax") == [
        {"epic": "DE40", "name": "Germany 40",
         "type": "INDICES", "status": "TRADEABLE"},
    ]
    assert calls == [("GET", "/markets", {"params": {"searchTerm": "dax"}})]


def test_no_match_is_an_empty_list_not_an_error():
    # The whole point is to learn that a guessed name does not exist. If that
    # raised, the caller would read it as an outage — the same failure mode
    # this method was added to end.
    feed = _feed({"markets": []}, [])
    assert feed.search_markets("ger40") == []


def test_a_response_without_a_markets_key_does_not_crash():
    feed = _feed({}, [])
    assert feed.search_markets("anything") == []


def test_partial_market_rows_survive():
    # Capital.com does not document every field as required; a row missing a
    # name must still surrender its epic, which is the one thing we came for.
    feed = _feed({"markets": [{"epic": "OIL_CRUDE"}]}, [])
    assert feed.search_markets("oil") == [
        {"epic": "OIL_CRUDE", "name": "", "type": "", "status": ""},
    ]
