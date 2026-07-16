# pylint: disable=import-error
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.middleware.cors import CORSMiddleware
from .config import Settings
from .routers import router as api_router
import os
import argparse
import uvicorn
from dotenv import load_dotenv
from metatrader_mcp.utils import init, env_flag
from contextlib import asynccontextmanager

# Instantiate settings
settings = Settings()

# Define a lifespan handler for MT5 client lifecycle
@asynccontextmanager
async def lifespan(app):
    # Load environment and support uppercase or lowercase vars
    load_dotenv()
    login = os.getenv("LOGIN", os.getenv("login"))
    password = os.getenv("PASSWORD", os.getenv("password"))
    server = os.getenv("SERVER", os.getenv("server"))
    path = os.getenv("MT5_PATH", os.getenv("mt5_path"))
    portable = env_flag("MT5_PORTABLE")
    client = init(login, password, server, path, portable)
    app.state.client = client
    yield
    if client:
        client.disconnect()

# Initialize FastAPI app with OpenAPI metadata and lifespan
app = FastAPI(
    title=settings.title,
    version=settings.version,
    openapi_url=settings.openapi_url,
    docs_url=settings.docs_url,
    redoc_url=settings.redoc_url,
    lifespan=lifespan,
)

# Enable CORS for Open WebUI and other clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
def strip_prefix(route: APIRoute) -> str:
    op_id = route.name
    prefix = "api_v1_"
    if op_id.startswith(prefix):
        op_id = op_id[len(prefix):]
    return op_id
app.include_router(api_router, prefix="/api/v1", generate_unique_id_function=strip_prefix)

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="MetaTrader OpenAPI server")
    parser.add_argument("--login", default=None, help="MT5 login (env: LOGIN)")
    parser.add_argument("--password", default=None, help="MT5 password (env: PASSWORD)")
    parser.add_argument("--server", default=None, help="MT5 server address (env: SERVER)")
    parser.add_argument("--path", default=None, help="Path to MT5 terminal executable (optional, auto-detected if not provided; env: MT5_PATH)")
    parser.add_argument("--portable", dest="portable", action="store_true", default=None, help="Launch/attach the MT5 terminal in portable mode (env: MT5_PORTABLE)")
    parser.add_argument("--no-portable", dest="portable", action="store_false", help="Disable portable mode")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    # CLI options override .env; set both uppercase and lowercase env vars when provided
    if args.login:
        os.environ["LOGIN"] = args.login
        os.environ["login"] = args.login
    if args.password:
        os.environ["PASSWORD"] = args.password
        os.environ["password"] = args.password
    if args.server:
        os.environ["SERVER"] = args.server
        os.environ["server"] = args.server
    if args.path:
        os.environ["MT5_PATH"] = args.path
    if args.portable is not None:
        os.environ["MT5_PORTABLE"] = "true" if args.portable else "false"

    uvicorn.run(
        "metatrader_openapi.main:app",
        host=args.host,
        port=args.port,
        reload=True,
    )

if __name__ == "__main__":
    main()
