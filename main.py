import os
import requests
import webbrowser
import threading
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client
from dotenv import load_dotenv
from urllib.parse import urlencode

load_dotenv()

app = FastAPI()

# =========================
# ENV
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZOHO_ACCOUNTS_URL = "https://accounts.zoho.in"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# TEMP SESSION STORE
# =========================
user_sessions = {}

# =========================
# HOME
# =========================
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h1>Welcome</h1>
    <a href="/signin">Sign In</a><br><br>
    <a href="/signup">Sign Up</a>
    """

# =========================
# SIGNUP
# =========================
@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    return """
    <h2>Sign Up</h2>
    <form action="/signup" method="post">
        Email: <input name="email"><br><br>
        Password: <input name="password" type="password"><br><br>
        <button type="submit">Sign Up</button>
    </form>
    """

@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...)):
    res = supabase.auth.sign_up({
        "email": email,
        "password": password
    })

    return RedirectResponse(url="/signin", status_code=302)

# =========================
# SIGNIN
# =========================
@app.get("/signin", response_class=HTMLResponse)
def signin_page():
    return """
    <h2>Sign In</h2>
    <form action="/signin" method="post">
        Email: <input name="email"><br><br>
        Password: <input name="password" type="password"><br><br>
        <button type="submit">Sign In</button>
    </form>
    """

@app.post("/signin")
def signin(email: str = Form(...), password: str = Form(...)):
    res = supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })

    user_id = res.user.id
    return RedirectResponse(url=f"/zoho-setup?user_id={user_id}", status_code=302)

# =========================
# ZOHO SETUP
# =========================
@app.get("/zoho-setup", response_class=HTMLResponse)
def zoho_setup(user_id: str):
    return f"""
    <h2>Zoho Setup</h2>
    <form action="/zoho-setup" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        Client ID: <input name="client_id"><br><br>
        Client Secret: <input name="client_secret"><br><br>
        Redirect URI: <input name="redirect_uri" value="http://localhost:8000/callback"><br><br>
        <button type="submit">Connect Zoho</button>
    </form>
    """

@app.post("/zoho-setup")
def zoho_setup_post(
    user_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    redirect_uri: str = Form(...)
):
    user_sessions[user_id] = {
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "redirect_uri": redirect_uri.strip()
    }

    # ✅ FIXED SCOPES
    params = {
        "scope": "ZohoCRM.settings.modules.ALL,ZohoCRM.settings.fields.ALL,ZohoCRM.modules.ALL",
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": redirect_uri,
        "state": user_id
    }

    auth_url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/auth?{urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=302)

# =========================
# CALLBACK
# =========================
@app.get("/callback")
def callback(code: str, state: str):
    user_id = state
    creds = user_sessions.get(user_id)

    token_url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token"

    payload = {
        "grant_type": "authorization_code",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "redirect_uri": creds["redirect_uri"],
        "code": code
    }

    response = requests.post(token_url, params=payload)
    data = response.json()

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token")

    supabase.table("zoho_tokens").upsert({
        "user_id": user_id,
        "zoho_access_token": access_token,
        "zoho_refresh_token": refresh_token
    }, on_conflict="user_id").execute()

    return RedirectResponse(url=f"/dashboard?user_id={user_id}", status_code=302)

# =========================
# 🔥 GET MODULES
# =========================
def get_zoho_modules(access_token):
    url = "https://www.zohoapis.in/crm/v2/settings/modules"

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}"
    }

    response = requests.get(url, headers=headers)

    print("MODULE STATUS:", response.status_code)
    print("MODULE RESPONSE:", response.text)

    data = response.json()

    modules = []
    for m in data.get("modules", []):
        modules.append({
            "api_name": m.get("api_name"),
            "display_name": m.get("module_name")
        })

    return modules

# =========================
# 🔥 GET FIELDS
# =========================
def get_zoho_fields(access_token, module):
    url = f"https://www.zohoapis.in/crm/v2/settings/fields?module={module}"

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}"
    }

    response = requests.get(url, headers=headers)

    print("FIELDS RESPONSE:", response.text)

    data = response.json()

    fields = []
    for f in data.get("fields", []):
        fields.append({
            "api_name": f.get("api_name"),
            "display_label": f.get("field_label"),
            "required": f.get("system_mandatory", False)
        })

    return fields

# =========================
# DASHBOARD (DYNAMIC MODULES)
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(user_id: str):

    token_res = supabase.table("zoho_tokens") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()

    access_token = token_res.data[0]["zoho_access_token"]

    modules = get_zoho_modules(access_token)

    options = ""
    for m in modules:
        options += f'<option value="{m["api_name"]}">{m["display_name"]}</option>'

    return f"""
    <h2>Zoho Connected ✅</h2>

    <form action="/push-select" method="get">
        <input type="hidden" name="user_id" value="{user_id}">

        <label>Select Module:</label><br><br>
        <select name="module">
            {options}
        </select><br><br>

        <button type="submit">Continue</button>
    </form>
    """

# =========================
# FIELD MAPPING UI
# =========================
@app.get("/push-select", response_class=HTMLResponse)
def push_select(user_id: str, module: str):

    token_res = supabase.table("zoho_tokens") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()

    access_token = token_res.data[0]["zoho_access_token"]

    fields = get_zoho_fields(access_token, module)

    # show fields as checkboxes
    fields_html = ""
    for f in fields:
        required_tag = " (Required)" if f["required"] else ""
        fields_html += f"""
        <input type="checkbox" name="fields" value="{f['api_name']}">
        {f['display_label']}{required_tag}<br>
        """

    return f"""
    <h2>Select Fields → {module}</h2>

    <form action="/field-input" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        <input type="hidden" name="module" value="{module}">

        {fields_html}

        <br><br>
        <button type="submit">Next</button>
    </form>
    """

@app.post("/field-input", response_class=HTMLResponse)
async def field_input(request: Request):

    form = await request.form()

    user_id = form.get("user_id")
    module = form.get("module")
    selected_fields = form.getlist("fields")

    # fetch row list
    res = supabase.table("answers") \
        .select("id, answer, questions(question_text)") \
        .eq("user_id", user_id) \
        .execute()

    rows = res.data

    row_options = ""
    for r in rows:
        q = r.get("questions", {}).get("question_text", "")
        row_options += f'<option value="{r["id"]}">{r["id"]} - {q}</option>'

    # generate inputs
    inputs_html = ""
    for f in selected_fields:
        inputs_html += f"""
        <label>{f}</label><br>
        <input name="value_{f}"><br><br>
        """

    return f"""
    <h2>Enter Field Values → {module}</h2>

    <form action="/push" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        <input type="hidden" name="module" value="{module}">

        {''.join([f'<input type="hidden" name="fields" value="{f}">' for f in selected_fields])}

        {inputs_html}

        <h3>Select Row ID (for Q&A context)</h3>
        <select name="row_id">
            {row_options}
        </select>

        <br><br>
        <button type="submit">Push</button>
    </form>
    """
# =========================
# PUSH DATA
# =========================
@app.post("/push")
async def push_to_zoho(request: Request):

    form = await request.form()

    user_id = form.get("user_id")
    module = form.get("module")
    selected_fields = form.getlist("fields")
    row_id = form.get("row_id")

    # 🔹 Get token
    token_res = supabase.table("zoho_tokens") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()

    access_token = token_res.data[0]["zoho_access_token"]

    # 🔹 Build field data
    zoho_data = {}

    for f in selected_fields:
        value = form.get(f"value_{f}")
        if value:
            zoho_data[f] = value

    # 🔹 Fetch row context
    row_res = supabase.table("answers") \
        .select("answer, follow_up_question, questions(question_text)") \
        .eq("id", int(row_id)) \
        .execute()

    if row_res.data:
        row = row_res.data[0]
        description = f"""
        Question: {row.get('questions', {}).get('question_text', '')}
        Answer: {row.get('answer', '')}
        Follow Up: {row.get('follow_up_question', '')}
        """

        zoho_data["Description"] = description

    # 🔹 Push
    payload = {"data": [zoho_data]}

    url = f"https://www.zohoapis.in/crm/v2/{module}"
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}"
    }

    response = requests.post(url, json=payload, headers=headers)

    return {
        "module": module,
        "data_sent": zoho_data,
        "zoho_response": response.json()
    }
# =========================
# AUTO OPEN
# =========================
def open_browser():
    webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    import uvicorn
    threading.Timer(1.5, open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)