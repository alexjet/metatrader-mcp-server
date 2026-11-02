"""In-memory stand-in for the Windows-only ``MetaTrader5`` package.

It models the one property that matters for the bridge's reliability: the library
holds ONE IPC link per process. ``initialize()`` opens it, ``shutdown()`` closes it
for every caller, and ``terminal_info()`` returns None while it is closed.
"""
import types
from collections import namedtuple

TerminalInfo = namedtuple("TerminalInfo", ["connected", "name"])
AccountInfo = namedtuple(
	"AccountInfo",
	[
		"login", "trade_mode", "leverage", "balance", "equity", "profit",
		"margin_level", "margin_free", "currency",
	],
)


class FakeMT5(types.ModuleType):
	def __init__(self):
		super().__init__("MetaTrader5")
		self.reset()

	def reset(self):
		self.linked = False
		self.initialize_ok = True
		self.calls = {"initialize": 0, "shutdown": 0, "login": 0}

	# ── the IPC link ──────────────────────────────────────────────────────────
	def initialize(self, **kwargs):
		self.calls["initialize"] += 1
		self.linked = self.initialize_ok
		return self.linked

	def shutdown(self):
		self.calls["shutdown"] += 1
		self.linked = False
		return True

	def login(self, **kwargs):
		self.calls["login"] += 1
		return self.linked

	def terminal_info(self):
		return TerminalInfo(connected=True, name="fake") if self.linked else None

	def account_info(self):
		if not self.linked:
			return None
		return AccountInfo(
			login=1, trade_mode=1, leverage=100, balance=1000.0, equity=1000.0,
			profit=0.0, margin_level=0.0, margin_free=1000.0, currency="USD",
		)

	def last_error(self):
		return (1, "Success") if self.linked else (-10004, "No IPC connection")

	# Constants (ORDER_TYPE_BUY, TIMEFRAME_H1, ...) referenced by the client code.
	def __getattr__(self, name):
		if name.isupper():
			return 0
		raise AttributeError(name)
