import os
import sys
import traceback

# 1. Log immediately to prove Python started
sys.stderr.write("DEBUG: Python process started. Attempting imports...\n")
sys.stderr.flush()

try:
    import threading

    import uvicorn

    from . import mcp_prompts, mcp_resources  # noqa: F401  (registers prompts/resources)
    from .mcp_tools import mcp
    from .web import app

    sys.stderr.write("DEBUG: Imports successful (tools, resources, prompts).\n")
    sys.stderr.flush()
except Exception as e:
    sys.stderr.write(f"CRITICAL IMPORT ERROR: {e}\n")
    traceback.print_exc(file=sys.stderr)
    sys.stderr.flush()
    sys.exit(1)


def run_web_server():
    try:
        auth_port = int(os.environ.get("FPL_AUTH_PORT", "8020"))
        sys.stderr.write(f"DEBUG: Starting Uvicorn on port {auth_port}...\n")
        sys.stderr.flush()
        # log_level="critical" is even quieter than "error"
        uvicorn.run(app, host="127.0.0.1", port=auth_port, log_level="critical")
    except Exception as e:
        sys.stderr.write(f"WEB SERVER ERROR: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


def main():
    try:
        sys.stderr.write("DEBUG: Starting Web Thread...\n")
        sys.stderr.flush()

        # Start FastAPI in a background thread
        t = threading.Thread(target=run_web_server, daemon=True)
        t.start()

        transport = os.environ.get("FPL_MCP_TRANSPORT", "stdio")
        sys.stderr.write(f"DEBUG: Starting MCP Server ({transport})... Waiting for input.\n")
        sys.stderr.flush()

        # This function blocks and waits for Claude to send JSON
        mcp.run(transport=transport)

        sys.stderr.write("DEBUG: MCP Server stopped normally.\n")
        sys.stderr.flush()

    except Exception as e:
        sys.stderr.write(f"MAIN RUNTIME ERROR: {e}\n")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


if __name__ == "__main__":
    main()
