import os
import requests
import webbrowser
import threading
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client
from dotenv import load_dotenv
from urllib.parse import urlencode
from datetime import datetime, timedelta
load_dotenv()

app = FastAPI()

# =========================
# ENV
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REDIRECT_URI = os.getenv("ZOHO_REDIRECT_URI")

ZOHO_ACCOUNTS_URL = "https://accounts.zoho.in"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

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
# AUTH
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
    supabase.auth.sign_up({"email": email, "password": password})
    return RedirectResponse(url="/signin", status_code=302)

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

    return RedirectResponse(url=f"/zoho-connect?user_id={res.user.id}", status_code=302)

# =========================
# ZOHO CONNECT
# =========================
@app.get("/zoho-connect", response_class=HTMLResponse)
def zoho_connect_page(user_id: str):
    return f"""
    <h2>Connect Zoho</h2>
    <a href="/zoho-auth?user_id={user_id}">Connect Now</a>
    """

@app.get("/zoho-auth")
def zoho_auth(user_id: str):

    params = {
        "scope": "ZohoCRM.settings.modules.ALL,ZohoCRM.settings.fields.ALL,ZohoCRM.modules.ALL",
        "client_id": ZOHO_CLIENT_ID,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": ZOHO_REDIRECT_URI,
        "state": user_id
    }

    url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/auth?{urlencode(params)}"
    return RedirectResponse(url=url)

# =========================
# CALLBACK
# =========================

