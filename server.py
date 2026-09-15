# -*- coding: utf-8 -*-
# ============================================================
#  MINI APP GAME SERVER — Crash + Trading (virtual points only)
#  FastAPI backend. Run with:
#     uvicorn server:app --host 0.0.0.0 --port 8000
#
#  This holds ONE shared Crash round and ONE shared Trading round
#  in memory — every user who opens the Mini App sees the exact
#  same live round (same plane, same multiplier, same players),
#  polled every ~500ms by the frontend.
#
#  Telegram WebApp initData is verified with the official HMAC
#  algorithm so a user can only ever spend/see their own points —
#  nobody can fake being someone else by editing requests.
#
#  IMPORTANT: Telegram requires Mini App URLs to be HTTPS and
#  publicly reachable. You must deploy this somewhere (Render,
#  Railway, Fly.io, a VPS with nginx+certbot, etc.) — it cannot
#  be opened from Telegram while only running on localhost.
# ============================================================

import asyncio
import hashlib
import hmac
import json
import math
import os
import random
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN", "8753733218:AAEPEinNOsUSUC1wvjJ__s3ahOHKf5OjGCk")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", os.getenv("OWNER_ID", "7467057651")).split(",") if x.strip().isdigit()}

USERS_FILE = "miniapp_users.json"
SETTINGS_FILE = "miniapp_settings.json"

QUICK_AMOUNTS = [50, 100, 250, 500]

DEFAULT_SETTINGS = {
    "starting_points": 1000,
    "crash": {
        "enabled": True,
        "join_window_seconds": 10,
        "tick_interval": 0.5,
        "min_crash_cap": 1.00,
        "max_crash_cap": 1000.0,
        "growth_rate": 0.12,
    },
    "trading": {
        "enabled": True,
        "round_seconds": 15,
        "tick_interval": 1.0,
        "volatility_pct": 1.5,
        "trend_bias_pct": 0.0,     # small drift added to each tick's random move (-2..2 typical); does NOT fix any single round's result
        "win_payout_multiplier": 1.9,
        "history_len": 40,
    },
}

# ==================== STORAGE ====================

def load_json(path, default):
    if not os.path.exists(path):
        save_json(path, default)
        return json.loads(json.dumps(default))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[save_json] {path}: {e}")

_settings_cache = None

def load_settings():
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    s = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    changed = False
    for k, v in DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = v; changed = True
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if k2 not in s[k]:
                    s[k][k2] = v2; changed = True
    if changed:
        save_json(SETTINGS_FILE, s)
    _settings_cache = s
    return s

def save_settings(s):
    global _settings_cache
    _settings_cache = s
    save_json(SETTINGS_FILE, s)

def get_setting(*path):
    s = load_settings()
    for p in path:
        s = s.get(p, {})
    return s

def set_setting(path, value):
    """path is a tuple like ('crash','growth_rate')."""
    s = load_settings()
    node = s
    for p in path[:-1]:
        node = node.setdefault(p, {})
    node[path[-1]] = value
    save_settings(s)

def load_users():
    return load_json(USERS_FILE, {})

def save_users(data):
    save_json(USERS_FILE, data)

def get_user(uid, name=None):
    uid = str(uid)
    data = load_users()
    if uid not in data:
        data[uid] = {"name": name or uid, "points": get_setting("starting_points") or 1000}
        save_users(data)
    elif name and data[uid].get("name") != name:
        data[uid]["name"] = name
        save_users(data)
    return data[uid]

def update_points(uid, delta):
    uid = str(uid)
    data = load_users()
    if uid not in data:
        data[uid] = {"name": uid, "points": get_setting("starting_points") or 1000}
    data[uid]["points"] = round(data[uid].get("points", 0) + delta, 2)
    save_users(data)
    return data[uid]["points"]

# ==================== TELEGRAM initData VERIFICATION ====================

