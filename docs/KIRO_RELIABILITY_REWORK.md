# Bridge reliability rework for long-lived (Kiro) clients

Status: **items 1–4 implemented on `feature/kiro-reliability-rework`; live on 8080 (terminal #1,
demo) since 2026-11-02 and on 8082 (seagull, live) since 2026-11-05, 8081 not yet.**

8082 on 2026-11-05: venv aligned to demo's `pip freeze`, code deployed, `probe_bridge.py`
PASS (B survived 10 A sessions over `/sse` and `/mcp`; 26 tools on both). 8081 `/mcp` still
404 (old code).

§6 step 2 on 8080 (`scripts/box/probe_bridge.py`, read-only): B on `/sse` stayed `OK` through
10 A sessions alternating `/sse` and `/mcp`; `/sse` and `/mcp` list the same 26 tools. The
terminal-restart case (acceptance criterion 3) is covered by unit tests only, not yet live.
Branched from `feature/expose-get-symbol-info` (carries portable mode, which the box needs).

Findings while implementing:
- `pyproject.toml` had `mcp[cli]>=1.6.0` unpinned, and the code already used 1.8+ APIs
  (`transport_security`, `streamable-http`), so the box is not on the 1.6.0 in `uv.lock`.
  `mcp` 2.x renamed FastMCP and the bridge will not import on it. The live bridge on `:8080`
  reports `serverInfo.version` 1.28.1 (the SDK fills it from the `mcp` package version), so
  the pin is now `mcp[cli]==1.28.1`. Tests pass on 1.28.1 and 1.30.0.
- `MetaTrader5` now has a `win32` marker and `tests/conftest.py` installs
  `tests/fake_mt5.py` when it is missing.
- Bridge start-up: terminal #1 runs `C:\Users\Administrator\start-mcp-server-demo.bat` at
  box boot (contents not yet read).
- §6 step 1 done 2026-11-02 11:5x UTC on the live `:8080` (read-only): B `OK`, A `OK` then
  closed, B `Not connected to MetaTrader 5 terminal` twice. `/mcp` there returns 404.
- `tests/test_mcp_link.py` end-to-end case fails on the old code with exactly the incident
  error and passes on the new one. Six OpenAPI route tests fail before and after (stale
  `stub_lifespan` stubs, unrelated).

## 1. Context

This fork (`/Users/alex/projects/metatrader-mcp-server`, `git@github.com:alexjet/metatrader-mcp-server.git`,
v0.5.2, `mcp` SDK 1.6.0 per `uv.lock`) runs on the Windows MT5 box `10.0.8.72` as the
"legacy bridge": one process per terminal, SSE transport, ports `8080` / `8081` / `8082`
for terminals #1 / #2 / #3 (portable mode for the isolated instances). Box layout and port
table: `/Users/alex/projects/agentic-copy-trader/telegram-listener/docs/WINDOWS_MT5_BOX.md`.

Each terminal also exposes MetaTrader's **native** MCP (`2234x`, streamable HTTP, bearer
key). That is a different server and is NOT affected by anything here.

Clients of the bridge today:

