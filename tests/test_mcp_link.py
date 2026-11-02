"""Process-wide MT5 link: one init per process, no shutdown per session, pre-call recovery.

Runs against tests/fake_mt5.py (installed by conftest.py when MetaTrader5 is absent),
so it needs no terminal. Skipped on a box where the real MetaTrader5 is importable.
"""
import asyncio
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager

import pytest

from tests.fake_mt5 import FakeMT5

if not isinstance(sys.modules.get("MetaTrader5"), FakeMT5):
	pytest.skip("real MetaTrader5 installed; these tests need the fake", allow_module_level=True)

import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

import metatrader_mcp.utils as utils
from metatrader_client.exceptions import ConnectionError as MT5ConnectionError
from metatrader_mcp.server import app_lifespan, mcp
from metatrader_mcp.utils import build_dual_app

fake: FakeMT5 = sys.modules["MetaTrader5"]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
	"""Reset the fake link and the module-level shared client between tests."""
	fake.reset()
	monkeypatch.setattr(utils, "_client", None)
	monkeypatch.setattr(utils, "_client_created", False)
	monkeypatch.setenv("LOGIN", "1")
	monkeypatch.setenv("PASSWORD", "pw")
	monkeypatch.setenv("SERVER", "Demo")
	monkeypatch.delenv("MT5_PATH", raising=False)
	monkeypatch.setenv("MT5_PORTABLE", "false")
	# Skip the 2 s connect cooldown; the call itself is still made.
	# (the package re-exports a function of the same name, so go via sys.modules)
	cooldown_mod = sys.modules["metatrader_client.connection._ensure_cooldown"]
	monkeypatch.setattr(cooldown_mod, "_ensure_cooldown", lambda connection: None)
	# Don't let atexit hold a reference to a per-test client.
	monkeypatch.setattr(utils.atexit, "register", lambda fn: None)
	yield


# ── lifespan ──────────────────────────────────────────────────────────────────

def test_lifespan_shares_one_client_and_never_shuts_down():
	async def run():
		a = app_lifespan(mcp)
		b = app_lifespan(mcp)
		ctx_a = await a.__aenter__()
		ctx_b = await b.__aenter__()
		assert ctx_a.client is ctx_b.client
		await a.__aexit__(None, None, None)  # session A closes
		assert fake.calls["shutdown"] == 0
		assert fake.terminal_info() is not None  # link still up for B
		await b.__aexit__(None, None, None)

	asyncio.run(run())
	assert fake.calls["initialize"] == 1
	assert fake.calls["shutdown"] == 0


def test_failed_first_connect_is_not_fatal_and_recovers_on_next_call():
	fake.initialize_ok = False
	client = utils.get_shared_client()
	assert client is not None and fake.terminal_info() is None

	fake.initialize_ok = True
	ran = []
	utils.mt5_tool(lambda: ran.append(1))()
	assert ran == [1]
	assert fake.terminal_info() is not None


# ── pre-call recovery ─────────────────────────────────────────────────────────

def test_lost_link_is_reinitialised_before_the_call():
	utils.get_shared_client()
	fake.shutdown()  # terminal restart / stray shutdown elsewhere
	before = fake.calls["initialize"]

	seen = []
	utils.mt5_tool(lambda: seen.append(fake.terminal_info() is not None))()
	assert seen == [True]
	assert fake.calls["initialize"] == before + 1


def test_healthy_link_is_not_reinitialised():
	utils.get_shared_client()
	before = fake.calls["initialize"]
	utils.mt5_tool(lambda: None)()
	assert fake.calls["initialize"] == before


def test_failed_reinit_raises_not_connected_and_skips_the_tool(monkeypatch):
	client = utils.get_shared_client()
	client._connection.max_retries = 1
	monkeypatch.setattr("time.sleep", lambda s: None)  # initialize() retry backoff
	fake.shutdown()
	fake.initialize_ok = False

	ran = []
	with pytest.raises(MT5ConnectionError, match="Not connected to MetaTrader 5 terminal"):
		utils.mt5_tool(lambda: ran.append(1))()
	assert ran == []


