"""
MailFlow SaaS — Full Stack
===========================
Features:
  - Animated light-blue / white UI throughout
  - Register / Login / Logout (hashed passwords)
  - Plan tiers: Free / Pro / Enterprise (enforced server-side)
  - Stripe Checkout + monthly subscriptions + webhooks
  - Per-user subscriber lists with unsubscribe tokens
  - Campaign sending via Resend with per-plan quota
  - Campaign history
  - Usage dashboard with progress bar
  - Admin panel

Install:
  pip install flask werkzeug requests stripe

Config:
  Edit the CONFIG section below before running.
"""

import sqlite3
import secrets
import uuid
import stripe
from contextlib import contextmanager
from datetime import datetime
from functools import wraps

import requests
from flask import (
    Flask, flash, redirect, render_template_string,
    request, session, url_for, abort
)
from werkzeug.security import check_password_hash, generate_password_hash

# =====================================================================
# CONFIG  —  edit these before running
# =====================================================================
SECRET_KEY        = "change-me-to-something-long-and-random"
RESEND_API_KEY    = "YOUR_RESEND_API_KEY"
FROM_EMAIL        = "onboarding@resend.dev"
BASE_URL          = "http://127.0.0.1:5000"

STRIPE_SECRET_KEY     = "sk_test_YOUR_STRIPE_SECRET"
STRIPE_WEBHOOK_SECRET = "whsec_YOUR_WEBHOOK_SECRET"

# Stripe Price IDs — create these in your Stripe dashboard
STRIPE_PRICES = {
    "pro":        "price_YOUR_PRO_PRICE_ID",
    "enterprise": "price_YOUR_ENTERPRISE_PRICE_ID",
}

DB_PATH = "mailflow.db"

PLANS = {
    "free":       {"limit": 50,     "label": "Free",       "price": 0},
    "pro":        {"limit": 5_000,  "label": "Pro",        "price": 19},
    "enterprise": {"limit": 100_000,"label": "Enterprise", "price": 99},
}

# =====================================================================
# App
# =====================================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY
stripe.api_key = STRIPE_SECRET_KEY

