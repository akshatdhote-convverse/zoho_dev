import os
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from supabase import create_client
from dotenv import load_dotenv
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
load_dotenv()
security = HTTPBearer()
app = FastAPI()

# =========================
# ENV + DB
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ZOHO_CLIENT_ID     = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REDIRECT_URI  = os.getenv("ZOHO_REDIRECT_URI")

ZOHO_AUTH_URL   = "https://accounts.zoho.in/oauth/v2/auth"
ZOHO_TOKEN_URL  = "https://accounts.zoho.in/oauth/v2/token"
ZOHO_REVOKE_URL = "https://accounts.zoho.in/oauth/v2/revoke"
ZOHO_API_BASE   = "https://www.zohoapis.in/crm/v2"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# =========================
# AUTH
# Reads token from Authorization header or cookie.
# Verifies through Supabase and returns the user object.
# =========================
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = supabase.auth.get_user(token)

    if not user or not user.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    return user.user
# =========================
# TOKEN MANAGEMENT
# Called before every Zoho API call.
# If token is expired → refresh it → update DB → return new token.
# =========================
def get_valid_token(user_id: str):
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
        expiry_dt = datetime.fromisoformat(expiry)
        if datetime.utcnow() < expiry_dt:
            return access_token  # still valid

    return do_refresh_token(user_id, data.get("refresh_token"))

    if not refresh_token:
        return None


def do_refresh_token(user_id: str, refresh_token: str):
    res = requests.post(ZOHO_TOKEN_URL, params={
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": refresh_token,
    })
    data = res.json()

    if "access_token" not in data:
        return None  # refresh failed — user must reconnect

    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))

    supabase.table("crm_connections").update({
        "access_token": data["access_token"],
        "expiry": expiry.isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("user_id", user_id).eq("provider", "zoho").execute()

    return data["access_token"]

# =========================
# ZOHO AUTH — CONNECT
# Returns the Zoho OAuth URL.
# Open auth_url in a browser → user logs in → Zoho calls /callback automatically.
# =========================
from fastapi import Form

@app.post("/crm/zoho/auth/token")
def get_token(email: str = Form(...), password: str = Form(...)):
    res = supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })

    if not res.session:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {"access_token": res.session.access_token}
@app.get("/crm/zoho/auth/connect")
def connect(user=Depends(get_current_user)):
    params = {
        "response_type": "code",
        "client_id": ZOHO_CLIENT_ID,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "scope": "ZohoCRM.modules.ALL",
        "access_type": "offline",
        "state": user.id,
    }
    url = f"{ZOHO_AUTH_URL}?{urlencode(params)}"
    return {"auth_url": url}

# =========================
# ZOHO AUTH — CALLBACK
# Zoho redirects here after the user grants access.
# Exchanges the code for tokens and saves them to DB.
# You don't call this — Zoho calls it as the redirect_uri.
# =========================
@app.get("/crm/zoho/auth/callback")
def callback(code: str, state: str):
    res = requests.post(ZOHO_TOKEN_URL, params={
        "grant_type": "authorization_code",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "code": code,
    })
    data = res.json()

    if "access_token" not in data:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {data}")

    expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))

    supabase.table("crm_connections").upsert({
        "user_id": state,         # state carries user_id from /connect
        "provider": "zoho",
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "expiry": expiry.isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="user_id,provider").execute()

    return {"success": True, "message": "Zoho connected successfully"}

# =========================
# ZOHO AUTH — DISCONNECT
# Revokes token on Zoho's side, then deletes from DB.
# =========================
@app.post("/crm/zoho/auth/disconnect")
def disconnect(user=Depends(get_current_user)):
    res = supabase.table("crm_connections") \
        .select("access_token") \
        .eq("user_id", user.id) \
        .eq("provider", "zoho") \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="No Zoho connection found")

    # Best-effort revoke — don't block disconnect if this fails
    try:
        requests.post(ZOHO_REVOKE_URL, params={"token": res.data[0]["access_token"]})
    except Exception:
        pass

    supabase.table("crm_connections") \
        .delete() \
        .eq("user_id", user.id) \
        .eq("provider", "zoho") \
        .execute()

    return {"success": True, "message": "Zoho disconnected"}

# =========================
# RECORDS — LIST
# Fetches Leads, Deals, Contacts from Zoho.
# Optional ?search=term filters by name.
# =========================
from typing import Optional

# =========================
# HELPER — PAGINATION
# =========================
def fetch_all_records(module, headers):
    all_data = []
    page = 1

    while True:
        res = requests.get(
            f"{ZOHO_API_BASE}/{module}",
            headers=headers,
            params={"page": page, "per_page": 200}
        )

        if res.status_code == 204:
            break

        if res.status_code != 200:
            print(f"Error in {module}: ", res.text)
            break

        data = res.json().get("data", [])

        if not data:
            break

        all_data.extend(data)

        if len(data) < 200:
            break

        page += 1

    return all_data


