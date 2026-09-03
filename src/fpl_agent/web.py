from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from .headless_auth import establish_session
from .state import store

app = FastAPI()

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='18' fill='%2317211b'/%3E%3Ctext x='32' y='39' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='700' fill='%23f3f5ef'%3EFA%3C/text%3E%3C/svg%3E">
    <title>FPL MCP Login</title>
    <style>
        :root { color: #17211b; background: #e9ebe5; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
        * { box-sizing: border-box; }
        body { display: grid; min-width: 320px; min-height: 100dvh; place-items: center; margin: 0; padding: 24px; background: radial-gradient(circle at 8% 5%, rgba(95,143,109,.13), transparent 34rem), #e9ebe5; }
        .shell { display: grid; grid-template-columns: minmax(260px,.78fr) minmax(360px,1fr); width: min(100%, 880px); overflow: hidden; border: 1px solid rgba(23,33,27,.12); border-radius: 30px; background: #f9f9f5; box-shadow: 0 38px 90px -55px rgba(23,33,27,.7); }
        .context { position: relative; display: flex; min-height: 590px; flex-direction: column; overflow: hidden; padding: 34px; background: #17211b; color: #eff2eb; }
        .context::before, .context::after { position: absolute; right: -75px; width: 250px; height: 150px; border: 1px solid rgba(255,255,255,.09); border-radius: 50%; content: ""; }
        .context::before { top: 118px; } .context::after { bottom: 62px; }
        .brand { position: relative; z-index: 1; display: flex; align-items: center; gap: 11px; }
        .seal { display: grid; width: 43px; height: 43px; place-items: center; border-radius: 14px 14px 14px 6px; background: #eef1ea; color: #17211b; font-size: 12px; font-weight: 800; }
        .brand span:last-child { display: grid; gap: 1px; font-size: 13px; font-weight: 720; }
        .brand small { color: rgba(239,242,235,.45); font-size: 9px; font-weight: 500; letter-spacing: .08em; text-transform: uppercase; }
        .context-copy { position: relative; z-index: 1; margin-top: auto; }
        .eyebrow { color: #abc8b2; font: 700 10px/1 ui-monospace, monospace; letter-spacing: .13em; text-transform: uppercase; }
        .context h1 { max-width: 310px; margin: 15px 0 12px; font-size: clamp(31px,4vw,46px); font-weight: 590; letter-spacing: -.055em; line-height: .98; }
        .context p { max-width: 320px; margin: 0; color: rgba(239,242,235,.58); font-size: 13px; line-height: 1.55; }
        .form-panel { display: flex; min-width: 0; flex-direction: column; justify-content: center; padding: clamp(34px,6vw,64px); }
        .local-pill { display: inline-flex; width: fit-content; align-items: center; gap: 7px; padding: 6px 9px; border: 1px solid rgba(49,91,62,.14); border-radius: 999px; background: #e3ebe1; color: #315b3e; font: 700 9px/1 ui-monospace, monospace; letter-spacing: .09em; text-transform: uppercase; }
        .local-pill::before { width: 6px; height: 6px; border-radius: 50%; background: #5f8f6d; content: ""; }
        h2 { margin: 19px 0 7px; color: #17211b; font-size: 31px; font-weight: 620; letter-spacing: -.045em; }
        .intro { margin: 0 0 27px; color: #68726c; font-size: 13px; line-height: 1.55; }
        label { display: grid; gap: 7px; margin-top: 14px; color: #68726c; font-size: 10px; font-weight: 680; }
        input { width: 100%; min-height: 48px; padding: 12px 14px; border: 1px solid rgba(23,33,27,.13); border-radius: 13px; outline: 0; background: white; color: #17211b; font: inherit; box-shadow: inset 0 1px 0 rgba(23,33,27,.025); }
        input:focus { border-color: #5f8f6d; box-shadow: 0 0 0 3px rgba(95,143,109,.12); }
        button { width: 100%; min-height: 49px; margin-top: 20px; border: 0; border-radius: 999px; background: #17211b; color: #f3f5ef; font-family: inherit; font-size: 13px; font-weight: 720; cursor: pointer; transition: transform .28s ease, background .28s ease; }
        button:hover { background: #243128; transform: translateY(-2px); }
        button:disabled { cursor: wait; opacity: .7; transform: none; }
        .note { display: grid; grid-template-columns: auto 1fr; gap: 9px; margin-top: 24px; padding-top: 20px; border-top: 1px solid rgba(23,33,27,.09); color: #68726c; font-size: 10px; line-height: 1.5; }
        .note strong { display: grid; width: 22px; height: 22px; place-items: center; border-radius: 8px; background: #e3ebe1; color: #315b3e; }
        .loader { display: none; margin-top: 13px; color: #315b3e; font-size: 11px; text-align: center; }
        body.embedded { display: block; min-height: 100%; padding: 0; background: #f9f9f5; }
        body.embedded .shell { grid-template-columns: 1fr; width: 100%; min-height: 100%; border: 0; border-radius: 0; box-shadow: none; }
        body.embedded .context { display: none; }
        body.embedded .form-panel { min-height: 500px; padding: clamp(34px, 8vw, 72px); }
        @media (max-width: 720px) { body { padding: 12px; } .shell { grid-template-columns: 1fr; border-radius: 24px; } .context { min-height: 225px; padding: 24px; } .context-copy { margin-top: 54px; } .context h1 { max-width: 420px; font-size: 34px; } .context p { display: none; } .form-panel { padding: 30px 24px 34px; } }
        @media (prefers-reduced-motion: reduce) { button { transition: none; } }
    </style>
    <script>
        function showLoader() {
            document.querySelector('.loader').style.display = 'block';
            document.querySelector('button').disabled = true;
            document.querySelector('button').innerText = 'Authenticating...';
        }
    </script>
</head>
<body>
    <main class="shell">
        <section class="context">
            <div class="brand"><span class="seal">FA</span><span>FPLAgent<small>Decision workspace</small></span></div>
            <div class="context-copy">
                <span class="eyebrow">Season connection</span>
                <h1>Keep the pitch in step with FPL.</h1>
                <p>Connect once, then let the local application read your current squad before each deadline.</p>
            </div>
        </section>
        <section class="form-panel">
            <span class="local-pill">127.0.0.1 · local only</span>
            <h2>Connect your team</h2>
            <p class="intro">Use the email and password for your Fantasy Premier League account.</p>
            <form action="/auth/submit/{request_id}" method="post" onsubmit="showLoader()">
                <label>Email address<input type="email" name="email" autocomplete="username" required></label>
                <label>Password<input type="password" name="password" autocomplete="current-password" required></label>
                <button type="submit">Connect securely</button>
            </form>
            <div class="loader">Authenticating with FPL in a private browser…</div>
            <div class="note"><strong>✓</strong><span>Your credentials are processed by this local MCP process only. FPLAgent receives the resulting squad snapshot, never your password.</span></div>
        </section>
    </main>
</body>
</html>
"""


def _result_page(title: str, message: str, *, success: bool) -> str:
    tone = "#315b3e" if success else "#93453d"
    mark = "✓" if success else "!"
    return f"""
        <!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title></head>
        <body style="display:grid;min-height:100dvh;place-items:center;margin:0;padding:24px;background:#e9ebe5;color:#17211b;font-family:Inter,system-ui,sans-serif">
          <main style="width:min(100%,520px);padding:44px;border:1px solid rgba(23,33,27,.12);border-radius:28px;background:#f9f9f5;box-shadow:0 38px 90px -55px rgba(23,33,27,.7)">
            <span style="display:grid;width:54px;height:54px;place-items:center;border-radius:18px 18px 18px 7px;background:#e3ebe1;color:{tone};font-size:25px;font-weight:800">{mark}</span>
            <small style="display:block;margin-top:27px;color:{tone};font:700 10px ui-monospace,monospace;letter-spacing:.13em;text-transform:uppercase">FPLAgent · local connection</small>
            <h1 style="margin:12px 0 9px;font-size:38px;line-height:1;letter-spacing:-.05em">{escape(title)}</h1>
            <p style="margin:0;color:#68726c;font-size:14px;line-height:1.55">{escape(message)}</p>
          </main>
        </body></html>
    """


@app.get("/", response_class=HTMLResponse)
async def auth_service_home():
    return _result_page(
        "Local login service is ready.",
        "Open FPLAgent. The application will detect this service and present a private login request automatically.",
        success=True,
    )


@app.get("/login/{request_id}", response_class=HTMLResponse)
async def login_page(request_id: str, request: Request):
    store.create_login_request(request_id)
    page = LOGIN_HTML.replace("{request_id}", request_id)
    if request.query_params.get("embed") == "1":
        page = page.replace("<body>", '<body class="embedded">', 1)
    return page


@app.post("/auth/submit/{request_id}")
async def submit_login(request_id: str, email: str = Form(...), password: str = Form(...)):
    try:
        # Entry ID is fetched automatically from /me inside establish_session.
        session_id, failure = await establish_session(email, password, request_id)

        if session_id:
            return HTMLResponse(
                _result_page(
                    "Your team is connected.",
                    "Return to FPLAgent. The decision workspace will detect this session and sync your current squad.",
                    success=True,
                )
            )
        return HTMLResponse(
            _result_page(
                "Connection failed.",
                failure or "Could not capture an authenticated FPL session.",
                success=False,
            )
        )

    except Exception as e:
        store.set_login_failure(request_id, str(e))
        return HTMLResponse(
            _result_page("Connection error.", str(e), success=False)
        )
