import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "src")))

import pytest
from fastapi.testclient import TestClient
from metatrader_openapi.main import app
import metatrader_openapi.main as main_module

import pandas as pd

class DummyHistory:
    def get_deals_as_dataframe(self, from_date=None, to_date=None, group=None):
        return pd.DataFrame([{"ticket": 1, "symbol": "EURUSD"}])

    def get_orders_as_dataframe(self, from_date=None, to_date=None, group=None):
        return pd.DataFrame([{"ticket": 2, "symbol": "USDJPY"}])

class DummyClient:
    history = DummyHistory()
    def disconnect(self):
        pass

@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch):
    # Stub dotenv and MT5 client init
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "init", lambda login, password, server, path=None: DummyClient())
    yield


def test_history_deals():
    with TestClient(app) as client:
        response = client.get("/api/v1/history/deals")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert data == [{"ticket": 1, "symbol": "EURUSD"}]


def test_history_orders():
    with TestClient(app) as client:
        response = client.get("/api/v1/history/orders")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert data == [{"ticket": 2, "symbol": "USDJPY"}]
