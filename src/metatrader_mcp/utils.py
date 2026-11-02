import atexit
import functools
import logging
import os
import threading
from typing import Any, Callable, Optional, Union
from metatrader_client import client
from metatrader_client.exceptions import ConnectionError as MT5ConnectionError

logger = logging.getLogger("metatrader_mcp")

# ────────────────────────────────────────────────────────────────────────────────
# Process-wide MT5 link
#
# The MetaTrader5 library holds ONE IPC link per process, and the MCP SDK enters
# the server lifespan once per client session (SSE connection / streamable HTTP
# session), not once per process. So the client is created once, shared by every
# session, and never shut down when a session ends: one session's mt5.shutdown()
# used to drop the link for every other open session.
# ────────────────────────────────────────────────────────────────────────────────
_client: Optional[client.MT5Client] = None
_client_created = False
# Serialises every MT5 call (and re-init) across sessions. Re-entrant so a tool
# that calls get_shared_client() while holding it does not deadlock.
_mt5_lock = threading.RLock()

NOT_CONNECTED_MESSAGE = "Not connected to MetaTrader 5 terminal"


def env_flag(name: str, default: bool = False) -> bool:
	"""Parse a boolean-ish environment variable.

	Accepts 1/true/yes/on (case-insensitive) as True and 0/false/no/off as False.
	Returns ``default`` when the variable is unset or empty.
	"""
	val = os.getenv(name)
	if val is None or val.strip() == "":
		return default
	return val.strip().lower() in ("1", "true", "yes", "on")


def resolve_transport_config(transport=None, host=None, port=None):
	"""Resolve transport config: CLI flag > env var > default."""
	transport = transport or os.getenv("MCP_TRANSPORT", "sse")
	host = host or os.getenv("MCP_HOST", "0.0.0.0")
	port = port if port is not None else int(os.getenv("MCP_PORT", "8080"))
	return transport, host, port


def run_mcp(mcp, transport, host, port):
	"""Run the MCP server with the resolved transport config."""
	if transport == "stdio":
		mcp.run(transport="stdio")
	else:
		mcp.settings.host = host
		mcp.settings.port = port
		# Disable DNS rebinding protection when binding to all interfaces,
		# since the server is intended to be accessed remotely.
		if host == "0.0.0.0":
			mcp.settings.transport_security.enable_dns_rebinding_protection = False
		if transport == "sse":
			# SSE mode serves streamable HTTP on /mcp as well, so long-lived clients
			# (Kiro) can connect directly while SSE clients keep using /sse unchanged.
			import uvicorn

			uvicorn.run(
				build_dual_app(mcp),
				host=host,
				port=port,
				log_level=mcp.settings.log_level.lower(),
			)
		else:
			mcp.run(transport=transport)


def build_dual_app(mcp):
	"""One Starlette app serving SSE (/sse + /messages/) and streamable HTTP (/mcp).

	Both transports dispatch to the same FastMCP instance, so the tool list is
	identical. The streamable HTTP session manager must be running for /mcp to
	answer, so its lifespan becomes the combined app's lifespan.
	"""
	from starlette.applications import Starlette

	http_app = mcp.streamable_http_app()
	sse_app = mcp.sse_app()
	return Starlette(
		debug=mcp.settings.debug,
		routes=[*http_app.routes, *sse_app.routes],
		lifespan=http_app.router.lifespan_context,
	)

def init(
	login: Optional[Union[str, int]],
	password: Optional[str],
	server: Optional[str],
	path: Optional[str] = None,
	portable: bool = False,
) -> Optional[client.MT5Client]:
	"""
	Initialize MT5Client

	Args:
		login (Optional[Union[str, int]]): Login ID
		password (Optional[str]): Password
		server (Optional[str]): Server name
		path (Optional[str]): Path to MT5 terminal executable (default: None for auto-detect)
		portable (bool): Whether to launch/attach the MT5 terminal in portable mode
			(default: False). Required when running multiple isolated terminals on the
			same machine so each keeps its own data folder.

	Returns:
		Optional[client.MT5Client]: MT5Client instance if all parameters are provided, None otherwise
	"""

	mt5_client = build_client(login, password, server, path, portable)
	if mt5_client is not None:
		mt5_client.connect()
	return mt5_client