# =====================================================================
# DB
# =====================================================================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        c = conn.cursor()
        yield conn, c
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as (conn, c):
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            email               TEXT UNIQUE NOT NULL,
            password            TEXT NOT NULL,
            is_admin            INTEGER NOT NULL DEFAULT 0,
            plan                TEXT NOT NULL DEFAULT 'free',
            used                INTEGER NOT NULL DEFAULT 0,
            stripe_customer_id  TEXT,
            stripe_sub_id       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS subscribers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            email       TEXT NOT NULL,
            unsub_token TEXT NOT NULL,
            subscribed  INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, email)
        );

        CREATE TABLE IF NOT EXISTS campaigns (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject     TEXT NOT NULL,
            body        TEXT NOT NULL,
            sent_count  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """)

init_db()

# =====================================================================
# CSRF
# =====================================================================
def _csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(24)
    return session["_csrf"]

def csrf_protect():
    if request.method == "POST":
        token = request.form.get("_csrf_token", "")
        if not secrets.compare_digest(token, _csrf_token()):
            abort(403, "CSRF check failed.")

app.jinja_env.globals["csrf_token"] = _csrf_token

# =====================================================================
# Auth decorators
# =====================================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            abort(403, "Admins only.")
        return f(*args, **kwargs)
    return wrapper

# =====================================================================
# Email
# =====================================================================
def send_email(to_email, subject, html):
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"from": FROM_EMAIL, "to": [to_email],
                  "subject": subject, "html": html},
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        print("EMAIL ERROR:", e)
        return False

# =====================================================================
# Shared CSS + Base template
# =====================================================================
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Serif+Display&display=swap');

@keyframes fadeDown {
  from { opacity:0; transform:translateY(-20px); }
  to   { opacity:1; transform:translateY(0); }
}
@keyframes fadeUp {
  from { opacity:0; transform:translateY(20px); }
  to   { opacity:1; transform:translateY(0); }
}
@keyframes fadeIn {
  from { opacity:0; }
  to   { opacity:1; }
}
@keyframes shimmer {
  0%   { background-position: -400px 0; }
  100% { background-position: 400px 0; }
}
@keyframes pulse-ring {
  0%,100% { box-shadow: 0 0 0 0 rgba(14,165,233,.4); }
  50%      { box-shadow: 0 0 0 8px rgba(14,165,233,0); }
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
@keyframes slideIn {
  from { opacity:0; transform:translateX(20px); }
  to   { opacity:1; transform:translateX(0); }
}

*, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }

:root {
  --sky:     #0ea5e9;
  --sky-lt:  #38bdf8;
  --sky-dim: #7dd3fc;
  --sky-bg:  #f0f9ff;
  --sky-100: #e0f2fe;
  --sky-200: #bae6fd;
  --navy:    #0c4a6e;
  --mid:     #0369a1;
  --white:   #ffffff;
  --card-bg: rgba(255,255,255,.92);
  --shadow:  0 4px 24px rgba(14,165,233,.12);
  --radius:  14px;
}

html { scroll-behavior: smooth; }

body {
  font-family: 'DM Sans', sans-serif;
  background: linear-gradient(145deg, #e0f2fe 0%, #f0f9ff 50%, #bae6fd 100%);
  min-height: 100vh;
  color: var(--navy);
}

/* ---- NAV ---- */
nav {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 32px;
  height: 62px;
  background: rgba(255,255,255,.8);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--sky-200);
  position: sticky; top:0; z-index:100;
  animation: fadeDown .4s ease both;
}
.nav-brand {
  font-family: 'DM Serif Display', serif;
  font-size: 1.3rem;
  color: var(--mid);
  text-decoration: none;
  margin-right: auto;
}
.nav-brand span { color: var(--sky); }
nav a {
  text-decoration: none;
  color: var(--mid);
  font-size: .9rem;
  font-weight: 500;
  padding: 6px 14px;
  border-radius: 8px;
  transition: background .15s;
}
nav a:hover { background: var(--sky-100); }
.nav-btn {
  background: var(--sky) !important;
  color: #fff !important;
  animation: pulse-ring 2.5s infinite;
}
.nav-btn:hover { opacity:.88; background: var(--sky) !important; }

/* ---- PAGE WRAPPER ---- */
.page {
  max-width: 860px;
  margin: 0 auto;
  padding: 40px 24px 80px;
}
.page-sm {
  max-width: 460px;
  margin: 0 auto;
  padding: 40px 24px 80px;
}

/* ---- CARD ---- */
.card {
  background: var(--card-bg);
  border: 1px solid var(--sky-200);
  border-radius: var(--radius);
  padding: 28px;
  box-shadow: var(--shadow);
}
.card + .card { margin-top: 20px; }

/* ---- HERO ---- */
.hero {
  text-align: center;
  padding: 60px 24px 40px;
  animation: fadeDown .6s ease both;
}
.hero h1 {
  font-family: 'DM Serif Display', serif;
  font-size: clamp(2.2rem, 5vw, 3.2rem);
  color: var(--mid);
  line-height: 1.2;
}
.hero h1 span { color: var(--sky); }
.hero p {
  color: var(--mid);
  opacity: .75;
  margin-top: 12px;
  font-size: 1.05rem;
  max-width: 480px;
  margin-left: auto;
  margin-right: auto;
}

/* ---- FORMS ---- */
label {
  display: block;
  font-size: .82rem;
  font-weight: 600;
  color: var(--mid);
  letter-spacing: .4px;
  text-transform: uppercase;
  margin-top: 16px;
  margin-bottom: 6px;
}
input[type=text], input[type=email], input[type=password], textarea, select {
  width: 100%;
  padding: 11px 14px;
  border-radius: 10px;
  border: 1.5px solid var(--sky-200);
  background: #fff;
  color: var(--navy);
  font-family: 'DM Sans', sans-serif;
  font-size: .95rem;
  outline: none;
  transition: border-color .2s, box-shadow .2s;
}
input:focus, textarea:focus, select:focus {
  border-color: var(--sky);
  box-shadow: 0 0 0 3px rgba(14,165,233,.15);
}
input::placeholder, textarea::placeholder { color: var(--sky-dim); }
textarea { min-height: 110px; resize: vertical; }

/* ---- BUTTONS ---- */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 11px 24px;
  border: none;
  border-radius: 10px;
  background: linear-gradient(90deg, var(--sky), var(--sky-lt));
  color: #fff;
  font-family: 'DM Sans', sans-serif;
  font-size: .95rem;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: opacity .2s, transform .15s;
  animation: pulse-ring 2.5s infinite;
}
.btn:hover  { opacity:.88; transform:translateY(-1px); }
.btn:active { transform:scale(.97); }
.btn-full   { width:100%; margin-top:18px; }
.btn-sm     { padding:6px 14px; font-size:.82rem; animation:none; }
.btn-ghost  {
  background: transparent;
  color: var(--sky);
  border: 1.5px solid var(--sky-200);
  animation: none;
}
.btn-ghost:hover { background: var(--sky-100); }
.btn-danger { background: linear-gradient(90deg,#f43f5e,#fb7185); animation:none; }

.spinner {
  display:none; width:15px; height:15px;
  border:2px solid rgba(255,255,255,.35);
  border-top-color:#fff;
  border-radius:50%;
  animation:spin .6s linear infinite;
}
.btn.loading { opacity:.7; pointer-events:none; }
.btn.loading .spinner { display:inline-block; }

/* ---- FLASH ---- */
.flash-wrap { max-width:860px; margin:16px auto 0; padding:0 24px; }
.flash {
  padding:11px 16px;
  border-radius:10px;
  font-size:.9rem;
  font-weight:500;
  margin-bottom:10px;
  animation: slideIn .3s ease both;
}
.flash.success { background:#dcfce7; color:#166534; border:1px solid #bbf7d0; }
.flash.error   { background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }
.flash.warning { background:#fef9c3; color:#854d0e; border:1px solid #fef08a; }
.flash.info    { background:var(--sky-100); color:var(--mid); border:1px solid var(--sky-200); }

/* ---- STAT CARDS ---- */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:16px; margin-bottom:24px; }
.stat-card {
  background:var(--card-bg);
  border:1px solid var(--sky-200);
  border-radius:var(--radius);
  padding:20px;
  box-shadow:var(--shadow);
  text-align:center;
}
.stat-card .num {
  font-family:'DM Serif Display',serif;
  font-size:2rem;
  color:var(--sky);
}
.stat-card .lbl { font-size:.82rem; color:var(--mid); opacity:.75; margin-top:4px; }

/* ---- PROGRESS ---- */
.progress { background:var(--sky-100); border-radius:999px; height:8px; overflow:hidden; margin-top:6px; }
.progress-bar { background:linear-gradient(90deg,var(--sky),var(--sky-lt)); height:100%; border-radius:999px; transition:width .6s ease; }

/* ---- BADGE ---- */
.badge {
  display:inline-block; padding:3px 12px;
  border-radius:999px; font-size:.75rem; font-weight:700;
}
.badge-free       { background:#dbeafe; color:#1d4ed8; }
.badge-pro        { background:#d1fae5; color:#065f46; }
.badge-enterprise { background:#ede9fe; color:#5b21b6; }

/* ---- TABLE ---- */
table { width:100%; border-collapse:collapse; font-size:.9rem; }
th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--sky-100); }
th { background:var(--sky-bg); font-size:.8rem; text-transform:uppercase; letter-spacing:.5px; color:var(--mid); }
tr:last-child td { border-bottom:none; }
tr:hover td { background:var(--sky-bg); }

/* ---- PLAN CARDS ---- */
.plans { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-top:24px; }
.plan-card {
  background:var(--card-bg);
  border:2px solid var(--sky-200);
  border-radius:var(--radius);
  padding:24px;
  text-align:center;
  transition:border-color .2s, transform .2s;
}
.plan-card:hover { border-color:var(--sky); transform:translateY(-2px); }
.plan-card.current { border-color:var(--sky); background:var(--sky-bg); }
.plan-card .price { font-family:'DM Serif Display',serif; font-size:2.2rem; color:var(--mid); }
.plan-card .price span { font-family:'DM Sans',sans-serif; font-size:1rem; color:var(--mid); opacity:.6; }
.plan-card h3 { font-size:1.1rem; color:var(--mid); margin-bottom:8px; }
.plan-card p { font-size:.85rem; color:var(--mid); opacity:.7; margin-bottom:16px; }

/* ---- DIVIDER ---- */
.divider { border:none; border-top:1px solid var(--sky-200); margin:24px 0; }

/* ---- SECTION TITLE ---- */
.section-title {
  font-family:'DM Serif Display',serif;
  font-size:1.4rem;
  color:var(--mid);
  margin-bottom:16px;
}

/* ---- ANIMATIONS on load ---- */
.anim-1 { animation: fadeUp .5s .1s ease both; }
.anim-2 { animation: fadeUp .5s .2s ease both; }
.anim-3 { animation: fadeUp .5s .3s ease both; }
.anim-4 { animation: fadeUp .5s .4s ease both; }
</style>
"""

NAV = """
<nav>
  <a class="nav-brand" href="{{ url_for('home') }}">Mail<span>Flow</span></a>
  {% if session.get('user_id') %}
    <a href="{{ url_for('dashboard') }}">Dashboard</a>
    <a href="{{ url_for('subscribers_page') }}">Subscribers</a>
    <a href="{{ url_for('history') }}">History</a>
    <a href="{{ url_for('billing') }}">Billing</a>
    {% if session.get('is_admin') %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
    <form method="POST" action="{{ url_for('logout') }}" style="margin:0">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-sm btn-ghost" style="cursor:pointer">Logout</button>
    </form>
  {% else %}
    <a href="{{ url_for('login') }}">Login</a>
    <a href="{{ url_for('register') }}" class="btn btn-sm nav-btn">Get Started</a>
  {% endif %}
</nav>
"""

FLASHES = """
<div class="flash-wrap">
{% for cat, msg in get_flashed_messages(with_categories=True) %}
  <div class="flash {{ cat }}">{{ msg }}</div>
{% endfor %}
</div>
"""

def page(body, title="MailFlow"):
    return render_template_string(
        f"<!doctype html><html lang='en'><head>"
        f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} — MailFlow</title>"
        f"{STYLE}</head><body>"
        f"{NAV}{FLASHES}{body}"
        f"<script>function startLoad(f){{var b=f.querySelector('.btn');b.classList.add('loading');b.querySelector('.lbl').textContent='Sending…';}}</script>"
        f"</body></html>"
    )