def test_write_is_never_retried_by_recovery():
	"""A tool that fails after it ran (e.g. order sent, then the link dropped) runs once."""
	utils.get_shared_client()
	sends = []

	def place_order():
		sends.append(1)
		fake.shutdown()  # link dies mid-call
		raise MT5ConnectionError("Not connected to MetaTrader 5 terminal")

	with pytest.raises(MT5ConnectionError):
		utils.mt5_tool(place_order)()
	assert sends == [1]


def test_calls_are_serialised_across_threads():
	utils.get_shared_client()
	active, peak = [0], [0]

	def body():
		active[0] += 1
		peak[0] = max(peak[0], active[0])
		time.sleep(0.02)
		active[0] -= 1

	tool = utils.mt5_tool(body)
	threads = [threading.Thread(target=tool) for _ in range(8)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()
	assert peak[0] == 1


def test_every_tool_is_wrapped():
	tools = asyncio.run(mcp.list_tools())
	assert tools, "no tools registered"
	for tool in mcp._tool_manager.list_tools():
		assert hasattr(tool.fn, "__wrapped__"), f"{tool.name} is not wrapped by mt5_tool"
		assert tool.context_kwarg == "ctx", f"{tool.name} lost its Context parameter"


# ── end to end over real SSE / streamable HTTP: the regression from the incident ─

def _free_port() -> int:
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


@pytest.fixture
def base_url():
	"""The bridge's SSE-mode app (SSE on /sse plus streamable HTTP on /mcp) on a free port."""
	# The SDK's StreamableHTTPSessionManager can run() only once; start each server fresh.
	mcp._session_manager = None
	port = _free_port()
	server = uvicorn.Server(uvicorn.Config(build_dual_app(mcp), host="127.0.0.1", port=port, log_level="warning"))
	thread = threading.Thread(target=server.run, daemon=True)
	thread.start()
	deadline = time.time() + 10
	while not server.started:
		if time.time() > deadline:
			raise RuntimeError("test server did not start")
		time.sleep(0.05)
	yield f"http://127.0.0.1:{port}"
	server.should_exit = True
	thread.join(timeout=10)


@asynccontextmanager
async def _session(base: str, transport: str):
	if transport == "sse":
		async with sse_client(f"{base}/sse") as (read, write):
			async with ClientSession(read, write) as session:
				await session.initialize()
				yield session
	else:
		async with streamablehttp_client(f"{base}/mcp") as (read, write, _):
			async with ClientSession(read, write) as session:
				await session.initialize()
				yield session


async def _account_info(session: ClientSession) -> str:
	result = await session.call_tool("get_account_info", {})
	return result.content[0].text if result.content else ""


@pytest.mark.parametrize(
	"b_transport, a_transport, a_cycles",
	[
		("sse", "sse", 1),    # the incident: listener turn closes, Kiro session dies
		("sse", "sse", 10),
		("http", "sse", 10),  # Kiro on /mcp, listener churning on /sse
		("sse", "http", 10),
	],
)
def test_session_b_survives_session_a_disconnecting(base_url, b_transport, a_transport, a_cycles):
	async def run():
		async with _session(base_url, b_transport) as b:
			assert "balance" in await _account_info(b)

			for _ in range(a_cycles):
				async with _session(base_url, a_transport) as a:
					assert "balance" in await _account_info(a)
				# give the server a moment to run A's lifespan exit
				await asyncio.sleep(0.1)

			text = await _account_info(b)
			assert "Not connected" not in text
			assert "balance" in text

	asyncio.run(run())
	assert fake.calls["shutdown"] == 0
	assert fake.calls["initialize"] == 1


def test_sse_and_mcp_list_identical_tools(base_url):
	async def names(transport):
		async with _session(base_url, transport) as s:
			return sorted(t.name for t in (await s.list_tools()).tools)

	async def run():
		return await names("sse"), await names("http")

	sse_tools, http_tools = asyncio.run(run())
	assert sse_tools and sse_tools == http_tools
