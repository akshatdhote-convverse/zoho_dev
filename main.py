import os
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import FastAPI, Request, HTTPException, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# =========================
# ENV + DB
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REDIRECT_URI = os.getenv("ZOHO_REDIRECT_URI")

ZOHO_AUTH_URL = "https://accounts.zoho.in/oauth/v2/auth"
ZOHO_TOKEN_URL = "https://accounts.zoho.in/oauth/v2/token"
ZOHO_API_BASE = "https://www.zohoapis.in/crm/v2"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# AUTH HELPER (FIXED)
# =========================
def get_current_user(request: Request):
    token = None

    # Header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        token = auth_header.replace("Bearer ", "")

    # Cookie fallback
    if not token:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = supabase.auth.get_user(token)

    if not user or not user.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    return user.user

# =========================
# HOME
# =========================
@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h1>Welcome</h1>
    <a href="/signup">Sign Up</a><br><br>
    <a href="/signin">Sign In</a>
    """

# =========================
# SIGNUP
# =========================
@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    return """
    <h2>Sign Up</h2>
    <form action="/signup" method="post">
        Email: <input name="email" type="email" required><br><br>
        Password: <input name="password" type="password" required><br><br>
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
        return {"success": False, "error": "Signup failed"}

    return RedirectResponse(url="/signin", status_code=302)

# =========================
# SIGNIN
# =========================
@app.get("/signin", response_class=HTMLResponse)
def signin_page():
    return """
    <h2>Sign In</h2>
    <form action="/signin" method="post">
        Email: <input name="email" type="email" required><br><br>
        Password: <input name="password" type="password" required><br><br>
        <button type="submit">Sign In</button>
    </form>
    """

@app.post("/signin")
def signin(email: str = Form(...), password: str = Form(...)):

    res = supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })

    if not res.session:
        return {"success": False, "error": "Invalid credentials"}

    access_token = res.session.access_token

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        samesite="lax"
    )

    return response

# =========================
# DASHBOARD
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(user=Depends(get_current_user)):
    return f"""
    <h2>Welcome {user.email}</h2>
    <a href="/crm/zoho/auth/connect">Connect Zoho</a>
    """

# =========================
# TOKEN MANAGEMENT
# =========================
def get_valid_token(user_id):
    res = supabase.table("crm_connections") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("provider", "zoho") \
        .execute()

    if not res.data:
        return None

    data = res.data[0]

    expiry = data.get("expiry")
    access_token = data.get("access_token")

    if expiry:
        expiry = datetime.fromisoformat(expiry)
        if datetime.utcnow() < expiry:
            return access_token

    return refresh_token(user_id, data.get("refresh_token"))

def refresh_token(user_id, refresh_token):
    payload = {
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": refresh_token
    }

    res = requests.post(ZOHO_TOKEN_URL, params=payload)
    data = res.json()

    if "access_token" not in data:
        return None

    new_token = data["access_token"]
    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))

    supabase.table("crm_connections").update({
        "access_token": new_token,
        "expiry": expiry.isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }).eq("user_id", user_id).execute()

    return new_token

# =========================
# ZOHO CONNECT
# =========================
@app.get("/crm/zoho/auth/connect")
def connect(user=Depends(get_current_user)):

    params = {
        "scope": "ZohoCRM.modules.ALL",
        "client_id": ZOHO_CLIENT_ID,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": ZOHO_REDIRECT_URI,
        "state": user.id
    }

    url = f"{ZOHO_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url)

@app.get("/crm/zoho/auth/callback")
def callback(code: str, state: str):

    payload = {
        "grant_type": "authorization_code",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "code": code
    }

    res = requests.post(ZOHO_TOKEN_URL, params=payload)
    data = res.json()

    if "access_token" not in data:
        return {"success": False, "error": data}

    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))

    supabase.table("crm_connections").upsert({
        "user_id": state,
        "provider": "zoho",
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expiry": expiry.isoformat(),
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="user_id, provider").execute()

    return RedirectResponse(url="/dashboard", status_code=302)

# =========================
# RECORD LIST
# =========================
@app.get("/crm/zoho/records/list")
def list_records(user=Depends(get_current_user)):

    token = get_valid_token(user.id)
    if not token:
        return {"success": False, "error": "Zoho not connected"}

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    modules = ["Leads", "Deals", "Contacts"]
    results = []

    for module in modules:
        url = f"{ZOHO_API_BASE}/{module}"
        res = requests.get(url, headers=headers).json()

        for r in res.get("data", []):
            results.append({
                "id": r.get("id"),
                "name": r.get("Deal_Name") or r.get("Full_Name"),
                "type": module
            })

    return {"success": True, "data": results}

# =========================
# MAPPING
# =========================
@app.post("/crm/zoho/records/map")
async def map_record(request: Request, user=Depends(get_current_user)):
    body = await request.json()

    supabase.table("crm_record_mappings").upsert({
        "user_id": user.id,
        "opportunity_id": body["opportunity_id"],
        "crm_record_id": body["crm_record_id"],
        "crm_object_type": body["crm_object_type"],
        "provider": "zoho",
        "updated_at": datetime.utcnow().isoformat()
    }).execute()

    return {"success": True}

# =========================
# PUSH
# =========================
@app.post("/crm/zoho/sync/push")
async def push(request: Request, user=Depends(get_current_user)):

    body = await request.json()
    opportunity_id = body["opportunity_id"]

    token = get_valid_token(user.id)
    if not token:
        return {"success": False, "error": "Zoho not connected"}

    mapping = supabase.table("crm_record_mappings") \
        .select("*") \
        .eq("user_id", user.id) \
        .eq("opportunity_id", opportunity_id) \
        .execute()

    if not mapping.data:
        return {"success": False, "error": "Mapping not found"}

    record = mapping.data[0]
    crm_id = record["crm_record_id"]
    module = record["crm_object_type"]

    config = supabase.table("user_config") \
        .select("*") \
        .eq("user_id", user.id) \
        .execute()

    push_mode = "notes"
    if config.data:
        push_mode = config.data[0].get("push_mode", "notes")

    res = supabase.table("answers") \
        .select("*") \
        .eq("opportunity_id", opportunity_id) \
        .execute()

    summary = "\n".join([r.get("answer", "") for r in res.data])

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    if push_mode == "notes":
        url = f"{ZOHO_API_BASE}/Notes"
        payload = {
            "data": [{
                "Note_Title": "Meeting Summary",
                "Note_Content": summary,
                "Parent_Id": crm_id
            }]
        }
        response = requests.post(url, json=payload, headers=headers).json()
    else:
        url = f"{ZOHO_API_BASE}/{module}/{crm_id}"
        payload = {"data": [{"Description": summary}]}
        response = requests.put(url, json=payload, headers=headers).json()

    return {"success": True, "zoho_response": response}