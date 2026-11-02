#!/usr/bin/env python3
"""Read-only probe of a live bridge: does a long-lived session survive others closing?

  python scripts/box/probe_bridge.py http://127.0.0.1:18080 [--cycles 10]

Calls only get_account_info. Session B stays open on /sse; session A opens and closes
--cycles times (alternating /sse and /mcp when /mcp exists); then B calls again.
Old bridge: B fails with "Not connected". Fixed bridge: B keeps working, and /sse and
/mcp list the same tools.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


@asynccontextmanager
async def session(base: str, transport: str):
	if transport == "sse":
		async with sse_client(f"{base}/sse") as (r, w):
			async with ClientSession(r, w) as s:
				await s.initialize()
				yield s
	else:
		async with streamablehttp_client(f"{base}/mcp") as (r, w, _):
			async with ClientSession(r, w) as s:
				await s.initialize()
				yield s


async def info(s: ClientSession) -> str:
	r = await s.call_tool("get_account_info", {})
	t = r.content[0].text if r.content else ""
	return ("ERROR " if r.isError else "OK    ") + t.replace("\n", " ")[:100]


async def main(base: str, cycles: int) -> int:
	async with httpx.AsyncClient() as h:
		try:
			code = (await h.post(f"{base}/mcp", json={}, timeout=5,
				headers={"Accept": "application/json, text/event-stream"})).status_code
		except httpx.HTTPError as e:
			print(f"bridge unreachable at {base}: {e!r}")
			print("RESULT: UNREACHABLE (tunnel down or bridge not listening)")
			return 1
	has_mcp = code != 404
	print(f"/mcp: HTTP {code} -> {'present' if has_mcp else 'absent'}")

	failures = 0
	async with session(base, "sse") as b:
		res = await info(b)
		print("B (sse) before :", res)
		for i in range(cycles):
			t = "http" if has_mcp and i % 2 else "sse"
			async with session(base, t) as a:
				print(f"A{i + 1} ({t:4})     :", await info(a))
			await asyncio.sleep(0.5)
		for label in ("after", "again"):
			res = await info(b)
			failures += res.startswith("ERROR")
			print(f"B (sse) {label:6} :", res)

	if has_mcp:
		async with session(base, "sse") as s1, session(base, "http") as s2:
			t1 = sorted(t.name for t in (await s1.list_tools()).tools)
			t2 = sorted(t.name for t in (await s2.list_tools()).tools)
		same = t1 == t2
		failures += not same
		print(f"tools: sse {len(t1)}, mcp {len(t2)}, identical={same}")

	print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures})")
	return failures


if __name__ == "__main__":
	ap = argparse.ArgumentParser()
	ap.add_argument("base", nargs="?", default="http://127.0.0.1:18080")
	ap.add_argument("--cycles", type=int, default=10)
	a = ap.parse_args()
	raise SystemExit(1 if asyncio.run(main(a.base.rstrip("/"), a.cycles)) else 0)