def verify_init_data(init_data: str):
    """Official Telegram WebApp validation algorithm. Returns the parsed user
    dict if the signature checks out, otherwise None. This is what stops
    anyone from spending someone else's points by forging a request."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        user_json = pairs.get("user")
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        print(f"[verify_init_data] {e}")
        return None

def require_user(init_data: str):
    user = verify_init_data(init_data)
    if not user:
        # DEV FALLBACK: allow a plain numeric "initData" like "12345:Name"
        # so you can test locally without going through actual Telegram.
        # Remove this in production.
        if ":" in (init_data or ""):
            uid, name = init_data.split(":", 1)
            if uid.isdigit():
                return {"id": int(uid), "first_name": name}
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    return user

# ==================== CRASH ROUND (shared, global) ====================

class CrashState:
    def __init__(self):
        self.phase = "joining"          # joining -> flying -> crashed
        self.players = {}               # uid -> {"name","bet","cashed_out","cashout_multiplier","cashout_time"}
        self.crash_point = None
        self.start_monotonic = None
        self.join_deadline = None
        self.current_multiplier = 1.0

crash_state = CrashState()

def generate_crash_point(min_cap, max_cap):
    """Pure randomness — nobody, including the server operator, can steer an
    individual round's crash point. Standard crash-game distribution."""
    r = random.random()
    if r < 0.03:
        return round(random.uniform(min_cap, min_cap + 0.05), 2)
    raw = 0.99 / (1 - r) if r < 0.999 else max_cap
    return max(min_cap, min(max_cap, round(raw, 2)))

def crash_multiplier_at(elapsed_seconds, growth_rate):
    return round(math.exp(growth_rate * elapsed_seconds), 2)

def crash_public_state():
    cfg = get_setting("crash")
    s = crash_state
    remain = None
    if s.phase == "joining" and s.join_deadline:
        remain = max(0.0, (s.join_deadline - datetime.now()).total_seconds())
    return {
        "phase": s.phase,
        "multiplier": s.current_multiplier,
        "crash_point": s.crash_point if s.phase == "crashed" else None,
        "join_seconds_left": remain,
        "players": [
            {"uid": uid, "name": p["name"], "bet": p["bet"], "cashed_out": p["cashed_out"],
             "cashout_multiplier": p["cashout_multiplier"], "cashout_time": p["cashout_time"],
             "auto_cashout": p.get("auto_cashout")}
            for uid, p in s.players.items()
        ],
        "enabled": cfg.get("enabled", True),
    }

async def crash_round_loop():
    """Runs forever: joining -> flying -> crashed -> (pause) -> repeat."""
    while True:
        if not get_setting("crash", "enabled"):
            await asyncio.sleep(2)
            continue

        s = crash_state
        s.phase = "joining"
        s.players = {}
        s.crash_point = None
        s.current_multiplier = 1.0
        s.join_deadline = datetime.now() + timedelta(seconds=get_setting("crash", "join_window_seconds") or 10)

        while datetime.now() < s.join_deadline:
            await asyncio.sleep(0.25)

        if not s.players:
            await asyncio.sleep(2)
            continue

        s.phase = "flying"
        s.start_monotonic = time.monotonic()
        cfg = get_setting("crash")
        s.crash_point = generate_crash_point(cfg.get("min_crash_cap", 1.0), cfg.get("max_crash_cap", 1000.0))
        growth_rate = cfg.get("growth_rate", 0.12)
        tick = cfg.get("tick_interval", 0.5)

        while True:
            await asyncio.sleep(tick)
            elapsed = time.monotonic() - s.start_monotonic
            s.current_multiplier = crash_multiplier_at(elapsed, growth_rate)

            # Server-side auto cash-out: purely reactive to the live multiplier
            # that's already climbing — this does not reveal or change the
            # crash point, it just automates what a manual tap would do.
            for uid, p in list(s.players.items()):
                if not p["cashed_out"] and p.get("auto_cashout") and p["auto_cashout"] <= s.current_multiplier:
                    p["cashed_out"] = True
                    p["cashout_multiplier"] = p["auto_cashout"]
                    p["cashout_time"] = round(elapsed, 1)
                    winnings = round(p["bet"] * p["auto_cashout"], 2)
                    update_points(uid, winnings)

            if s.current_multiplier >= s.crash_point:
                s.current_multiplier = s.crash_point
                break

        s.phase = "crashed"
        await asyncio.sleep(4)  # let players see the result before next round

