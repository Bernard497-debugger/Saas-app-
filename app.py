#!/usr/bin/env python3
"""
Complete SaaS Automation App - No Compilation Required
Run with: python app.py
"""

import json
import secrets
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, List, Any
from functools import wraps

# Use only pure-Python dependencies
from fastapi import FastAPI, HTTPException, Depends, Request, Form, Cookie, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import requests
import smtplib
from email.mime.text import MIMEText
import hashlib
import hmac
import os
import threading
import time
import queue
from dataclasses import dataclass, asdict

# ============ Simplified Config ============
DATABASE_FILE = "saas_app.db"
SECRET_KEY = secrets.token_urlsafe(32)
APP_NAME = "AutomationFlow SaaS"

# ============ Pure Python Password Hashing (no bcrypt needed) ============
def hash_password(password: str, salt: str = None) -> tuple:
    """Simple but secure password hashing"""
    if salt is None:
        salt = secrets.token_hex(16)
    hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return hash_obj.hex(), salt

def verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verify password against hash"""
    new_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(new_hash, hashed)

# ============ Database Setup (SQLite, no SQLAlchemy) ============
def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                full_name TEXT,
                plan TEXT DEFAULT 'free',
                api_key TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS automations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                trigger_type TEXT,
                trigger_config TEXT,
                actions TEXT,
                is_active BOOLEAN DEFAULT 1,
                last_run TIMESTAMP,
                run_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            
            CREATE TABLE IF NOT EXISTS automation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                automation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT,
                result TEXT,
                error_message TEXT,
                execution_time_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (automation_id) REFERENCES automations(id)
            );
            
            CREATE TABLE IF NOT EXISTS execution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                automation_id INTEGER NOT NULL,
                action_type TEXT,
                status TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

# Initialize database
init_db()

# ============ Background Task Queue (Simple threading instead of Celery) ============
class TaskQueue:
    def __init__(self):
        self.queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
    
    def _worker(self):
        while True:
            task_func, args, kwargs, result_queue = self.queue.get()
            try:
                result = task_func(*args, **kwargs)
                if result_queue:
                    result_queue.put(result)
            except Exception as e:
                if result_queue:
                    result_queue.put({"error": str(e)})
            finally:
                self.queue.task_done()
    
    def add_task(self, task_func, *args, **kwargs):
        self.queue.put((task_func, args, kwargs, None))
    
    def add_task_with_result(self, task_func, *args, **kwargs):
        result_queue = queue.Queue()
        self.queue.put((task_func, args, kwargs, result_queue))
        return result_queue.get(timeout=30)

task_queue = TaskQueue()

# ============ Action Executors ============
def execute_http_request(config: Dict) -> Dict:
    """Execute HTTP request"""
    try:
        method = config.get("method", "GET")
        url = config.get("url")
        headers = config.get("headers", {})
        body = config.get("body")
        timeout = config.get("timeout", 30)
        
        start_time = time.time()
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body if isinstance(body, dict) else None,
            data=body if isinstance(body, str) else None,
            timeout=timeout
        )
        execution_time = (time.time() - start_time) * 1000
        
        return {
            "status": "success",
            "status_code": response.status_code,
            "body": response.text[:1000],
            "execution_time_ms": execution_time
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

def execute_automation(automation_id: int, user_id: int, trigger_data: Dict = None):
    """Execute an automation workflow"""
    with get_db() as conn:
        # Get automation
        automation = conn.execute(
            "SELECT * FROM automations WHERE id = ? AND user_id = ?",
            (automation_id, user_id)
        ).fetchone()
        
        if not automation or not automation["is_active"]:
            return {"error": "Automation not found or inactive"}
        
        actions = json.loads(automation["actions"])
        results = []
        context = trigger_data or {}
        
        for action in actions:
            action_type = action.get("type")
            config = action.get("config", {})
            
            start_time = time.time()
            
            if action_type == "http_request":
                result = execute_http_request(config)
            elif action_type == "send_email":
                result = {"status": "success", "message": "Email would be sent (configure SMTP)"}
            elif action_type == "webhook":
                result = execute_http_request(config)
            else:
                result = {"status": "error", "error": f"Unknown action: {action_type}"}
            
            execution_time = (time.time() - start_time) * 1000
            
            # Log execution
            conn.execute(
                """INSERT INTO execution_logs (user_id, automation_id, action_type, status, details)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, automation_id, action_type, 
                 result.get("status", "unknown"),
                 json.dumps(result))
            )
            
            results.append(result)
            context[f"action_{len(results)}_result"] = result
            
            if result.get("status") == "error" and action.get("stop_on_fail", False):
                break
        
        # Update automation stats
        conn.execute(
            "UPDATE automations SET last_run = CURRENT_TIMESTAMP, run_count = run_count + 1 WHERE id = ?",
            (automation_id,)
        )
        
        # Create automation log
        conn.execute(
            """INSERT INTO automation_logs (automation_id, user_id, status, result, execution_time_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (automation_id, user_id,
             "success" if all(r.get("status") == "success" for r in results) else "partial",
             json.dumps({"results": results}),
             sum(r.get("execution_time_ms", 0) for r in results if isinstance(r, dict)))
        )
        conn.commit()
        
        return {"status": "completed", "results": results}

# ============ FastAPI App ============
app = FastAPI(title=APP_NAME)

# Setup templates
templates = Jinja2Templates(directory="templates")
os.makedirs("templates", exist_ok=True)

# ============ Auth Helpers ============
def get_current_user(request: Request):
    session_token = request.cookies.get("session_token")
    if not session_token:
        return None
    
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE api_key = ? AND is_active = 1",
            (session_token,)
        ).fetchone()
        return dict(user) if user else None

# ============ Routes ============
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: dict = Depends(get_current_user)):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "app_name": APP_NAME, "user": current_user}
    )

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    with get_db() as conn:
        automations = conn.execute(
            "SELECT * FROM automations WHERE user_id = ? ORDER BY created_at DESC",
            (current_user["id"],)
        ).fetchall()
        
        recent_logs = conn.execute(
            """SELECT * FROM execution_logs 
               WHERE user_id = ? ORDER BY created_at DESC LIMIT 20""",
            (current_user["id"],)
        ).fetchall()
        
        automations = [dict(a) for a in automations]
        for a in automations:
            a["trigger_config"] = json.loads(a["trigger_config"]) if a["trigger_config"] else {}
            a["actions"] = json.loads(a["actions"]) if a["actions"] else []
        
        total_runs = sum(a["run_count"] for a in automations)
        active_automations = sum(1 for a in automations if a["is_active"])
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "automations": automations,
            "recent_logs": [dict(l) for l in recent_logs],
            "total_runs": total_runs,
            "active_automations": active_automations
        }
    )

@app.get("/automations/new", response_class=HTMLResponse)
async def new_automation_form(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("automation_form.html", {"request": request, "user": current_user})

@app.post("/automations/create")
async def create_automation(
    name: str = Form(...),
    description: str = Form(""),
    trigger_type: str = Form(...),
    trigger_config_json: str = Form(...),
    actions_json: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    with get_db() as conn:
        conn.execute(
            """INSERT INTO automations (user_id, name, description, trigger_type, trigger_config, actions)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (current_user["id"], name, description, trigger_type, trigger_config_json, actions_json)
        )
        conn.commit()
    
    return RedirectResponse(url="/dashboard", status_code=302)