# =====================================================================
# HOME
# =====================================================================
@app.route("/")
def home():
    return page("""
<div class="hero">
  <h1>Email marketing<br>made <span>simple.</span></h1>
  <p>Send beautiful campaigns to your audience. Start free, scale when you're ready.</p>
  <div style="margin-top:28px;display:flex;gap:12px;justify-content:center;flex-wrap:wrap">
    <a href="{{ url_for('register') }}" class="btn">Start for free</a>
    <a href="{{ url_for('pricing') }}" class="btn btn-ghost">See pricing</a>
  </div>
</div>

<div class="page">
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-top:20px">
    <div class="card anim-1" style="text-align:center">
      <div style="font-size:2rem;margin-bottom:8px">✉️</div>
      <h3 style="color:var(--mid);margin-bottom:6px">Easy Campaigns</h3>
      <p style="font-size:.9rem;opacity:.7;color:var(--navy)">Write and send to your whole list in seconds.</p>
    </div>
    <div class="card anim-2" style="text-align:center">
      <div style="font-size:2rem;margin-bottom:8px">📊</div>
      <h3 style="color:var(--mid);margin-bottom:6px">Usage Dashboard</h3>
      <p style="font-size:.9rem;opacity:.7;color:var(--navy)">Track sends, subscribers, and quota in one place.</p>
    </div>
    <div class="card anim-3" style="text-align:center">
      <div style="font-size:2rem;margin-bottom:8px">🔒</div>
      <h3 style="color:var(--mid);margin-bottom:6px">Secure & Reliable</h3>
      <p style="font-size:.9rem;opacity:.7;color:var(--navy)">Powered by Resend. Your data stays yours.</p>
    </div>
  </div>
</div>
""", "Home")

