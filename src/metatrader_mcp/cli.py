import click
import os
from dotenv import load_dotenv
from metatrader_mcp.server import mcp
from metatrader_mcp.utils import resolve_transport_config, run_mcp

@click.command()
@click.option("--login", default=None, type=int, help="MT5 login ID (env: LOGIN)")
@click.option("--password", default=None, help="MT5 password (env: PASSWORD)")
@click.option("--server", default=None, help="MT5 server name (env: SERVER)")
@click.option("--path", default=None, help="Path to MT5 terminal executable (optional, auto-detected if not provided; env: MT5_PATH)")
@click.option("--portable/--no-portable", "portable", default=None, help="Launch/attach the MT5 terminal in portable mode (env: MT5_PORTABLE). Use when running multiple isolated terminals on the same machine.")
@click.option("--transport", default=None, type=click.Choice(["sse", "stdio", "streamable-http"], case_sensitive=False), help="MCP transport type (default: sse, env: MCP_TRANSPORT)")
@click.option("--host", default=None, help="Host to bind for SSE/HTTP transport (default: 0.0.0.0, env: MCP_HOST)")
@click.option("--port", default=None, type=int, help="Port to bind for SSE/HTTP transport (default: 8080, env: MCP_PORT)")
def main(login, password, server, path, portable, transport, host, port):
    """Launch the MetaTrader MCP server.

    Credentials may be supplied via CLI options or via a .env file using the
    documented uppercase variables (LOGIN, PASSWORD, SERVER). CLI options take
    precedence over environment/.env values.
    """
    load_dotenv()
    # CLI options override .env; only set env vars when provided so .env can drive config.
    if login is not None:
        os.environ["LOGIN"] = str(login)
    if password is not None:
        os.environ["PASSWORD"] = password
    if server is not None:
        os.environ["SERVER"] = server
    if path:
        os.environ["MT5_PATH"] = path
    if portable is not None:
        os.environ["MT5_PORTABLE"] = "true" if portable else "false"

    transport, host, port = resolve_transport_config(transport, host, port)
    run_mcp(mcp, transport, host, port)

if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    main()