def build_client(
	login: Optional[Union[str, int]],
	password: Optional[str],
	server: Optional[str],
	path: Optional[str] = None,
	portable: bool = False,
) -> Optional[client.MT5Client]:
	"""Build an MT5Client from credentials without connecting. None if any credential is missing."""
	if not (login and password and server):
		return None

	config = {
		"login": int(login),
		"password": password,
		"server": server,
	}
	if path:
		config["path"] = path
	if portable:
		config["portable"] = True
	return client.MT5Client(config=config)


def get_shared_client() -> Optional[client.MT5Client]:
	"""Return the process-wide MT5 client, creating and connecting it on first use.

	Credentials come from the environment (LOGIN / PASSWORD / SERVER / MT5_PATH /
	MT5_PORTABLE). A failed first connect is logged, not raised: the client is
	kept and the pre-call link check retries the connect on the next tool call,
	so a terminal that is down at bridge start-up no longer kills every session.
	"""
	global _client, _client_created
	with _mt5_lock:
		if _client_created:
			return _client
		_client = build_client(
			os.getenv("LOGIN", os.getenv("login")),
			os.getenv("PASSWORD", os.getenv("password")),
			os.getenv("SERVER", os.getenv("server")),
			os.getenv("MT5_PATH", os.getenv("mt5_path")),
			env_flag("MT5_PORTABLE"),
		)
		_client_created = True
		if _client is None:
			logger.warning("MT5 credentials missing (LOGIN/PASSWORD/SERVER); tools will not work")
			return None
		try:
			_client.connect()
		except Exception as e:
			logger.warning("Initial MT5 connect failed, will retry on the next tool call: %s", e)
		atexit.register(shutdown_shared_client)
		return _client


def shutdown_shared_client() -> None:
	"""Shut the process-wide link down. Only for process exit, never per session."""
	with _mt5_lock:
		if _client is not None:
			try:
				_client.disconnect()
			except Exception as e:
				logger.debug("MT5 shutdown at exit failed: %s", e)


def ensure_link(mt5_client: Optional[client.MT5Client]) -> None:
	"""Re-initialise the MT5 link once if it is gone; raise ConnectionError if that fails.

	The trigger is ``mt5.terminal_info() is None`` (IPC link lost: terminal restart,
	re-login, a stray shutdown). It is deliberately NOT ``is_connected()``, which is
	also False while the terminal is up but has lost the broker; re-initialising does
	not fix that, and the tool's own error reports it.

	Runs only BEFORE a tool executes, so a write is never re-sent by recovery.
	Caller must hold ``_mt5_lock``.
	"""
	if mt5_client is None:
		return
	import MetaTrader5 as mt5

	if mt5.terminal_info() is not None:
		return
	logger.warning("MT5 link lost (terminal_info() is None); re-initialising")
	try:
		# connect() applies the existing cooldown, then initialize() + login().
		mt5_client.connect()
	except Exception as e:
		logger.error("MT5 re-initialisation failed: %s", e)
		raise MT5ConnectionError(NOT_CONNECTED_MESSAGE) from e
	logger.info("MT5 link re-initialised")


def mt5_tool(fn: Callable) -> Callable:
	"""Wrap a sync MCP tool: hold the process-wide MT5 lock, check the link, then run once.

	``functools.wraps`` keeps the signature, so FastMCP still sees the original
	parameters (including the Context one) when it builds the tool schema.
	"""
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		with _mt5_lock:
			ensure_link(get_shared_client())
			return fn(*args, **kwargs)
	return wrapper


def get_client(ctx: Any) -> Optional[client.MT5Client]:
	return ctx.request_context.lifespan_context.client