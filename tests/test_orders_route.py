import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "src")))

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from metatrader_openapi.main import app
import metatrader_openapi.main as main_module


class DummyOrder:
    def __init__(self):
        self.place_market_order = MagicMock()


class DummyClient:
    def __init__(self):
        self.order = DummyOrder()

    def disconnect(self):
        pass


@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch):
    client = DummyClient()
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "init", lambda *a, **kw: client)
    yield client


def test_place_market_order_api_with_sl_tp(stub_lifespan):
    stub_lifespan.order.place_market_order.return_value = {
        "error": False,
        "message": "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 12345)",
        "data": None,
    }

    with TestClient(app) as api_client:
        response = api_client.post("/api/v1/order/market", json={
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "BUY",
            "stop_loss": 1.0990,
            "take_profit": 1.1010,
        })

    assert response.status_code == 200, response.text
    assert response.json()["error"] is False
    assert "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 12345)" in response.json()["message"]
    stub_lifespan.order.place_market_order.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="BUY",
        stop_loss=1.0990,
        take_profit=1.1010,
    )


def test_place_market_order_api_no_sl_tp(stub_lifespan):
    stub_lifespan.order.place_market_order.return_value = {
        "error": False,
        "message": "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 67890) (no SL/TP)",
        "data": None,
    }

    with TestClient(app) as api_client:
        response = api_client.post("/api/v1/order/market", json={
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "BUY",
        })

    assert response.status_code == 200, response.text
    assert response.json()["error"] is False
    assert "Mocked BUY EURUSD 0.01 LOT at 1.1000 success (Position ID: 67890) (no SL/TP)" in response.json()["message"]
    stub_lifespan.order.place_market_order.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="BUY",
        stop_loss=0.0,
        take_profit=0.0,
    )


def test_place_market_order_api_invalid_type(stub_lifespan):
    stub_lifespan.order.place_market_order.return_value = {
        "error": True,
        "message": "Invalid type, should be BUY or SELL.",
        "data": None,
    }

    with TestClient(app) as api_client:
        response = api_client.post("/api/v1/order/market", json={
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": "INVALID_TYPE",
            "stop_loss": 1.0990,
            "take_profit": 1.1010,
        })

    assert response.status_code == 200, response.text
    assert response.json()["error"] is True
    assert "Invalid type, should be BUY or SELL." in response.json()["message"]
    stub_lifespan.order.place_market_order.assert_called_once_with(
        symbol="EURUSD",
        volume=0.01,
        type="INVALID_TYPE",
        stop_loss=1.0990,
        take_profit=1.1010,
    )


def test_place_market_order_api_missing_fields(stub_lifespan):
    with TestClient(app) as api_client:
        response = api_client.post("/api/v1/order/market", json={"symbol": "EURUSD"})

    assert response.status_code == 422