# =========================
# MAIN ENDPOINT
# =========================
@app.get("/crm/zoho/records/list")
def list_records(
    search: Optional[str] = None,
    user=Depends(get_current_user)
):
    token = get_valid_token(user.id)

    if not token:
        raise HTTPException(status_code=401, detail="Zoho not connected")

    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    modules = [
        ("Leads", "lead", "Full_Name"),
        ("Deals", "opportunity", "Deal_Name"),
        ("Contacts", "contact", "Full_Name"),
    ]

    results = []

    for module, record_type, name_field in modules:

        # =========================
        # SEARCH MODE
        # =========================
        if search and len(search) >= 3:
            res = requests.get(
                f"{ZOHO_API_BASE}/{module}/search",
                headers=headers,
                params={"criteria": f"({name_field}:contains:{search})"}
            )

            if res.status_code != 200:
                print(f"Search error {module}: ", res.text)
                continue

            data = res.json().get("data", [])

        # =========================
        # FETCH ALL MODE
        # =========================
        else:
            data = fetch_all_records(module, headers)

            # Local filtering for short search
            if search:
                search_lower = search.lower()
                data = [
                    r for r in data
                    if search_lower in str(r.get(name_field, "")).lower()
                ]

        # =========================
        # FORMAT OUTPUT
        # =========================
        for r in data:
            results.append({
                "id": r.get("id"),
                "name": r.get(name_field, ""),
                "type": record_type,
            })

    return {
        "success": True,
        "count": len(results),
        "data": results
    }
# =========================
# RECORDS — MAP
# Links a Convverse opportunity_id to a Zoho crm_record_id.
# This mapping is used by /sync/push to know which Zoho record to update.
# One opportunity maps to one Zoho record — re-mapping replaces the old one.
# =========================
from pydantic import BaseModel

class MapRecordRequest(BaseModel):
    opportunity_id: str
    crm_record_id: str
    crm_object_type: str  # "lead" | "opportunity" | "contact"

class PushRequest(BaseModel):
    opportunity_id: str
    user_id: str


@app.post("/crm/zoho/records/map")
def map_record(body: MapRecordRequest, user=Depends(get_current_user)):
    supabase.table("crm_record_mappings").upsert({
        "user_id": user.id,
        "opportunity_id": body.opportunity_id,
        "crm_record_id": body.crm_record_id,
        "crm_object_type": body.crm_object_type,
        "provider": "zoho",
        "updated_at": datetime.utcnow().isoformat(),
    }, on_conflict="user_id,opportunity_id").execute()

    return {"success": True, "message": "Mapping saved"}


@app.post("/crm/zoho/sync/push")
def push(body: PushRequest, user=Depends(get_current_user)):
    opportunity_id = body.opportunity_id
    user_id = body.user_id
    # 1. Get valid token
    token = get_valid_token(user.id)
    if not token:
        raise HTTPException(status_code=401, detail="Zoho not connected or token refresh failed")

    # 2. Fetch CRM mapping
    mapping_res = supabase.table("crm_record_mappings") \
        .select("*") \
        .eq("user_id", user.id) \
        .eq("opportunity_id", opportunity_id) \
        .execute()

    if not mapping_res.data:
        raise HTTPException(status_code=404, detail="No mapping found. Call POST /crm/zoho/records/map first.")

    record = mapping_res.data[0]
    crm_id = record["crm_record_id"]
    crm_object_type = record["crm_object_type"]

    module_map = {"lead": "Leads", "opportunity": "Deals", "contact": "Contacts"}
    zoho_module = module_map.get(crm_object_type.lower(), "Deals")

    # 3. Fetch push_mode from user_config
    config_res = supabase.table("user_config") \
        .select("push_mode") \
        .eq("user_id", user.id) \
        .execute()

    push_mode = "notes"
    if config_res.data:
        push_mode = config_res.data[0].get("push_mode", "notes")

    # 4. Fetch answers
    answers_res = supabase.table("answers") \
        .select("*") \
        .eq("opportunity_id", opportunity_id) \
        .execute()

    if not answers_res.data:
        raise HTTPException(status_code=404, detail="No answers found for this opportunity")

    summary = "\n".join([r.get("answer", "") for r in answers_res.data if r.get("answer")])

    if not summary.strip():
        raise HTTPException(status_code=400, detail="No content to push — answers are empty")

    # 5. Push to Zoho
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}

    if push_mode == "notes":
        payload = {"data": [{
            "Note_Title": "Meeting Summary",
            "Note_Content": summary,
            "Parent_Id": crm_id,
            "$se_module": zoho_module,
        }]}
        zoho_res = requests.post(f"{ZOHO_API_BASE}/Notes", json=payload, headers=headers)
    else:
        payload = {"data": [{"Description": summary}]}
        zoho_res = requests.put(f"{ZOHO_API_BASE}/{zoho_module}/{crm_id}", json=payload, headers=headers)

    if zoho_res.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Zoho push failed: {zoho_res.text}")

    return {"success": True, "push_mode": push_mode, "zoho_response": zoho_res.json()}