@app.get("/callback")
def callback(code: str, state: str):

    token_url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token"

    payload = {
        "grant_type": "authorization_code",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "code": code
    }

    response = requests.post(token_url, params=payload)
    data = response.json()

    if "access_token" not in data:
        return {"error": "Token failed", "details": data}

    expires_in = data.get("expires_in", 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    supabase.table("zoho_tokens").upsert({
        "user_id": state,
        "zoho_access_token": data["access_token"],
        "zoho_refresh_token": data.get("refresh_token"),
        "expires_in": expires_in,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.utcnow().isoformat(),
        "modified_at": datetime.utcnow().isoformat()
    }, on_conflict="user_id").execute()

    return RedirectResponse(url=f"/dashboard?user_id={state}", status_code=302)


def get_valid_token(user_id):

    res = supabase.table("zoho_tokens") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()

    if not res.data:
        return None

    token_data = res.data[0]

    access_token = token_data.get("zoho_access_token")
    refresh_token = token_data.get("zoho_refresh_token")
    expires_at = token_data.get("expires_at")

    # ✅ Safe expiry check
    try:
        if expires_at:
            expires_at = datetime.fromisoformat(expires_at)

            if datetime.utcnow() < expires_at:
                return access_token
    except Exception as e:
        print("Expiry parse error:", e)

    # 🔁 Refresh fallback
    if refresh_token:
        return refresh_access_token(refresh_token, user_id)

    return access_token


def refresh_access_token(refresh_token, user_id):

    url = "https://accounts.zoho.in/oauth/v2/token"

    payload = {
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": refresh_token
    }

    response = requests.post(url, params=payload)
    data = response.json()

    if "access_token" not in data:
        print("REFRESH FAILED:", data)
        return None

    new_access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    # 🔥 Update DB
    supabase.table("zoho_tokens").update({
        "zoho_access_token": new_access_token,
        "expires_in": expires_in,
        "expires_at": expires_at.isoformat(),
        "modified_at": datetime.utcnow().isoformat()
    }).eq("user_id", user_id).execute()

    return new_access_token
# =========================
# HELPERS
# =========================


def get_modules(token):
    url = "https://www.zohoapis.in/crm/v2/settings/modules"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    return requests.get(url, headers=headers).json().get("modules", [])

def get_fields(token, module):
    url = f"https://www.zohoapis.in/crm/v2/settings/fields?module={module}"
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    return requests.get(url, headers=headers).json().get("fields", [])

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(user_id: str):

    token = get_valid_token(user_id)
    if not token:
        return "Zoho not connected"

    modules = get_modules(token)

    options = "".join([
        f'<option value="{m["api_name"]}">{m["module_name"]}</option>'
        for m in modules if m.get("api_name")
    ])

    return f"""
    <h2>Select Module</h2>
    <form action="/push-select">
        <input type="hidden" name="user_id" value="{user_id}">
        <select name="module">{options}</select><br><br>
        <button type="submit">Next</button>
    </form>
    """

# =========================
# FIELD SELECT
# =========================
@app.get("/push-select", response_class=HTMLResponse)
def push_select(user_id: str, module: str):

    token = get_valid_token(user_id)   # ✅ FIXED

    if not token:
        return "Zoho not connected"

    fields = get_fields(token, module)

    html = ""
    for f in fields:
        html += f"""
        <input type="checkbox" name="fields" value="{f['api_name']}">
        {f['field_label']}<br>
        """

    return f"""
    <form action="/field-input" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        <input type="hidden" name="module" value="{module}">
        {html}
        <button type="submit">Next</button>
    </form>
    """
# =========================
# FIELD INPUT
# =========================
@app.post("/field-input", response_class=HTMLResponse)
async def field_input(request: Request):

    form = await request.form()

    user_id = form.get("user_id")
    module = form.get("module")
    fields = form.getlist("fields")

    # 🔹 Fetch data from Supabase
    res = supabase.table("answers") \
        .select("id, answer, questions(question_text)") \
        .eq("user_id", user_id) \
        .execute()

    rows = res.data

    if not rows:
        return "<h3>No data found for this user</h3>"

    # 🔹 Build checkbox list + table
    row_options = ""
    table_rows = ""

    for r in rows:
        q = r.get("questions", {}).get("question_text", "")
        a = r.get("answer", "")

        row_options += f"""
        <input type="checkbox" name="row_ids" value="{r["id"]}">
        <b>{r["id"]}</b> - {q}<br>
        """

        table_rows += f"""
        <tr>
            <td>{r["id"]}</td>
            <td>{q}</td>
            <td>{a}</td>
        </tr>
        """

    # 🔹 Build input fields
    inputs = ""
    for f in fields:
        inputs += f"""
        <label><b>{f}</b></label><br>
        <input name="val_{f}" placeholder="Enter {f}"><br><br>
        """

    # 🔥 FINAL HTML
    return f"""
    <h2>Enter Field Values → {module}</h2>

    <form action="/push" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        <input type="hidden" name="module" value="{module}">

        {''.join([f"<input type='hidden' name='fields' value='{f}'>" for f in fields])}

        <h3>Enter Values</h3>
        {inputs}

        <h3>Select Rows (Multiple Allowed)</h3>
        {row_options}

        <br><br>
        <button type="submit">Push</button>
    </form>

    <hr>

    <h3>Your Data Preview</h3>
    <table border="1" cellpadding="5">
        <tr>
            <th>ID</th>
            <th>Question</th>
            <th>Answer</th>
        </tr>
        {table_rows}
    </table>
    """
# =========================
# PUSH
# =========================
@app.post("/push")
async def push(request: Request):

    form = await request.form()

    user_id = form.get("user_id")
    module = form.get("module")
    fields = form.getlist("fields")
    row_ids = form.getlist("row_ids")

    if not row_ids:
        return {"error": "No rows selected"}

    token = get_valid_token(user_id)

    if not token:
        return {"error": "Zoho not connected"}

    data_list = []

    for row_id in row_ids:

        try:
            row_id_int = int(row_id)
        except:
            continue

        row_res = supabase.table("answers") \
            .select("answer, follow_up_question, questions(question_text)") \
            .eq("id", row_id_int) \
            .execute()

        if not row_res.data:
            continue

        row = row_res.data[0]

        description = f"""
        Question: {row.get('questions', {}).get('question_text', '')}
        Answer: {row.get('answer', '')}
        Follow Up: {row.get('follow_up_question', '')}
        """

        record = {}

        for f in fields:
            val = form.get(f"val_{f}")
            if val:
                record[f] = val

        record["Description"] = description

        data_list.append(record)

    if not data_list:
        return {"error": "No valid rows found"}

    url = f"https://www.zohoapis.in/crm/v2/{module}"

    headers = {
        "Authorization": f"Zoho-oauthtoken {token}"
    }

    response = requests.post(url, json={"data": data_list}, headers=headers)

    # 🔥 Safe update
    supabase.table("zoho_tokens").update({
        "modified_at": datetime.utcnow().isoformat()
    }).eq("user_id", user_id).execute()

    return {
        "records_sent": len(data_list),
        "zoho_response": response.json()
    }