| Client | Repo | Transport | Session lifetime |
|---|---|---|---|
| Listener's Claude agents (`claude -p` per turn), e.g. cypher-forex on `:8080` | `agentic-copy-trader/telegram-listener` (`workspace.<ch>/.mcp.json`, `"type": "sse"`) | SSE | one session per turn: opened and closed every few minutes |
| Listener's `desk` MCP (sizing fields native returns as zero) | `telegram-listener/desk.py` | SSE | per call / short |
| Kiro worker agents (kralow, cypher-gold on terminal #1) | `agentic-copy-trader/trading-agent` | SSE via the stdio adapter `trading-agent/sse_stdio.py` (kiro-cli v2 speaks stdio/streamable HTTP, not SSE) | **one session per channel, held for hours** |

## 2. Problem

Since 2026-11-01 the Kiro agents repeatedly answer "the terminal is disconnected".
The agent's tool result was literally:

```
get_account_info -> "Not connected to MetaTrader 5 terminal"
```

At the same moments:
- native MCP on `:22345` worked (GC wrote stop moves, reconcile read positions, excursion read candles);
- the listener's cypher-forex agent, **same bridge `:8080`**, returned the balance fine;
- restarting the Kiro worker (new SSE sessions) fixed it, until it happened again.

## 3. Root cause (from source, not yet reproduced live)

1. `src/metatrader_mcp/server.py` `app_lifespan`: `init(...)` -> `mt5.initialize()` on entry;
   `finally: client.disconnect()` -> `mt5.shutdown()`
   (`src/metatrader_client/connection/disconnect.py`).
2. With SSE transport the `mcp` SDK enters the lifespan **once per SSE connection**, not
   once per process (`mcp/server/fastmcp/server.py` SSE handler calls `_mcp_server.run()`
   per connection; `mcp/server/lowlevel/server.py` `run()` enters `self.lifespan(self)`).
3. The `MetaTrader5` library holds **one IPC link per process**. Any session's
   `mt5.shutdown()` drops it for every other open session in the bridge.
4. Surviving sessions then fail every tool: `connection/is_connected.py` sees
   `mt5.terminal_info() is None` and `get_terminal_info.py` / history / order code raise
   `ConnectionError("Not connected to MetaTrader 5 terminal")`. Their own
   `connection._connected` is still True, so nothing ever re-initialises them.
5. A NEW session calls `initialize()` again, which is why per-turn clients (Claude) always
   work and also briefly revive the old sessions, until the next close.

So: every short-lived session closing (a listener turn, a `desk` call, one Kiro adapter
reconnecting) kills the terminal link for the long-lived Kiro sessions. Intermittent by
nature; it repeats on every close.

## 4. Decided fix (scope for now)

1. **Initialise once per process, never shut down per session.**
   - Module-level MT5 client created on first lifespan entry and reused by every later
     session. The lifespan must not call `disconnect()` / `mt5.shutdown()`.
   - Shut down only at process exit (atexit / server shutdown), if at all.
2. **Recover a genuinely lost link** (terminal restart, re-login, maintenance reboot).
   - Before each tool call (in `get_client(ctx)` or a decorator), if
     `mt5.terminal_info()` is None, re-initialise once (respect the existing
     `_ensure_cooldown`), then run the call.
   - If re-init fails, return the existing `Not connected` error. **Never auto-retry a
     write after it was sent**: only the pre-call connection check may trigger re-init.
3. **Serve streamable HTTP on `/mcp` alongside SSE on `/sse`, in the same process.**
   - SSE must keep working unchanged for the listener's Claude agents and `desk`.
   - Kiro then connects directly with `"type": "http", "url": "http://10.0.8.72:808x/mcp"`
     and `sse_stdio.py` can be retired (separate change in `trading-agent`).
   - Check how FastMCP 1.6 exposes both apps (`sse_app()` + `streamable_http_app()`
     mounted into one Starlette app, or upgrade `mcp` if 1.6 lacks streamable HTTP
     server support). Pin whatever version is chosen.
4. **Serialise MT5 calls with one process-wide lock.** The library has one IPC link per
   process; concurrent sessions must not interleave calls (e.g. `order_send` with a
   `positions_get`). Verify whether sync FastMCP tools run on a thread pool in the chosen
   SDK version; the lock is needed either way once two sessions are live.

### Optional, only if cheap (not required for this round)
- Bearer-token auth on 808x (env var), matching native MCP. If added, it must be optional
  so existing SSE clients without a token keep working until they are updated.
- A switch to hide the 5 bulk-close tools from the tool list.

### Explicitly out of scope
- **Parallel request handling for throughput.** Calls serialise on the single IPC link
  anyway, and the worker already runs `max_concurrent_turns_per_account: 1`; turn time is
  model inference (3–20 s per turn), not the bridge. Multi-process-per-terminal is
  unverified; do not build on it.
- Moving the agents' trading tools to native MCP. That is the longer-term plan (bridge
  shrinks to a read-only specs service); tracked in
  `/Users/alex/projects/agentic-copy-trader/telegram-listener/docs/KIRO_ACP_POC.md`
  ("Component 3: own Windows specs service vs. keeping the legacy bridge").
- Any change to who computes lot size. Sizing stays with the agent.

## 5. Acceptance criteria

- Two clients connected at once; client A disconnects; client B's next
  `get_account_info` still succeeds. (Fails on the current code: this is the regression test.)
- Same, with A reconnecting repeatedly (10x) while B stays connected.
- Terminal restarted/re-logged while B stays connected: B's next call re-initialises and
  succeeds, or returns `Not connected` once and succeeds on the following call.
- `/sse` and `/mcp` both answer on the same port; tool list identical on both.
- Unit tests for the lifespan (no `shutdown()` on session exit) and the pre-call
  re-init (mock `MetaTrader5`), runnable on macOS without MT5.
- No write tool is ever executed twice by the recovery path.

## 6. How to test against the real box

