import os
import requests
import webbrowser
import threading
from fastapi import FastAPI, Form
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
user_sessions = {}  # user_id -> zoho creds


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

    if res.user is None:
        return {"error": "Signup failed"}

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

    if res.user is None:
        return {"error": "Invalid credentials"}

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
    # clean inputs
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    redirect_uri = redirect_uri.strip()

    # store session
    user_sessions[user_id] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri
    }

    # 🔥 NEW WAY (correct encoding)
    params = {
        "scope": "ZohoCRM.modules.ALL",
        "client_id": client_id,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": redirect_uri,
        "state": user_id
    }

    auth_url = f"{ZOHO_ACCOUNTS_URL}/oauth/v2/auth?{urlencode(params)}"

    print("AUTH URL:", auth_url)

    return RedirectResponse(url=auth_url, status_code=302)

# =========================
# CALLBACK (FIXED)
# =========================
@app.get("/callback")
def callback(code: str, state: str):

    user_id = state

    creds = user_sessions.get(user_id)
    if not creds:
        return {"error": "Session expired"}

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

    if "access_token" not in data:
        return {"error": "Token exchange failed", "details": data}

    access_token = data["access_token"]
    refresh_token = data.get("refresh_token")

    # store in Supabase (linked to auth.users)
    supabase.table("zoho_tokens").upsert({
    "user_id": user_id,
    "zoho_access_token": access_token,
    "zoho_refresh_token": refresh_token
    }, on_conflict="user_id").execute()

    # cleanup session
    return RedirectResponse(url=f"/dashboard?user_id={user_id}", status_code=302)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(user_id: str):
    return f"""
    <h2>Zoho Connected ✅</h2>
    <p>Select where you want to push data:</p>

    <a href="/push-select?user_id={user_id}&module=Leads">Push to Leads</a><br><br>
    <a href="/push-select?user_id={user_id}&module=Contacts">Push to Contacts</a>
    """
@app.get("/push-select", response_class=HTMLResponse)
def push_select(user_id: str, module: str):

    res = supabase.table("answers") \
        .select("id, answer, follow_up_question, question_id, questions(question_text)") \
        .eq("user_id", user_id) \
        .execute()

    rows = res.data

    html_rows = ""
    for row in rows:
        question_text = row.get("questions", {}).get("question_text", "")

        html_rows += f"""
        <tr>
            <td>{row['id']}</td>
            <td>{question_text}</td>
            <td>{row.get('answer', '')}</td>
        </tr>
        """

    return f"""
    <h2>Select Row to Push → {module}</h2>

    <table border="1">
        <tr>
            <th>ID</th>
            <th>Question</th>
            <th>Answer</th>
        </tr>
        {html_rows}
    </table>

    <h3>Enter Details</h3>

    <form action="/push" method="post">
        <input type="hidden" name="user_id" value="{user_id}">
        <input type="hidden" name="module" value="{module}">

        Row ID: <input name="row_id"><br><br>
        Last Name: <input name="last_name"><br><br>
        Company: <input name="company"><br><br>
        Email: <input name="email"><br><br>

        <button type="submit">Push to Zoho</button>
    </form>
    """

@app.post("/push")
def push_to_zoho(
    user_id: str = Form(...),
    module: str = Form(...),
    row_id: int = Form(...),
    last_name: str = Form(...),
    company: str = Form(...),
    email: str = Form(...)
):

    # 🔹 Get Zoho token
    token_res = supabase.table("zoho_tokens").select("*").eq("user_id", user_id).execute()

    if not token_res.data:
        return {"error": "Zoho not connected"}

    access_token = token_res.data[0]["zoho_access_token"]

    # 🔹 Get row from answers
    row_res = supabase.table("answers") \
    .select("id, answer, follow_up_question, question_id, questions(question_text)") \
    .eq("id", row_id) \
    .eq("user_id", user_id) \
    .execute()

    if not row_res.data:
        return {"error": "Row not found"}

    row = row_res.data[0]

    question_text = row.get("questions", {}).get("question_text", "")

    description = f"""
    Question: {question_text}
    Answer: {row.get('answer', '')}
    Follow Up: {row.get('follow_up_question', '')}
    """

    # 🔹 Zoho API URL
    url = f"https://www.zohoapis.in/crm/v2/{module}"

    payload = {
        "data": [
            {
                "Last_Name": last_name,
                "Company": company,
                "Email": email,
                "Description": description
            }
        ]
    }

    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}"
    }

    response = requests.post(url, json=payload, headers=headers)
    data = response.json()

    return {
        "message": f"Pushed to {module}",
        "zoho_response": data
    }
# =========================
# AUTO OPEN BROWSER
# =========================
def open_browser():
    webbrowser.open("http://127.0.0.1:8000")


# =========================
# RUN
# =========================
if __name__ == "__main__":
    import uvicorn
    threading.Timer(1.5, open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")