# =====================================================================
# PRICING
# =====================================================================
@app.route("/pricing")
def pricing():
    return page("""
<div class="hero" style="padding-bottom:20px">
  <h1>Simple, <span>honest</span> pricing</h1>
  <p>No hidden fees. Cancel anytime.</p>
</div>
<div class="page" style="padding-top:0">
  <div class="plans">
    {% for key, plan in plans.items() %}
    <div class="plan-card anim-{{ loop.index }}">
      <h3>{{ plan.label }}</h3>
      <div class="price">${{ plan.price }}<span>/mo</span></div>
      <p>{{ "{:,}".format(plan.limit) }} emails / month</p>
      {% if key == 'free' %}
        <a href="{{ url_for('register') }}" class="btn btn-full">Get started</a>
      {% else %}
        <a href="{{ url_for('billing') }}" class="btn btn-full">Upgrade</a>
      {% endif %}
    </div>
    {% endfor %}
  </div>
</div>
""", "Pricing", plans=PLANS)

# Override page() to pass plans — quick helper for pricing
def page(body, title="MailFlow", **ctx):
    return render_template_string(
        f"<!doctype html><html lang='en'><head>"
        f"<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} — MailFlow</title>"
        f"{STYLE}</head><body>"
        f"{NAV}{FLASHES}{body}"
        f"<script>function startLoad(f){{var b=f.querySelector('.btn');b.classList.add('loading');b.querySelector('.lbl').textContent='Sending…';}}</script>"
        f"</body></html>",
        plans=PLANS,
        **ctx
    )

# =====================================================================
# REGISTER
# =====================================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return page("""
<div class="page-sm">
  <div class="hero" style="padding:40px 0 28px">
    <h1 style="font-size:2rem">Create account</h1>
    <p>Free forever. No credit card required.</p>
  </div>
  <div class="card anim-1">
    <form method="POST" action="{{ url_for('register') }}">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <label>Email address</label>
      <input type="email" name="email" placeholder="you@example.com" required autocomplete="username">
      <label>Password <span style="font-weight:400;text-transform:none;font-size:.8rem;opacity:.6">(min 8 chars)</span></label>
      <input type="password" name="password" placeholder="••••••••" minlength="8" required>
      <button class="btn btn-full" type="submit">
        <span class="spinner"></span>
        <span class="lbl">Create account</span>
      </button>
    </form>
  </div>
  <p style="text-align:center;margin-top:16px;font-size:.9rem;color:var(--mid)">
    Already have an account? <a href="{{ url_for('login') }}" style="color:var(--sky);font-weight:600">Log in</a>
  </p>
</div>
""", "Register")

    csrf_protect()
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if "@" not in email or len(password) < 8:
        flash("Please enter a valid email and password (8+ chars).", "error")
        return redirect(url_for("register"))

    try:
        with get_db() as (conn, c):
            c.execute(
                "INSERT INTO users (email, password) VALUES (?, ?)",
                (email, generate_password_hash(password)),
            )
        flash("Account created — please log in.", "success")
        return redirect(url_for("login"))
    except sqlite3.IntegrityError:
        flash("An account with that email already exists.", "error")
        return redirect(url_for("register"))

# =====================================================================
# LOGIN
# =====================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return page("""
<div class="page-sm">
  <div class="hero" style="padding:40px 0 28px">
    <h1 style="font-size:2rem">Welcome back</h1>
    <p>Log in to your MailFlow account.</p>
  </div>
  <div class="card anim-1">
    <form method="POST" action="{{ url_for('login') }}">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <label>Email address</label>
      <input type="email" name="email" placeholder="you@example.com" required autocomplete="username">
      <label>Password</label>
      <input type="password" name="password" placeholder="••••••••" required>
      <button class="btn btn-full" type="submit">
        <span class="spinner"></span>
        <span class="lbl">Log in</span>
      </button>
    </form>
  </div>
  <p style="text-align:center;margin-top:16px;font-size:.9rem;color:var(--mid)">
    No account? <a href="{{ url_for('register') }}" style="color:var(--sky);font-weight:600">Sign up free</a>
  </p>
