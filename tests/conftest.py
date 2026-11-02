import sys

# MetaTrader5 ships Windows wheels only. Off Windows, install an in-memory fake
# before anything imports metatrader_client, so unit tests run on macOS/Linux.
try:
	import MetaTrader5  # noqa: F401
except ImportError:
	from tests.fake_mt5 import FakeMT5

	sys.modules["MetaTrader5"] = FakeMT5()