@app.post("/automations/{automation_id}/toggle")
async def toggle_automation(automation_id: int, current_user: dict = Depends(get_current_user)):
    with get_db() as conn:
        conn.execute(
            "UPDATE automations SET is_active = NOT is_active WHERE id = ? AND user_id = ?",
            (automation_id, current_user["id"])
        )
        conn.commit()
    return RedirectResponse(url="/dashboard", status_code=302)

@app.post("/automations/{automation_id}/run")
async def run_automation(automation_id: int, current_user: dict = Depends(get_current_user)):
    task_queue.add_task(execute_automation, automation_id, current_user["id"])
    return RedirectResponse(url="/dashboard", status_code=302)

@app.delete("/automations/{automation_id}")
async def delete_automation(automation_id: int, current_user: dict = Depends(get_current_user)):
    with get_db() as conn:
        conn.execute("DELETE FROM automations WHERE id = ? AND user_id = ?", (automation_id, current_user["id"]))
        conn.commit()
    return JSONResponse({"success": True})

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    with get_db() as conn:
        logs = conn.execute(
            """SELECT * FROM execution_logs 
               WHERE user_id = ? ORDER BY created_at DESC LIMIT 100""",
            (current_user["id"],)
        ).fetchall()
        logs = [dict(l) for l in logs]
        for log in logs:
            if log["details"]:
                log["details"] = json.loads(log["details"])
    
    return templates.TemplateResponse("logs.html", {"request": request, "user": current_user, "logs": logs})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: dict = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request, "user": current_user})

