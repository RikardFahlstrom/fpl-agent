"""Authentication and session tools: starting a login and activating it."""

from ...sessions import sessions
import uuid
from .core import BASE_URL, mcp, set_active_session
from .core import _get_client, _mapping_contract, _records_contract


@mcp.tool()
async def begin_web_login() -> dict:
    """Start the local browser login flow and return a structured URL for an app client."""
    request_id = str(uuid.uuid4())
    sessions.create_login_request(request_id)
    return {
        "status": "pending",
        "request_id": request_id,
        "login_url": f"{BASE_URL}/login/{request_id}",
    }


@mcp.tool()
async def poll_web_login(request_id: str) -> dict:
    """Poll a structured browser-login request and activate the authenticated session."""
    request = sessions.pending_logins.get(request_id)
    if not request:
        return {"status": "failed", "error": "invalid_request_id"}
    if request.status == "pending":
        return {"status": "pending", "request_id": request_id}
    if request.status == "failed":
        return {"status": "failed", "error": request.error or "login_failed"}
    set_active_session(request.session_id)
    client = _get_client()
    player = (client.user_info or {}).get("player", {}) if client else {}
    return {
        "status": "connected",
        "request_id": request_id,
        "entry_id": player.get("entry"),
        "first_name": player.get("first_name"),
        "last_name": player.get("last_name"),
    }


@mcp.tool()
async def get_auth_status() -> dict:
    """Return lightweight local authentication state without making an FPL request."""
    client = _get_client()
    if not client:
        return {"status": "not_authenticated", "entry_id": None}
    player = (client.user_info or {}).get("player", {})
    return {
        "status": "connected",
        "entry_id": player.get("entry"),
        "first_name": player.get("first_name"),
        "last_name": player.get("last_name"),
    }


@mcp.tool()
async def get_authenticated_schema_diagnostics() -> dict:
    """Return redacted schemas for authenticated FPL endpoints, never their values."""
    client = _get_client()
    if not client:
        return {"status": "not_authenticated"}
    entry_id = sessions.get_user_entry_id(client)
    if not entry_id:
        return {"status": "entry_unavailable"}

    me = client.user_info or await client.get_me()
    my_team = await client.get_my_team(entry_id)
    transfers = my_team.get("transfers") if isinstance(my_team, dict) else None
    return {
        "status": "connected",
        "redacted": True,
        "endpoints": {
            "/api/me/": {
                "response": _mapping_contract(me),
                "player": _mapping_contract(me.get("player") if isinstance(me, dict) else None),
            },
            "/api/my-team/{entry_id}/": {
                "response": _mapping_contract(my_team),
                "picks": _records_contract(
                    my_team.get("picks") if isinstance(my_team, dict) else None
                ),
                "transfers": _mapping_contract(transfers),
                "chips": _records_contract(
                    my_team.get("chips") if isinstance(my_team, dict) else None
                ),
            },
        },
    }


@mcp.tool()
async def login_to_fpl() -> str:
    """
    Step 1: Generates a secure login link. 
    Call this when the user wants to log in or when other tools return 'Authentication required'.
    After successful login, your session will be automatically activated.
    """
    request_id = str(uuid.uuid4())
    sessions.create_login_request(request_id)
    
    return (
        f"Please authenticate here: {BASE_URL}/login/{request_id}\n\n"
        f"INSTRUCTION: Wait for the user to confirm they have finished logging in. "
        f"Then, immediately call 'check_login_status' with ID: {request_id}"
    )


@mcp.tool()
async def check_login_status(request_id: str) -> str:
    """
    Step 2: Checks if the user has completed the web login. 
    On success, automatically activates your session for all future tool calls.
    """
    
    req = sessions.pending_logins.get(request_id)
    if not req:
        return "Error: Invalid Request ID"
    
    if req.status == "pending":
        return "Login pending. Waiting for user..."
    if req.status == "failed":
        return f"Login failed: {req.error}"
    
    # Store the session ID globally
    set_active_session(req.session_id)
    
    client = _get_client()
    if client and client.user_info:
        user_entry = client.user_info.get('player', {}).get('entry')
        return (
            f"✅ Authentication Successful!\n"
            f"Your session is now active. You can now use all FPL tools without providing a session ID.\n"
            f"Your FPL entry has been loaded automatically."
        )
    
    return "✅ Authentication Successful! Your session is now active."