</div>
""", "Login")

    csrf_protect()
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    with get_db() as (conn, c):
        c.execute("SELECT id, password, is_admin, plan FROM users WHERE email=?", (email,))
        user = c.fetchone()

    if user and check_password_hash(user["password"], password):
        session.clear()
        session["user_id"]  = user["id"]
        session["is_admin"] = bool(user["is_admin"])
        session["plan"]     = user["plan"]
        flash("Welcome back! 👋", "success")
        return redirect(url_for("dashboard"))

    flash("Invalid email or password.", "error")
    return redirect(url_for("login"))

# =====================================================================
# LOGOUT
# =====================================================================
@app.route("/logout", methods=["POST"])
def logout():
    csrf_protect()
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("home"))

# =====================================================================
# DASHBOARD
# =====================================================================
@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("SELECT email, plan, used FROM users WHERE id=?", (user_id,))
        user = c.fetchone()
        c.execute("SELECT COUNT(*) as n FROM subscribers WHERE user_id=? AND subscribed=1", (user_id,))
        sub_count = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM campaigns WHERE user_id=?", (user_id,))
        camp_count = c.fetchone()["n"]

    plan  = user["plan"]
    limit = PLANS[plan]["limit"]
    used  = user["used"]
    pct   = min(100, round(used / limit * 100, 1)) if limit else 0

    return page("""
<div class="page">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:24px;animation:fadeDown .4s ease both">
    <div>
      <h1 class="section-title" style="margin-bottom:4px">Dashboard</h1>
      <p style="font-size:.9rem;color:var(--mid);opacity:.7">{{ user['email'] }}
        <span class="badge badge-{{ user['plan'] }}" style="margin-left:6px">{{ plans[user['plan']]['label'] }}</span>
      </p>
    </div>
    <a href="{{ url_for('billing') }}" class="btn btn-ghost btn-sm">Upgrade plan</a>
  </div>

  <div class="stats">
    <div class="stat-card anim-1">
      <div class="num">{{ used }}</div>
      <div class="lbl">Emails sent</div>
    </div>
    <div class="stat-card anim-2">
      <div class="num">{{ sub_count }}</div>
      <div class="lbl">Subscribers</div>
    </div>
    <div class="stat-card anim-3">
      <div class="num">{{ camp_count }}</div>
      <div class="lbl">Campaigns</div>
    </div>
    <div class="stat-card anim-4">
      <div class="num">{{ limit - used }}</div>
      <div class="lbl">Emails remaining</div>
    </div>
  </div>

  <div class="card anim-2" style="margin-bottom:20px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-weight:600;font-size:.9rem;color:var(--mid)">Monthly quota</span>
      <span style="font-size:.85rem;color:var(--mid);opacity:.7">{{ used }} / {{ "{:,}".format(limit) }}</span>
    </div>
    <div class="progress"><div class="progress-bar" style="width:{{ pct }}%"></div></div>
    {% if pct >= 90 %}
      <p style="margin-top:8px;font-size:.82rem;color:#dc2626;font-weight:500">
        ⚠️ Approaching limit — <a href="{{ url_for('billing') }}" style="color:#dc2626">upgrade your plan</a>
      </p>
    {% endif %}
  </div>

  <div class="card anim-3">
    <h2 class="section-title" style="font-size:1.1rem;margin-bottom:16px">📣 Send Campaign</h2>
    <form method="POST" action="{{ url_for('send_campaign') }}" onsubmit="startLoad(this)">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <label>Subject line</label>
      <input type="text" name="subject" placeholder="Your campaign subject…" required {% if used >= limit %}disabled{% endif %}>
      <label>Message <span style="font-weight:400;text-transform:none;font-size:.8rem;opacity:.6">(HTML supported)</span></label>
      <textarea name="message" placeholder="Write your email…" required {% if used >= limit %}disabled{% endif %}></textarea>
      <button class="btn btn-full" type="submit" {% if used >= limit %}disabled style="opacity:.4;animation:none;pointer-events:none"{% endif %}>
        <span class="spinner"></span>
        <span class="lbl">{% if used >= limit %}Limit reached — upgrade to send{% else %}Send to {{ sub_count }} subscriber{{ 's' if sub_count != 1 else '' }}{% endif %}</span>
      </button>
    </form>
  </div>
</div>
""", "Dashboard", user=user, used=used, limit=limit, pct=pct,
     sub_count=sub_count, camp_count=camp_count)

# =====================================================================
# SUBSCRIBERS
# =====================================================================
@app.route("/subscribers")
@login_required
def subscribers_page():
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("""SELECT id, email, subscribed, created_at
                     FROM subscribers WHERE user_id=? ORDER BY id DESC""", (user_id,))
        subs = c.fetchall()

    return page("""