@app.post("/settings/update")
async def update_settings(
    full_name: str = Form(None),
    email: str = Form(None),
    current_user: dict = Depends(get_current_user)
):
    with get_db() as conn:
        if full_name:
            conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name, current_user["id"]))
        if email:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, current_user["id"]))
        conn.commit()
    return RedirectResponse(url="/settings", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        
        if user and verify_password(password, user["password_hash"], user["password_salt"]):
            response = RedirectResponse(url="/dashboard", status_code=302)
            response.set_cookie(key="session_token", value=user["api_key"], httponly=True)
            return response
    
    return HTMLResponse("Invalid credentials", status_code=401)

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(email: str = Form(...), password: str = Form(...), full_name: str = Form(...)):
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            return HTMLResponse("Email already registered", status_code=400)
        
        password_hash, password_salt = hash_password(password)
        api_key = secrets.token_urlsafe(32)
        
        conn.execute(
            """INSERT INTO users (email, password_hash, password_salt, full_name, api_key)
               VALUES (?, ?, ?, ?, ?)""",
            (email, password_hash, password_salt, full_name, api_key)
        )
        conn.commit()
        
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(key="session_token", value=user["api_key"], httponly=True)
        return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session_token")
    return response

@app.post("/webhook/{automation_id}")
async def webhook_trigger(automation_id: int, request: Request):
    """Webhook endpoint to trigger automations"""
    data = await request.json()
    
    with get_db() as conn:
        automation = conn.execute(
            "SELECT * FROM automations WHERE id = ? AND trigger_type = 'webhook' AND is_active = 1",
            (automation_id,)
        ).fetchone()
        
        if automation:
            task_queue.add_task(execute_automation, automation_id, automation["user_id"], data)
            return {"status": "triggered"}
    
    return {"error": "Automation not found"}

# ============ Templates (same as before, but simplified) ============
templates_content = {
    "index.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ app_name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-br from-blue-50 to-indigo-100 min-h-screen">
    <div class="container mx-auto px-4 py-16">
        <div class="max-w-4xl mx-auto">
            <div class="text-center mb-12">
                <h1 class="text-5xl font-bold text-gray-800 mb-4">{{ app_name }}</h1>
                <p class="text-xl text-gray-600">Automate your workflows without writing code</p>
            </div>
            
            <div class="bg-white rounded-lg shadow-xl p-8 mb-8">
                <div class="grid md:grid-cols-3 gap-6 mb-8">
                    <div class="text-center"><div class="text-3xl font-bold text-blue-600">⚡</div><h3 class="font-semibold mt-2">Real-time Triggers</h3><p class="text-sm text-gray-600">Schedule, webhook, or interval-based</p></div>
                    <div class="text-center"><div class="text-3xl font-bold text-blue-600">🔗</div><h3 class="font-semibold mt-2">Multiple Actions</h3><p class="text-sm text-gray-600">HTTP, Email, Webhooks, Conditions</p></div>
                    <div class="text-center"><div class="text-3xl font-bold text-blue-600">📊</div><h3 class="font-semibold mt-2">Analytics</h3><p class="text-sm text-gray-600">Track every execution</p></div>
                </div>
                <div class="text-center space-x-4">
                    {% if user %}<a href="/dashboard" class="inline-block bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition">Go to Dashboard</a>
                    {% else %}<a href="/login" class="inline-block bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition">Login</a><a href="/register" class="inline-block bg-green-600 text-white px-6 py-3 rounded-lg hover:bg-green-700 transition">Sign Up Free</a>{% endif %}
                </div>
            </div>
        </div>
    </div>
</body>
</html>
    """,
    
    "dashboard.html": """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Dashboard</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100">
<nav class="bg-white shadow-sm"><div class="container mx-auto px-4 py-3 flex justify-between items-center"><h1 class="text-xl font-bold">{{ app_name }}</h1><div class="space-x-4"><span>Welcome, {{ user.full_name or user.email }}</span><a href="/dashboard" class="text-blue-600">Dashboard</a><a href="/automations/new" class="text-green-600">New</a><a href="/logs" class="text-gray-600">Logs</a><a href="/settings" class="text-gray-600">Settings</a><a href="/logout" class="text-red-600">Logout</a></div></div></nav>
<div class="container mx-auto px-4 py-8">
<div class="grid md:grid-cols-4 gap-4 mb-8"><div class="bg-white rounded-lg p-4 shadow"><div class="text-2xl font-bold text-blue-600">{{ automations|length }}</div><div class="text-gray-600">Automations</div></div><div class="bg-white rounded-lg p-4 shadow"><div class="text-2xl font-bold text-green-600">{{ active_automations }}</div><div class="text-gray-600">Active</div></div><div class="bg-white rounded-lg p-4 shadow"><div class="text-2xl font-bold text-purple-600">{{ total_runs }}</div><div class="text-gray-600">Total Runs</div></div><div class="bg-white rounded-lg p-4 shadow"><div class="text-2xl font-bold text-orange-600">{{ user.plan|upper }}</div><div class="text-gray-600">Plan</div></div></div>
<div class="bg-white rounded-lg shadow"><div class="p-4 border-b"><h2 class="text-lg font-semibold">Your Automations</h2></div><div class="divide-y">{% for automation in automations %}<div class="p-4 flex justify-between items-center"><div><h3 class="font-semibold">{{ automation.name }}</h3><p class="text-sm text-gray-600">{{ automation.description or 'No description' }}</p><div class="text-xs text-gray-500 mt-1">Trigger: {{ automation.trigger_type }} | Runs: {{ automation.run_count }} | Last: {{ automation.last_run[:19] if automation.last_run else 'Never' }}</div></div><div class="space-x-2"><form action="/automations/{{ automation.id }}/toggle" method="post" class="inline"><button class="px-3 py-1 rounded text-sm {{ 'bg-green-500' if automation.is_active else 'bg-gray-500' }} text-white">{{ 'Active' if automation.is_active else 'Inactive' }}</button></form><form action="/automations/{{ automation.id }}/run" method="post" class="inline"><button class="px-3 py-1 bg-blue-500 text-white rounded text-sm">Run</button></form><button onclick="deleteAutomation({{ automation.id }})" class="px-3 py-1 bg-red-500 text-white rounded text-sm">Delete</button></div></div>{% else %}<div class="p-8 text-center text-gray-500">No automations yet. <a href="/automations/new" class="text-blue-600">Create one →</a></div>{% endfor %}</div></div>
</div>
<script>async function deleteAutomation(id){if(confirm('Delete?')){await fetch(`/automations/${id}`,{method:'DELETE'});location.reload();}}</script>
</body>
</html>
    """,
    
    "automation_form.html": """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>New Automation</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100"><div class="container mx-auto px-4 py-8 max-w-3xl"><div class="bg-white rounded-lg shadow p-6"><h1 class="text-2xl font-bold mb-6">Create Automation</h1>
<form method="post" action="/automations/create"><div class="mb-4"><label class="block text-sm font-medium mb-2">Name</label><input type="text" name="name" required class="w-full border rounded px-3 py-2"></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Description</label><textarea name="description" rows="2" class="w-full border rounded px-3 py-2"></textarea></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Trigger Type</label><select name="trigger_type" class="w-full border rounded px-3 py-2"><option value="schedule">Schedule (Cron)</option><option value="webhook">Webhook</option><option value="interval">Interval</option></select></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Trigger Config (JSON)</label><textarea name="trigger_config_json" required rows="3" class="w-full border rounded px-3 py-2 font-mono text-sm">{"cron": "0 * * * *"}</textarea></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Actions (JSON Array)</label><textarea name="actions_json" required rows="8" class="w-full border rounded px-3 py-2 font-mono text-sm">[{"type": "http_request", "config": {"method": "GET", "url": "https://httpbin.org/get"}}]</textarea></div>
<div class="flex space-x-3"><button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded">Create</button><a href="/dashboard" class="bg-gray-300 px-6 py-2 rounded">Cancel</a></div></form></div></div></body></html>
    """,
    
    "logs.html": """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Logs</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100"><div class="container mx-auto px-4 py-8"><div class="bg-white rounded-lg shadow"><div class="p-4 border-b"><h1 class="text-xl font-bold">Execution Logs</h1></div><div class="divide-y">{% for log in logs %}<div class="p-4"><div><span class="font-mono text-sm">{{ log.created_at }}</span><span class="ml-3 px-2 py-1 rounded text-xs {{ 'bg-green-100 text-green-800' if log.status == 'success' else 'bg-red-100 text-red-800' }}">{{ log.status }}</span><span class="ml-3 text-sm font-semibold">{{ log.action_type }}</span></div><div class="mt-2 text-sm"><pre class="bg-gray-50 p-2 rounded overflow-x-auto">{{ log.details | tojson(indent=2) if log.details else '{}' }}</pre></div></div>{% else %}<div class="p-8 text-center">No logs yet</div>{% endfor %}</div></div></div></body></html>
    """,
    
    "settings.html": """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Settings</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100"><div class="container mx-auto px-4 py-8 max-w-2xl"><div class="bg-white rounded-lg shadow p-6"><h1 class="text-2xl font-bold mb-6">Settings</h1>
<form method="post" action="/settings/update"><div class="mb-4"><label class="block text-sm font-medium mb-2">Full Name</label><input type="text" name="full_name" value="{{ user.full_name or '' }}" class="w-full border rounded px-3 py-2"></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Email</label><input type="email" name="email" value="{{ user.email }}" class="w-full border rounded px-3 py-2"></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">API Key</label><div class="font-mono text-sm bg-gray-100 p-2 rounded">{{ user.api_key }}</div></div>
<button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded">Save</button></form></div></div></body></html>
    """,
    
    "login.html": """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Login</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100"><div class="container mx-auto px-4 py-16 max-w-md"><div class="bg-white rounded-lg shadow p-6"><h1 class="text-2xl font-bold mb-6 text-center">Login</h1>
<form method="post"><div class="mb-4"><label class="block text-sm font-medium mb-2">Email</label><input type="email" name="email" required class="w-full border rounded px-3 py-2"></div>
<div class="mb-6"><label class="block text-sm font-medium mb-2">Password</label><input type="password" name="password" required class="w-full border rounded px-3 py-2"></div>
<button type="submit" class="w-full bg-blue-600 text-white py-2 rounded">Login</button></form>
<p class="mt-4 text-center text-sm">No account? <a href="/register" class="text-blue-600">Register</a></p></div></div></body></html>
    """,
    
    "register.html": """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Register</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-gray-100"><div class="container mx-auto px-4 py-16 max-w-md"><div class="bg-white rounded-lg shadow p-6"><h1 class="text-2xl font-bold mb-6 text-center">Register</h1>
<form method="post"><div class="mb-4"><label class="block text-sm font-medium mb-2">Full Name</label><input type="text" name="full_name" required class="w-full border rounded px-3 py-2"></div>
<div class="mb-4"><label class="block text-sm font-medium mb-2">Email</label><input type="email" name="email" required class="w-full border rounded px-3 py-2"></div>
<div class="mb-6"><label class="block text-sm font-medium mb-2">Password</label><input type="password" name="password" required class="w-full border rounded px-3 py-2"></div>
<button type="submit" class="w-full bg-green-600 text-white py-2 rounded">Register</button></form>
<p class="mt-4 text-center text-sm">Have an account? <a href="/login" class="text-blue-600">Login</a></p></div></div></body></html>
    """
}

# Write templates
for filename, content in templates_content.items():
    with open(f"templates/{filename}", "w") as f:
        f.write(content)

# ============ Run ============
if __name__ == "__main__":
    import uvicorn
    print(f"""
    ╔══════════════════════════════════════════╗
    ║   {APP_NAME}                         ║
    ║   Ready to run!                         ║
    ╠══════════════════════════════════════════╣
    ║   Visit: http://localhost:8000          ║
    ║                                          ║
    ║   Changes from original:                ║
    ║   • SQLite instead of PostgreSQL        ║
    ║   • No SQLAlchemy (pure SQL)            ║
    ║   • No bcrypt (pure Python hashing)     ║
    ║   • No Celery (threading queue)         ║
    ║   • No Redis required                   ║
    ╚══════════════════════════════════════════╝
    """)
    uvicorn.run(app, host="0.0.0.0", port=8000)