The Mac reaches the bridge only through the SSM tunnel
`/Users/alex/projects/agentic-copy-trader/telegram-listener/scripts/mt_tunnel.sh`
(`127.0.0.1:18080` -> terminal #1 bridge `:8080`, `127.0.0.1:12345` -> native `:22345`).
The user must start it in their own terminal (agent policy blocks SSM sessions).
Check both ports are open before testing; the 12345 leg has been seen to die silently.

1. Reproduce on the CURRENT bridge first, read-only tools only: open session B and keep
   it; open and close session A; call `get_account_info` on B -> expect `Not connected`.
2. Deploy the fix to terminal #1 (demo) only, re-run the same script -> B keeps working.
3. Only then roll to `8081` / `8082`.

Terminal #1 is demo; terminal #3 is production. Never place orders in these tests.

## 7. Deployment notes

How the bridges are installed (`discover` run 2026-11-02; all 102 `src/**/*.py` in every
folder identical to `1153a68`; Python 3.14.5 everywhere; `.env` `MCP_TRANSPORT=sse`):

| Folder under `C:\Users\Administrator\projects\` | Port | `.env` SERVER | Scheduled task (logon trigger) → `.bat` | `mcp` / uvicorn / starlette / MetaTrader5 |
|---|---|---|---|---|
| `metatrader-mcp-server-demo` | 8080 | VTMarkets-Demo | `Start MT MCP Server - DEV` → `start-mcp-server-demo.bat` | 1.28.1 / 0.51.0 / 1.3.1 / 5.0.5735 |
| `metatrader-mcp-server-prod` | 8081 | **VTMarkets-Live 6** | `Start MT MCP Server PROD` → `start-mcp-server-prod.bat` | 1.28.1 / 0.51.0 / 1.3.1 / 5.0.5735 |
| `metatrader-mcp-server-seagull` | 8082 | **VTMarkets-Live 6** | `Start MT MCP Server Seagull PROD` → `start-mcp-server-seagull.bat` | 1.29.0 / 0.52.2 / 1.6.0 / 5.0.6090 |

- Terminal #1 (8080) is the only demo account; 8081 and 8082 are both live. The broker is
  now VTMarkets, so the account table in `telegram-listener/docs/WINDOWS_MT5_BOX.md`
  (Pepperstone, "#2 is demo") is stale.
- Each `.bat` is `cd <folder>` + `.venv\Scripts\python.exe -m metatrader_mcp.server`, which
  shows up as a venv-launcher python with a base-interpreter child holding the port.
- `Start MT MCP Server on launch` (→ `start-mcp-server.bat`) is disabled since 2026-06-07;
  `start-mcp-server-dev.bat` has no task. That `.bat` holds a plaintext login/password on
  its command line: remove the file and change that password.
- Decided 2026-11-02: align seagull's venv to demo/prod's exact package set, so all three
  bridges run the same versions. `gen_payloads.py align` builds it; it must run while the
  seagull bridge is stopped (loaded `.pyd` files are locked) and refuses otherwise.
- Tests pass on the demo/prod stack (3.14.5, `mcp` 1.28.1, uvicorn 0.51.0, starlette 1.3.1).

- No git on the box. Each folder is a copied source tree with `pip install -e .` into its
  own `.venv`, so an update is: overwrite files under `src/`, restart that bridge. The
  running process keeps the old modules until restarted.
- Delivery is a self-verifying PowerShell payload over SSM Run Command, run by the user:
  `python scripts/box/gen_payloads.py deploy --folder <f> --baseline <rev> --out <dir> FILE...`
  then `bash scripts/box/ssm_run.sh <dir>/box_<f>_deploy.ps1`. The payload writes nothing
  unless every file is at the baseline hash (or already at target), backs each one up to
  `.bak-<stamp>`, re-hashes, and compiles. `..._rollback.ps1` restores the newest backup.
- This change touches only `src/metatrader_mcp/server.py` and `utils.py`, and needs no new
  packages.
- Live clients during the swap: the listener's agents reconnect per turn; the Kiro worker
  reconnects through `sse_stdio.py` on a dropped stream. A brief bridge restart is safe;
  avoid the Saturday maintenance window.
- After `/mcp` is live, follow-up in `trading-agent`: switch each channel's
  `.kiro/settings/mcp.json` to `"type": "http"`, adapt `mcpconfig.validate` (it looks for the
  bridge URL in the adapter's `args` and refuses a raw `"type": "sse"` entry; check how it
  treats a `url` entry), drop `sse_stdio.py` from the Dockerfile and docs.