<div class="page">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;animation:fadeDown .4s ease both">
    <h1 class="section-title" style="margin-bottom:0">Subscribers</h1>
    <span style="font-size:.9rem;color:var(--mid);opacity:.7">{{ subs|length }} total</span>
  </div>

  <div class="card anim-1" style="margin-bottom:20px">
    <h2 style="font-size:1rem;color:var(--mid);margin-bottom:14px;font-family:'DM Serif Display',serif">Add subscriber</h2>
    <form method="POST" action="{{ url_for('add_sub') }}" style="display:flex;gap:10px;flex-wrap:wrap">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <input type="email" name="email" placeholder="subscriber@example.com" required style="flex:1;min-width:200px">
      <button class="btn" type="submit" style="margin-top:0;animation:none">Add</button>
    </form>
  </div>

  <div class="card anim-2" style="overflow:auto">
    <table>
      <thead>
        <tr><th>Email</th><th>Status</th><th>Added</th><th></th></tr>
      </thead>
      <tbody>
      {% for s in subs %}
        <tr>
          <td>{{ s['email'] }}</td>
          <td>{% if s['subscribed'] %}<span style="color:#16a34a;font-weight:600">● Active</span>
              {% else %}<span style="color:#dc2626;font-weight:600">● Unsubscribed</span>{% endif %}</td>
          <td style="opacity:.6;font-size:.85rem">{{ s['created_at'][:10] }}</td>
          <td>
            <form method="POST" action="{{ url_for('delete_sub', sub_id=s['id']) }}" style="display:inline">
              <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <button class="btn btn-sm btn-danger" type="submit">Remove</button>
            </form>
          </td>
        </tr>
      {% else %}
        <tr><td colspan="4" style="text-align:center;opacity:.5;padding:24px">No subscribers yet. Add one above.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
""", "Subscribers", subs=subs)


@app.route("/add_sub", methods=["POST"])
@login_required
def add_sub():
    csrf_protect()
    email   = request.form.get("email", "").strip().lower()
    user_id = session["user_id"]

    if "@" not in email:
        flash("Invalid email address.", "error")
        return redirect(url_for("subscribers_page"))

    token = secrets.token_urlsafe(32)
    try:
        with get_db() as (conn, c):
            c.execute(
                "INSERT INTO subscribers (user_id, email, unsub_token) VALUES (?, ?, ?)",
                (user_id, email, token),
            )
        flash(f"{email} added.", "success")
    except sqlite3.IntegrityError:
        flash("That subscriber already exists.", "warning")

    return redirect(url_for("subscribers_page"))


@app.route("/delete_sub/<int:sub_id>", methods=["POST"])
@login_required
def delete_sub(sub_id):
    csrf_protect()
    with get_db() as (conn, c):
        c.execute("DELETE FROM subscribers WHERE id=? AND user_id=?",
                  (sub_id, session["user_id"]))
    flash("Subscriber removed.", "info")
    return redirect(url_for("subscribers_page"))

# =====================================================================
# SEND CAMPAIGN
# =====================================================================
@app.route("/send", methods=["POST"])
@login_required
def send_campaign():
    csrf_protect()
    user_id = session["user_id"]
    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()

    if not subject or not message:
        flash("Subject and message are required.", "error")
        return redirect(url_for("dashboard"))

    with get_db() as (conn, c):
        c.execute("SELECT used, plan FROM users WHERE id=?", (user_id,))
        row = c.fetchone()
        limit = PLANS.get(row["plan"], PLANS["free"])["limit"]

        if row["used"] >= limit:
            flash("Email limit reached. Please upgrade your plan.", "error")
            return redirect(url_for("billing"))

        c.execute("""SELECT id, email, unsub_token FROM subscribers
                     WHERE user_id=? AND subscribed=1""", (user_id,))
        subs = c.fetchall()

    remaining = limit - row["used"]
    count = 0

    for sub in subs:
        if count >= remaining:
            break
        unsub_url = url_for("unsubscribe", token=sub["unsub_token"], _external=True)
        html = f"""{message}
        <hr style="margin-top:32px;border:none;border-top:1px solid #e0f2fe">
        <p style="font-size:.75rem;color:#94a3b8;margin-top:8px">
          Don't want these emails?
          <a href="{unsub_url}" style="color:#0ea5e9">Unsubscribe</a>
        </p>"""
        if send_email(sub["email"], subject, html):
            count += 1

    with get_db() as (conn, c):
        c.execute("UPDATE users SET used = used + ? WHERE id=?", (count, user_id))
        c.execute(
            "INSERT INTO campaigns (user_id, subject, body, sent_count) VALUES (?,?,?,?)",
            (user_id, subject, message, count),
        )

    flash(f"✅ Campaign sent to {count} subscriber{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("dashboard"))

# =====================================================================
# CAMPAIGN HISTORY
# =====================================================================
@app.route("/history")
@login_required
def history():
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("""SELECT subject, sent_count, created_at FROM campaigns
                     WHERE user_id=? ORDER BY id DESC LIMIT 50""", (user_id,))
        campaigns = c.fetchall()

    return page("""
<div class="page">
  <h1 class="section-title" style="animation:fadeDown .4s ease both">Campaign History</h1>
  <div class="card anim-1" style="overflow:auto">
    <table>
      <thead><tr><th>Subject</th><th>Sent to</th><th>Date</th></tr></thead>
      <tbody>
      {% for c in campaigns %}
        <tr>
          <td style="font-weight:500">{{ c['subject'] }}</td>
          <td>{{ c['sent_count'] }} subscriber{{ 's' if c['sent_count'] != 1 else '' }}</td>
          <td style="opacity:.6;font-size:.85rem">{{ c['created_at'][:16] }}</td>
        </tr>
      {% else %}
        <tr><td colspan="3" style="text-align:center;opacity:.5;padding:24px">No campaigns sent yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