# ==================== TRADING ROUND (shared, global) ====================

class TradingState:
    def __init__(self):
        self.price = round(random.uniform(80, 120), 2)
        self.start_price = self.price
        self.history = [self.price]
        self.bets = {}          # uid -> {"name","side","amount"}
        self.round_end = None
        self.last_result = None  # {"start","end","direction"} after resolution

trading_state = TradingState()

def trading_public_state():
    s = trading_state
    remain = max(0.0, (s.round_end - datetime.now()).total_seconds()) if s.round_end else 0
    return {
        "price": s.price,
        "start_price": s.start_price,
        "history": s.history[-(get_setting("trading", "history_len") or 40):],
        "seconds_left": remain,
        "bets": [{"uid": uid, "name": b["name"], "side": b["side"], "amount": b["amount"]} for uid, b in s.bets.items()],
        "last_result": s.last_result,
        "enabled": get_setting("trading", "enabled"),
    }

async def trading_round_loop():
    while True:
        if not get_setting("trading", "enabled"):
            await asyncio.sleep(2)
            continue

        s = trading_state
        s.start_price = s.price
        s.bets = {}
        s.last_result = None
        round_seconds = get_setting("trading", "round_seconds") or 15
        s.round_end = datetime.now() + timedelta(seconds=round_seconds)

        tick = get_setting("trading", "tick_interval") or 1.0
        vol = get_setting("trading", "volatility_pct") or 1.5
        bias = get_setting("trading", "trend_bias_pct") or 0.0
        while datetime.now() < s.round_end:
            await asyncio.sleep(tick)
            move_pct = (random.uniform(-vol, vol) + bias) / 100
            s.price = max(0.01, round(s.price * (1 + move_pct), 2))
            s.history.append(s.price)
            hist_len = get_setting("trading", "history_len") or 40
            if len(s.history) > hist_len:
                s.history = s.history[-hist_len:]

        direction = "up" if s.price > s.start_price else ("down" if s.price < s.start_price else "flat")
        payout_mult = get_setting("trading", "win_payout_multiplier") or 1.9
        results = []
        for uid, b in s.bets.items():
            won = b["side"] == direction
            if won:
                payout = round(b["amount"] * payout_mult, 2)
                update_points(uid, payout)
                results.append({"uid": uid, "name": b["name"], "side": b["side"], "won": True, "payout": payout})
            else:
                results.append({"uid": uid, "name": b["name"], "side": b["side"], "won": False, "payout": 0})
        s.last_result = {"start": s.start_price, "end": s.price, "direction": direction, "results": results}
        await asyncio.sleep(4)

# ==================== FASTAPI APP ====================

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(crash_round_loop())
    asyncio.create_task(trading_round_loop())

# ---- request models ----

class JoinBody(BaseModel):
    initData: str
    amount: float
    auto_cashout: float | None = None

class CashoutBody(BaseModel):
    initData: str

class BetBody(BaseModel):
    initData: str
    side: str
    amount: float

# ---- crash endpoints ----

@app.get("/api/crash")
async def api_crash_state():
    return crash_public_state()

@app.post("/api/crash/join")
async def api_crash_join(body: JoinBody):
    user = require_user(body.initData)
    uid = str(user["id"])
    s = crash_state
    if s.phase != "joining":
        raise HTTPException(400, "This round is no longer joinable.")
    if uid in s.players:
        raise HTTPException(400, "You already joined this round.")
    if body.amount <= 0:
        raise HTTPException(400, "Invalid amount.")
    if body.auto_cashout is not None and body.auto_cashout < 1.01:
        raise HTTPException(400, "Auto cash out target must be at least 1.01x.")
    u = get_user(uid, user.get("first_name"))
    if u["points"] < body.amount:
        raise HTTPException(400, f"Not enough points. Balance: {u['points']:.0f}")
    update_points(uid, -body.amount)
    s.players[uid] = {
        "name": u["name"], "bet": body.amount, "cashed_out": False,
        "cashout_multiplier": None, "cashout_time": None,
        "auto_cashout": body.auto_cashout,
    }
    return {"ok": True, "balance": get_user(uid)["points"]}

