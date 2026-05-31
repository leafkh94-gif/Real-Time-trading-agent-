"""
Tests for CapitalComBroker.
All HTTP calls are mocked — no real network access.
"""
import time
import pytest
from unittest.mock import MagicMock, patch, call

from execution.capital_broker import CapitalComBroker, _DEMO_BASE, _ORDER_MIN_GAP
from execution.models import Signal


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_broker(**kw) -> CapitalComBroker:
    guard = MagicMock()
    guard.can_trade.return_value = (True, "")
    switch = MagicMock()
    switch.check.return_value = False
    store = MagicMock()
    store.count_open_positions.return_value = 0
    notifier = MagicMock()
    return CapitalComBroker(
        api_key="key", identifier="user@test.com", password="pass", demo=True,
        guard=guard, switch=switch, store=store, notifier=notifier, **kw
    )


def _login_response(cst="CST123", token="TOK456"):
    r = MagicMock()
    r.status_code = 200
    r.headers = {"CST": cst, "X-SECURITY-TOKEN": token}
    r.raise_for_status = MagicMock()
    return r


def _json_response(data: dict, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


# ── connect / login ───────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.post")
@patch("execution.capital_broker._req.request")
def test_connect_stores_session_tokens(mock_req, mock_post):
    mock_post.return_value = _login_response("MY_CST", "MY_TOKEN")
    mock_req.return_value = _json_response({})  # ping

    broker = _make_broker()
    broker.connect()

    assert broker._cst == "MY_CST"
    assert broker._security_token == "MY_TOKEN"


@patch("execution.capital_broker._req.post")
def test_connect_calls_correct_demo_url(mock_post):
    mock_post.return_value = _login_response()
    broker = _make_broker()
    with patch("threading.Thread"):  # suppress keepalive thread
        broker.connect()
    url = mock_post.call_args[0][0]
    assert url.startswith(_DEMO_BASE)


# ── _submit_order ─────────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.post")
@patch("execution.capital_broker._req.request")
def test_submit_order_happy_path(mock_req, mock_post):
    mock_post.return_value = _login_response()
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_abc123"}),          # POST /positions
        _json_response({                                          # GET /confirms/...
            "dealStatus": "ACCEPTED",
            "dealId": "deal-99",
            "level": 2345.50,
        }),
    ]

    order = broker._submit_order(Signal("buy", 0.05))

    assert order.order_id == "deal-99"
    assert order.price == 2345.50
    assert order.direction == "buy"


@patch("execution.capital_broker._req.post")
@patch("execution.capital_broker._req.request")
def test_submit_order_rejected_deal_raises(mock_req, mock_post):
    mock_post.return_value = _login_response()
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_abc"}),
        _json_response({"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"}),
    ]

    with pytest.raises(RuntimeError, match="REJECTED"):
        broker._submit_order(Signal("sell", 0.05))


@patch("execution.capital_broker._req.request")
def test_submit_order_includes_stop_and_profit(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_x"}),
        _json_response({"dealStatus": "ACCEPTED", "dealId": "d1", "level": 2300.0}),
    ]

    broker._submit_order(Signal("buy", 0.05, stop_loss=2280.0, take_profit=2340.0))

    post_call = mock_req.call_args_list[0]
    body = post_call[1]["json"]
    assert body["stopLevel"] == 2280.0
    assert body["profitLevel"] == 2340.0


# ── close_position ────────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.request")
def test_close_uses_post_delete_header(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_close1"}),           # POST (close)
        _json_response({"dealStatus": "ACCEPTED", "profit": 12.5}),  # GET /confirms
    ]

    pnl = broker.close_position("deal-77")

    close_call = mock_req.call_args_list[0]
    assert close_call[0][0] == "POST"
    assert "_method" in close_call[1].get("headers", {}) or \
           "_method" in close_call[0]  # extra_headers merged into headers
    assert pnl == 12.5


@patch("execution.capital_broker._req.request")
def test_close_calls_remove_position_and_records_pnl(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_c"}),
        _json_response({"profit": -5.0}),
    ]

    broker.close_position("pos-1")

    broker._store.remove_position.assert_called_once_with("pos-1")
    broker._store.add_pnl.assert_called_once_with(-5.0)
    broker._guard.record_pnl.assert_called_once_with(-5.0)


# ── reconcile ─────────────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.request")
def test_reconcile_syncs_positions_to_store(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.return_value = _json_response({
        "positions": [
            {
                "position": {
                    "dealId": "d1", "dealSize": 0.05, "direction": "BUY",
                    "openLevel": 2300.0, "createdDateUTC": "2024-01-01T00:00:00Z",
                },
                "market": {"epic": "GOLD"},
            }
        ]
    })

    orders = broker.reconcile()

    assert len(orders) == 1
    assert orders[0].order_id == "d1"
    broker._store.add_position.assert_called_once()
    call_kw = broker._store.add_position.call_args[1]
    assert call_kw["position_id"] == "d1"
    assert call_kw["direction"] == "buy"


@patch("execution.capital_broker._req.request")
def test_reconcile_empty_returns_empty_list(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"
    mock_req.return_value = _json_response({"positions": []})
    assert broker.reconcile() == []


# ── open_position_count ───────────────────────────────────────────────────────

@patch("execution.capital_broker._req.request")
def test_open_position_count_queries_broker(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"
    mock_req.return_value = _json_response({"positions": [{}, {}]})
    assert broker.open_position_count() == 2


@patch("execution.capital_broker._req.request")
def test_open_position_count_falls_back_on_error(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"
    broker._store.count_open_positions.return_value = 3
    mock_req.side_effect = RuntimeError("network down")
    assert broker.open_position_count() == 3


# ── 401 re-auth ───────────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.post")
@patch("execution.capital_broker._req.request")
def test_reauth_on_401(mock_req, mock_post):
    mock_post.return_value = _login_response("NEW_CST", "NEW_TOKEN")
    broker = _make_broker()
    broker._cst = "OLD_CST"
    broker._security_token = "OLD_TOKEN"

    expired = MagicMock()
    expired.status_code = 401
    expired.raise_for_status = MagicMock()

    ok = _json_response({"positions": []})

    mock_req.side_effect = [expired, ok]

    broker.open_position_count()

    assert broker._cst == "NEW_CST"
    mock_post.assert_called_once()  # _login was called once for re-auth


# ── rate limiting ─────────────────────────────────────────────────────────────

@patch("execution.capital_broker._req.request")
def test_order_rate_limit_enforced(mock_req):
    broker = _make_broker()
    broker._cst = "C"
    broker._security_token = "T"

    mock_req.side_effect = [
        _json_response({"dealReference": "o_1"}),
        _json_response({"dealStatus": "ACCEPTED", "dealId": "d1", "level": 2300.0}),
        _json_response({"dealReference": "o_2"}),
        _json_response({"dealStatus": "ACCEPTED", "dealId": "d2", "level": 2300.0}),
    ]

    broker._last_order_at = time.monotonic()  # simulate a just-placed order
    t0 = time.monotonic()
    broker._submit_order(Signal("buy", 0.05))
    elapsed = time.monotonic() - t0

    assert elapsed >= _ORDER_MIN_GAP - 0.01  # a small tolerance for timing jitter