""", "History", campaigns=campaigns)

# =====================================================================
# UNSUBSCRIBE (public)
# =====================================================================
@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    with get_db() as (conn, c):
        c.execute("UPDATE subscribers SET subscribed=0 WHERE unsub_token=?", (token,))
        changed = conn.total_changes

    if changed:
        return page("""
<div class="page-sm" style="text-align:center;padding-top:80px">
  <div style="font-size:3rem;margin-bottom:16px">✅</div>
  <h1 style="font-family:'DM Serif Display',serif;color:var(--mid);margin-bottom:8px">Unsubscribed</h1>
  <p style="color:var(--mid);opacity:.7">You won't receive further emails from this sender.</p>
  <a href="{{ url_for('home') }}" class="btn" style="margin-top:24px;animation:none">Go home</a>
</div>
""", "Unsubscribed")
    return page("<div class='page-sm' style='padding-top:80px;text-align:center'><h2>Invalid link.</h2></div>", "Error"), 404

# =====================================================================
# BILLING / STRIPE
# =====================================================================
@app.route("/billing")
@login_required
def billing():
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("SELECT plan, stripe_sub_id FROM users WHERE id=?", (user_id,))
        user = c.fetchone()

    current_plan = user["plan"]

    return page("""
<div class="page">
  <h1 class="section-title" style="animation:fadeDown .4s ease both">Billing & Plans</h1>
  <p style="color:var(--mid);opacity:.7;margin-bottom:4px;animation:fadeDown .4s ease both">
    Current plan: <strong>{{ plans[current_plan]['label'] }}</strong>
  </p>

  <div class="plans" style="margin-top:20px">
    {% for key, plan in plans.items() %}
    <div class="plan-card {% if key == current_plan %}current{% endif %} anim-{{ loop.index }}">
      {% if key == current_plan %}
        <div style="font-size:.75rem;font-weight:700;color:var(--sky);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Current plan</div>
      {% endif %}
      <h3>{{ plan.label }}</h3>
      <div class="price">${{ plan.price }}<span>/mo</span></div>
      <p>{{ "{:,}".format(plan.limit) }} emails / month</p>
      {% if key == current_plan %}
        <button class="btn btn-full btn-ghost" style="animation:none" disabled>Active</button>
      {% elif key == 'free' %}
        <form method="POST" action="{{ url_for('downgrade') }}">
          <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <button class="btn btn-full btn-ghost" style="animation:none" type="submit">Downgrade</button>
        </form>
      {% else %}
        <form method="POST" action="{{ url_for('create_checkout') }}">
          <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
          <input type="hidden" name="plan" value="{{ key }}">
          <button class="btn btn-full" type="submit">Upgrade to {{ plan.label }}</button>
        </form>
      {% endif %}
    </div>
    {% endfor %}
  </div>

  {% if current_plan != 'free' and user['stripe_sub_id'] %}
  <div class="card anim-4" style="margin-top:24px;text-align:center">
    <p style="font-size:.9rem;color:var(--mid);margin-bottom:12px">Want to cancel your subscription?</p>
    <form method="POST" action="{{ url_for('cancel_sub') }}">
      <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-sm btn-danger" type="submit" style="animation:none">Cancel subscription</button>
    </form>
  </div>
  {% endif %}