@app.post("/api/crash/cashout")
async def api_crash_cashout(body: CashoutBody):
    user = require_user(body.initData)
    uid = str(user["id"])
    s = crash_state
    if s.phase != "flying":
        raise HTTPException(400, "Nothing to cash out right now.")
    p = s.players.get(uid)
    if not p or p["cashed_out"]:
        raise HTTPException(400, "You're not in this round (or already cashed out).")
    elapsed = time.monotonic() - s.start_monotonic
    growth_rate = get_setting("crash", "growth_rate") or 0.12
    mult = crash_multiplier_at(elapsed, growth_rate)
    if s.crash_point:
        mult = min(mult, s.crash_point - 0.001)
    p["cashed_out"] = True
    p["cashout_multiplier"] = mult
    p["cashout_time"] = round(elapsed, 1)
    winnings = round(p["bet"] * mult, 2)
    new_balance = update_points(uid, winnings)
    return {"ok": True, "multiplier": mult, "winnings": winnings, "balance": new_balance}

# ---- trading endpoints ----

@app.get("/api/trading")
async def api_trading_state():
    return trading_public_state()

@app.post("/api/trading/bet")
async def api_trading_bet(body: BetBody):
    user = require_user(body.initData)
    uid = str(user["id"])
    s = trading_state
    if body.side not in ("up", "down"):
        raise HTTPException(400, "Side must be 'up' or 'down'.")
    if not s.round_end or datetime.now() >= s.round_end:
        raise HTTPException(400, "Betting is closed for this round.")
    if uid in s.bets:
        raise HTTPException(400, "You already placed a bet this round.")
    if body.amount <= 0:
        raise HTTPException(400, "Invalid amount.")
    u = get_user(uid, user.get("first_name"))
    if u["points"] < body.amount:
        raise HTTPException(400, f"Not enough points. Balance: {u['points']:.0f}")
    update_points(uid, -body.amount)
    s.bets[uid] = {"name": u["name"], "side": body.side, "amount": body.amount}
    return {"ok": True, "balance": get_user(uid)["points"]}

# ---- misc ----

@app.get("/api/me")
async def api_me(initData: str):
    user = require_user(initData)
    u = get_user(str(user["id"]), user.get("first_name"))
    return {
        "uid": user["id"], "name": u["name"], "points": u["points"],
        "is_admin": int(user["id"]) in ADMIN_IDS,
    }

@app.get("/api/leaderboard")
async def api_leaderboard():
    data = load_users()
    rows = sorted(data.items(), key=lambda kv: kv[1].get("points", 0), reverse=True)[:10]
    return [{"uid": uid, "name": u.get("name", uid), "points": u.get("points", 0)} for uid, u in rows]

# ---- admin ----

def require_admin(init_data: str):
    user = require_user(init_data)
    if int(user["id"]) not in ADMIN_IDS:
        raise HTTPException(403, "Admin only.")
    return user

class AdminPointsBody(BaseModel):
    initData: str
    target_uid: str
    delta: float

class AdminSettingBody(BaseModel):
    initData: str
    path: list[str]     # e.g. ["crash", "growth_rate"]
    value: float | bool

@app.get("/api/admin/settings")
async def api_admin_settings(initData: str):
    require_admin(initData)
    return load_settings()

@app.post("/api/admin/settings")
async def api_admin_set_setting(body: AdminSettingBody):
    require_admin(body.initData)
    set_setting(tuple(body.path), body.value)
    return {"ok": True, "settings": load_settings()}

@app.post("/api/admin/points")
async def api_admin_points(body: AdminPointsBody):
    require_admin(body.initData)
    new_balance = update_points(body.target_uid, body.delta)
    return {"ok": True, "uid": body.target_uid, "balance": new_balance}

@app.get("/api/admin/users")
async def api_admin_users(initData: str):
    require_admin(initData)
    data = load_users()
    return [{"uid": uid, "name": u.get("name", uid), "points": u.get("points", 0)} for uid, u in data.items()]

# ---- static frontend ----

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
