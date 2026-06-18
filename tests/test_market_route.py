import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "src")))

import pytest
from fastapi.testclient import TestClient
from datetime import datetime
import pandas as pd
from unittest.mock import MagicMock

from metatrader_openapi.main import app
import metatrader_openapi.main as main_module
from metatrader_client.exceptions import ConnectionError as MT5ConnectionError


class DummyMarket:
    def __init__(self):
        self.get_symbol_info = MagicMock()
        self.get_candles_by_date = MagicMock()


class DummyClient:
    def __init__(self):
        self.market = DummyMarket()

    def disconnect(self):
        pass


@pytest.fixture
def dummy_client(monkeypatch):
    client = DummyClient()
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "init", lambda *a, **kw: client)
    return client


def test_get_symbol_info_success(dummy_client):
    symbol_name = "EURUSD"
    sample_symbol_info = {
        "name": symbol_name,
        "description": "Euro vs US Dollar",
        "currency_base": "EUR",
        "currency_profit": "USD",
        "digits": 5,
        "spread": 10,
        "volume_min": 0.01,
        "volume_max": 1000.0,
        "trade_mode_description": "Full access",
        "visible": True,
    }
    dummy_client.market.get_symbol_info.return_value = sample_symbol_info

    with TestClient(app) as api_client:
        response = api_client.get(f"/api/v1/market/symbol/info/{symbol_name}")

    assert response.status_code == 200, response.text
    assert response.json() == sample_symbol_info
    dummy_client.market.get_symbol_info.assert_called_once_with(symbol_name=symbol_name)


def test_get_symbol_info_not_found(dummy_client):
    symbol_name = "UNKNOWN_SYMBOL"
    dummy_client.market.get_symbol_info.return_value = None

    with TestClient(app) as api_client:
        response = api_client.get(f"/api/v1/market/symbol/info/{symbol_name}")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Symbol {symbol_name} not found or no info available."}
    dummy_client.market.get_symbol_info.assert_called_once_with(symbol_name=symbol_name)


def test_get_symbol_info_connection_error(dummy_client):
    symbol_name = "EURUSD"
    dummy_client.market.get_symbol_info.side_effect = MT5ConnectionError("Test connection error")

    with TestClient(app) as api_client:
        response = api_client.get(f"/api/v1/market/symbol/info/{symbol_name}")

    assert response.status_code == 503, response.text
    assert "Test connection error" in response.json()["detail"]
    dummy_client.market.get_symbol_info.assert_called_once_with(symbol_name=symbol_name)


def test_get_candles_by_date_success(dummy_client):
    symbol_name = "EURUSD"
    timeframe = "H1"
    date_from_str = "2023-01-01T00:00:00"
    date_to_str = "2023-01-01T05:00:00"
    date_from_dt = datetime.fromisoformat(date_from_str)
    date_to_dt = datetime.fromisoformat(date_to_str)

    sample_candles_df_data = [
        {"time": datetime(2023, 1, 1, 0, 0), "open": 1.1, "high": 1.105, "low": 1.095, "close": 1.102, "tick_volume": 100, "spread": 5, "real_volume": 1000},
        {"time": datetime(2023, 1, 1, 1, 0), "open": 1.102, "high": 1.108, "low": 1.100, "close": 1.107, "tick_volume": 120, "spread": 5, "real_volume": 1200},
    ]
    dummy_client.market.get_candles_by_date.return_value = pd.DataFrame(sample_candles_df_data)

    expected_response_data = [
        {"time": "2023-01-01T00:00:00", "open": 1.1, "high": 1.105, "low": 1.095, "close": 1.102, "tick_volume": 100, "spread": 5, "real_volume": 1000},
        {"time": "2023-01-01T01:00:00", "open": 1.102, "high": 1.108, "low": 1.100, "close": 1.107, "tick_volume": 120, "spread": 5, "real_volume": 1200},
    ]

    with TestClient(app) as api_client:
        response = api_client.get(
            f"/api/v1/market/candles/date?symbol_name={symbol_name}&timeframe={timeframe}&date_from={date_from_str}&date_to={date_to_str}"
        )

    assert response.status_code == 200, response.text
    assert response.json() == expected_response_data
    dummy_client.market.get_candles_by_date.assert_called_once_with(
        symbol_name=symbol_name,
        timeframe=timeframe,
        from_date=date_from_dt,
        to_date=date_to_dt,
    )


def test_get_candles_by_date_no_data(dummy_client):
    symbol_name = "EURUSD"
    timeframe = "M5"
    date_from_str = "2023-02-01T00:00:00"
    date_to_str = "2023-02-01T01:00:00"
    date_from_dt = datetime.fromisoformat(date_from_str)
    date_to_dt = datetime.fromisoformat(date_to_str)

    dummy_client.market.get_candles_by_date.return_value = pd.DataFrame()

    with TestClient(app) as api_client:
        response = api_client.get(
            f"/api/v1/market/candles/date?symbol_name={symbol_name}&timeframe={timeframe}&date_from={date_from_str}&date_to={date_to_str}"
        )

    assert response.status_code == 200, response.text
    assert response.json() == []
    dummy_client.market.get_candles_by_date.assert_called_once_with(
        symbol_name=symbol_name,
        timeframe=timeframe,
        from_date=date_from_dt,
        to_date=date_to_dt,
    )


def test_get_candles_by_date_value_error(dummy_client):
    symbol_name = "EURUSD"
    timeframe = "H1"
    date_from_str = "2023-01-01T00:00:00"
    date_to_str = "2022-01-01T00:00:00"
    date_from_dt = datetime.fromisoformat(date_from_str)
    date_to_dt = datetime.fromisoformat(date_to_str)

    dummy_client.market.get_candles_by_date.side_effect = ValueError("Test ValueError: date_to cannot be before date_from")

    with TestClient(app) as api_client:
        response = api_client.get(
            f"/api/v1/market/candles/date?symbol_name={symbol_name}&timeframe={timeframe}&date_from={date_from_str}&date_to={date_to_str}"
        )

    assert response.status_code == 400, response.text
    assert "Test ValueError: date_to cannot be before date_from" in response.json()["detail"]
    dummy_client.market.get_candles_by_date.assert_called_once_with(
        symbol_name=symbol_name,
        timeframe=timeframe,
        from_date=date_from_dt,
        to_date=date_to_dt,
    )


def test_get_candles_by_date_connection_error(dummy_client):
    symbol_name = "EURUSD"
    timeframe = "H1"
    date_from_str = "2023-01-01T00:00:00"
    date_to_str = "2023-01-02T00:00:00"
    date_from_dt = datetime.fromisoformat(date_from_str)
    date_to_dt = datetime.fromisoformat(date_to_str)

    dummy_client.market.get_candles_by_date.side_effect = MT5ConnectionError("Test connection error for candles")

    with TestClient(app) as api_client:
        response = api_client.get(
            f"/api/v1/market/candles/date?symbol_name={symbol_name}&timeframe={timeframe}&date_from={date_from_str}&date_to={date_to_str}"
        )

    assert response.status_code == 503, response.text
    assert "Test connection error for candles" in response.json()["detail"]
    dummy_client.market.get_candles_by_date.assert_called_once_with(
        symbol_name=symbol_name,
        timeframe=timeframe,
        from_date=date_from_dt,
        to_date=date_to_dt,
    )