</div>
""", "Billing", current_plan=current_plan, user=user)


@app.route("/billing/checkout", methods=["POST"])
@login_required
def create_checkout():
    csrf_protect()
    plan = request.form.get("plan")
    if plan not in STRIPE_PRICES:
        flash("Invalid plan.", "error")
        return redirect(url_for("billing"))

    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("SELECT email, stripe_customer_id FROM users WHERE id=?", (user_id,))
        user = c.fetchone()

    # Create or reuse Stripe customer
    cust_id = user["stripe_customer_id"]
    if not cust_id:
        cust = stripe.Customer.create(email=user["email"])
        cust_id = cust.id
        with get_db() as (conn, c):
            c.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust_id, user_id))

    try:
        checkout = stripe.checkout.Session.create(
            customer=cust_id,
            mode="subscription",
            line_items=[{"price": STRIPE_PRICES[plan], "quantity": 1}],
            success_url=BASE_URL + url_for("billing_success"),
            cancel_url=BASE_URL + url_for("billing"),
        )
        return redirect(checkout.url, code=303)
    except Exception as e:
        flash(f"Stripe error: {e}", "error")
        return redirect(url_for("billing"))


@app.route("/billing/success")
@login_required
def billing_success():
    flash("🎉 Payment successful! Your plan has been upgraded.", "success")
    return redirect(url_for("dashboard"))


@app.route("/billing/downgrade", methods=["POST"])
@login_required
def downgrade():
    csrf_protect()
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("UPDATE users SET plan='free', stripe_sub_id=NULL WHERE id=?", (user_id,))
    session["plan"] = "free"
    flash("Downgraded to Free plan.", "info")
    return redirect(url_for("billing"))


@app.route("/billing/cancel", methods=["POST"])
@login_required
def cancel_sub():
    csrf_protect()
    user_id = session["user_id"]
    with get_db() as (conn, c):
        c.execute("SELECT stripe_sub_id FROM users WHERE id=?", (user_id,))
        sub_id = c.fetchone()["stripe_sub_id"]

    if sub_id:
        try:
            stripe.Subscription.cancel(sub_id)
        except Exception as e:
            flash(f"Error cancelling: {e}", "error")
            return redirect(url_for("billing"))

    with get_db() as (conn, c):
        c.execute("UPDATE users SET plan='free', stripe_sub_id=NULL WHERE id=?", (user_id,))
    session["plan"] = "free"
    flash("Subscription cancelled. You've been moved to Free.", "info")
    return redirect(url_for("billing"))


# =====================================================================
# STRIPE WEBHOOK
# =====================================================================
@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return "Bad signature", 400

    if event["type"] == "checkout.session.completed":
        sess    = event["data"]["object"]
        cust_id = sess.get("customer")
        sub_id  = sess.get("subscription")
        # Determine plan from subscription items
        try:
            sub   = stripe.Subscription.retrieve(sub_id)
            price = sub["items"]["data"][0]["price"]["id"]
            plan  = next((k for k, v in STRIPE_PRICES.items() if v == price), "free")
        except Exception:
            plan = "pro"
        with get_db() as (conn, c):
            c.execute(
                "UPDATE users SET plan=?, stripe_sub_id=? WHERE stripe_customer_id=?",
                (plan, sub_id, cust_id),
            )

    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        sub     = event["data"]["object"]
        cust_id = sub.get("customer")
        with get_db() as (conn, c):
            c.execute(
                "UPDATE users SET plan='free', stripe_sub_id=NULL WHERE stripe_customer_id=?",
                (cust_id,),
            )

    return "ok", 200

# =====================================================================
# ADMIN
# =====================================================================
@app.route("/admin")
@login_required
@admin_required
def admin():
    with get_db() as (conn, c):
        c.execute("SELECT id, email, plan, used, created_at FROM users ORDER BY id DESC")
        users = c.fetchall()
        c.execute("SELECT COUNT(*) as n FROM subscribers")
        total_subs = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM campaigns")
        total_camps = c.fetchone()["n"]

    return page("""
<div class="page">
  <h1 class="section-title" style="animation:fadeDown .4s ease both">Admin Panel</h1>

  <div class="stats" style="margin-bottom:24px">
    <div class="stat-card anim-1"><div class="num">{{ users|length }}</div><div class="lbl">Users</div></div>
    <div class="stat-card anim-2"><div class="num">{{ total_subs }}</div><div class="lbl">Subscribers</div></div>
    <div class="stat-card anim-3"><div class="num">{{ total_camps }}</div><div class="lbl">Campaigns</div></div>
  </div>

  <div class="card anim-4" style="overflow:auto">
    <table>
      <thead><tr><th>Email</th><th>Plan</th><th>Used</th><th>Joined</th><th>Actions</th></tr></thead>
      <tbody>
      {% for u in users %}
        <tr>
          <td>{{ u['email'] }}</td>
          <td>
            <form method="POST" action="{{ url_for('admin_set_plan', user_id=u['id']) }}" style="display:inline">
              <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <select name="plan" onchange="this.form.submit()" style="width:auto;padding:4px 8px;font-size:.82rem">
                {% for key, info in plans.items() %}
                  <option value="{{ key }}" {% if u['plan']==key %}selected{% endif %}>{{ info['label'] }}</option>
                {% endfor %}
              </select>
            </form>
          </td>
          <td>{{ u['used'] }}</td>
          <td style="opacity:.6;font-size:.85rem">{{ u['created_at'][:10] }}</td>
          <td>
            <form method="POST" action="{{ url_for('admin_reset_usage', user_id=u['id']) }}" style="display:inline">
              <input type="hidden" name="_csrf_token" value="{{ csrf_token() }}">
              <button class="btn btn-sm btn-ghost" type="submit" style="animation:none">Reset usage</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
""", "Admin", users=users, total_subs=total_subs, total_camps=total_camps)


@app.route("/admin/set_plan/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_set_plan(user_id):
    csrf_protect()
    plan = request.form.get("plan")
    if plan not in PLANS:
        flash("Invalid plan.", "error")
        return redirect(url_for("admin"))
    with get_db() as (conn, c):
        c.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
    flash("Plan updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/reset_usage/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def admin_reset_usage(user_id):
    csrf_protect()
    with get_db() as (conn, c):
        c.execute("UPDATE users SET used=0 WHERE id=?", (user_id,))
    flash("Usage reset.", "success")
    return redirect(url_for("admin"))

# =====================================================================
# ERROR PAGES
# =====================================================================
@app.errorhandler(403)
def forbidden(e):
    return page(f"<div class='page-sm' style='padding-top:80px;text-align:center'><h2 style='color:var(--mid)'>403 — Forbidden</h2><p style='margin-top:8px;opacity:.7'>{e}</p></div>", "Forbidden"), 403

@app.errorhandler(404)
def not_found(e):
    return page("<div class='page-sm' style='padding-top:80px;text-align:center'><h2 style='color:var(--mid)'>404 — Page not found</h2></div>", "Not Found"), 404

# =====================================================================
# RUN
# =====================================================================
app.run(host="0.0.0.0", port=5000, debug=True)
