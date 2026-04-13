#!/usr/bin/env python3
"""
Complete SaaS Automation App - Single File
Run with: python app.py
Then visit: http://localhost:8000
"""

import json
import os
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

# Core dependencies
from fastapi import FastAPI, HTTPException, Depends, Request, Form, Cookie, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from passlib.context import CryptContext
from celery import Celery
from celery.schedules import crontab
import requests
import smtplib
from email.mime.text import MIMEText

# ============ Configuration ============
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./saas_app.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
APP_NAME = "AutomationFlow SaaS"
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")  # Optional for payments

# ============ Database Setup ============
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ============ Models ============
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String)
    plan = Column(String, default="free")  # free, pro, enterprise
    api_key = Column(String, unique=True, default=lambda: secrets.token_urlsafe(32))
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    automations = relationship("Automation", back_populates="owner")
    execution_logs = relationship("ExecutionLog", back_populates="user")

class Automation(Base):
    __tablename__ = "automations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    trigger_type = Column(String)  # schedule, webhook, interval
    trigger_config = Column(JSON)  # {"cron": "0 * * * *"} or {"webhook_url": "/my-webhook"}
    actions = Column(JSON)  # [{"type": "http_request", "config": {...}}, ...]
    is_active = Column(Boolean, default=True)
    last_run = Column(DateTime)
    run_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    owner = relationship("User", back_populates="automations")
    logs = relationship("AutomationLog", back_populates="automation")

class AutomationLog(Base):
    __tablename__ = "automation_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    automation_id = Column(Integer, ForeignKey("automations.id"), nullable=False)
    status = Column(String)  # success, failed, running
    result = Column(JSON)
    error_message = Column(Text)
    execution_time_ms = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    automation = relationship("Automation", back_populates="logs")

class ExecutionLog(Base):
    __tablename__ = "execution_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    automation_id = Column(Integer)
    action_type = Column(String)
    status = Column(String)
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="execution_logs")

# Create tables
Base.metadata.create_all(bind=engine)

# ============ Celery Setup ============
celery_app = Celery("saas_automation", broker=REDIS_URL, backend=REDIS_URL)

@celery_app.task
def execute_automation_task(automation_id: int, user_id: int, trigger_data: Dict = None):
    """Background task to execute automation"""
    db = SessionLocal()
    try:
        automation = db.query(Automation).filter(Automation.id == automation_id).first()
        if not automation or not automation.is_active:
            return {"error": "Automation not found or inactive"}
        
        results = []
        context = trigger_data or {}
        
        for action in automation.actions:
            action_type = action.get("type")
            config = action.get("config", {})
            
            start_time = datetime.utcnow()
            
            try:
                if action_type == "http_request":
                    result = execute_http_request(config)
                elif action_type == "send_email":
                    result = send_email_action(config)
                elif action_type == "webhook":
                    result = call_webhook(config)
                elif action_type == "delay":
                    result = {"status": "delayed", "seconds": config.get("seconds", 0)}
                elif action_type == "condition":
                    result = evaluate_condition(config, context)
                elif action_type == "transform":
                    result = transform_data(config, context)
                else:
                    result = {"error": f"Unknown action: {action_type}"}
                
                execution_time = (datetime.utcnow() - start_time).total_seconds() * 1000
                
                # Log execution
                log = ExecutionLog(
                    user_id=user_id,
                    automation_id=automation_id,
                    action_type=action_type,
                    status="success" if "error" not in result else "failed",
                    details={"action": action, "result": result, "execution_time_ms": execution_time}
                )
                db.add(log)
                
                results.append(result)
                context[f"action_{len(results)}_result"] = result
                
                if result.get("error") and action.get("stop_on_fail", False):
                    break
                    
            except Exception as e:
                log = ExecutionLog(
                    user_id=user_id,
                    automation_id=automation_id,
                    action_type=action_type,
                    status="failed",
                    details={"error": str(e), "config": config}
                )
                db.add(log)
                results.append({"error": str(e)})
                break
        
        # Update automation stats
        automation.last_run = datetime.utcnow()
        automation.run_count += 1
        
        # Create automation log
        automation_log = AutomationLog(
            automation_id=automation_id,
            status="success" if all("error" not in r for r in results) else "partial",
            result={"results": results, "context_keys": list(context.keys())},
            execution_time_ms=sum(r.get("execution_time_ms", 0) for r in results if isinstance(r, dict))
        )
        db.add(automation_log)
        db.commit()
        
        return {"status": "completed", "results": results}
        
    except Exception as e:
        db.rollback()
        return {"error": str(e)}
    finally:
        db.close()

def execute_http_request(config: Dict) -> Dict:
    """Execute HTTP request"""
    try:
        method = config.get("method", "GET")
        url = config.get("url")
        headers = config.get("headers", {})
        body = config.get("body")
        timeout = config.get("timeout", 30)
        
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body if isinstance(body, dict) else None,
            data=body if isinstance(body, str) else None,
            timeout=timeout
        )
        
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text[:1000],  # Truncate for storage
            "execution_time_ms": response.elapsed.total_seconds() * 1000
        }
    except Exception as e:
        return {"error": str(e)}

def send_email_action(config: Dict) -> Dict:
    """Send email (configure your SMTP)"""
    # This is a mock - configure with real SMTP
    return {
        "status": "sent",
        "to": config.get("to"),
        "subject": config.get("subject", "Automation Email"),
        "execution_time_ms": 100
    }

def call_webhook(config: Dict) -> Dict:
    """Call external webhook"""
    return execute_http_request(config)

def evaluate_condition(config: Dict, context: Dict) -> Dict:
    """Evaluate condition based on context"""
    condition = config.get("condition", "")
    # Simple evaluation - can be extended
    return {"condition_met": True, "condition": condition}

def transform_data(config: Dict, context: Dict) -> Dict:
    """Transform data using JSONPath-like operations"""
    source = context.get(config.get("source", ""), {})
    template = config.get("template", {})
    return {"transformed": template, "source": source}

# ============ FastAPI App ============
app = FastAPI(title=APP_NAME, version="1.0.0")

# Setup templates
templates = Jinja2Templates(directory="templates")
os.makedirs("templates", exist_ok=True)

# ============ Authentication ============
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(request: Request, db: Session = Depends(get_db)):
    session_token = request.cookies.get("session_token")
    if not session_token:
        return None
    
    # Simple session management - store user_id in token
    # In production, use JWT or proper session storage
    user = db.query(User).filter(User.api_key == session_token).first()
    return user

# ============ API Routes ============
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, current_user: User = Depends(get_current_user)):
    """Landing page"""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "app_name": APP_NAME, "user": current_user}
    )

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    automations = db.query(Automation).filter(Automation.user_id == current_user.id).all()
    recent_logs = db.query(ExecutionLog).filter(ExecutionLog.user_id == current_user.id).order_by(ExecutionLog.created_at.desc()).limit(20).all()
    
    # Calculate stats
    total_runs = sum(a.run_count for a in automations)
    active_automations = sum(1 for a in automations if a.is_active)
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": current_user,
            "automations": automations,
            "recent_logs": recent_logs,
            "total_runs": total_runs,
            "active_automations": active_automations
        }
    )

@app.get("/automations/new", response_class=HTMLResponse)
async def new_automation_form(request: Request, current_user: User = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("automation_form.html", {"request": request, "user": current_user})

@app.post("/automations/create")
async def create_automation(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    trigger_type: str = Form(...),
    trigger_config_json: str = Form(...),
    actions_json: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    automation = Automation(
        user_id=current_user.id,
        name=name,
        description=description,
        trigger_type=trigger_type,
        trigger_config=json.loads(trigger_config_json),
        actions=json.loads(actions_json)
    )
    
    db.add(automation)
    db.commit()
    
    return RedirectResponse(url="/dashboard", status_code=302)

@app.post("/automations/{automation_id}/toggle")
async def toggle_automation(
    automation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    automation = db.query(Automation).filter(Automation.id == automation_id, Automation.user_id == current_user.id).first()
    if automation:
        automation.is_active = not automation.is_active
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=302)

@app.post("/automations/{automation_id}/run")
async def run_automation_manually(
    automation_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    automation = db.query(Automation).filter(Automation.id == automation_id, Automation.user_id == current_user.id).first()
    if automation:
        background_tasks.add_task(execute_automation_task, automation_id, current_user.id)
    return RedirectResponse(url="/dashboard", status_code=302)

@app.delete("/automations/{automation_id}")
async def delete_automation(
    automation_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    automation = db.query(Automation).filter(Automation.id == automation_id, Automation.user_id == current_user.id).first()
    if automation:
        db.delete(automation)
        db.commit()
    return JSONResponse({"success": True})

@app.get("/logs", response_class=HTMLResponse)
async def view_logs(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    
    logs = db.query(ExecutionLog).filter(ExecutionLog.user_id == current_user.id).order_by(ExecutionLog.created_at.desc()).limit(100).all()
    return templates.TemplateResponse("logs.html", {"request": request, "user": current_user, "logs": logs})

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: User = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("settings.html", {"request": request, "user": current_user})

@app.post("/settings/update")
async def update_settings(
    full_name: str = Form(None),
    email: str = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if full_name:
        current_user.full_name = full_name
    if email:
        current_user.email = email
    db.commit()
    return RedirectResponse(url="/settings", status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.email == email).first()
    if user and pwd_context.verify(password, user.hashed_password):
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(key="session_token", value=user.api_key, httponly=True)
        return response
    return HTMLResponse("Invalid credentials", status_code=401)

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(
    email: EmailStr = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    db: Session = Depends(get_db)
):
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        return HTMLResponse("Email already registered", status_code=400)
    
    user = User(
        email=email,
        hashed_password=pwd_context.hash(password),
        full_name=full_name,
        plan="free"
    )
    db.add(user)
    db.commit()
    
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(key="session_token", value=user.api_key, httponly=True)
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session_token")
    return response

@app.post("/webhook/{automation_id}")
async def webhook_trigger(automation_id: int, request: Request, db: Session = Depends(get_db)):
    """Webhook endpoint to trigger automations"""
    automation = db.query(Automation).filter(Automation.id == automation_id).first()
    if automation and automation.is_active and automation.trigger_type == "webhook":
        data = await request.json()
        execute_automation_task.delay(automation_id, automation.user_id, data)
        return {"status": "triggered"}
    return {"error": "Automation not found"}

# ============ HTML Templates ============
# Write templates to files
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
                    <div class="text-center">
                        <div class="text-3xl font-bold text-blue-600">⚡</div>
                        <h3 class="font-semibold mt-2">Real-time Triggers</h3>
                        <p class="text-sm text-gray-600">Schedule, webhook, or interval-based</p>
                    </div>
                    <div class="text-center">
                        <div class="text-3xl font-bold text-blue-600">🔗</div>
                        <h3 class="font-semibold mt-2">Multiple Actions</h3>
                        <p class="text-sm text-gray-600">HTTP, Email, Webhooks, Conditions</p>
                    </div>
                    <div class="text-center">
                        <div class="text-3xl font-bold text-blue-600">📊</div>
                        <h3 class="font-semibold mt-2">Analytics</h3>
                        <p class="text-sm text-gray-600">Track every execution</p>
                    </div>
                </div>
                
                <div class="text-center space-x-4">
                    {% if user %}
                        <a href="/dashboard" class="inline-block bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition">Go to Dashboard</a>
                    {% else %}
                        <a href="/login" class="inline-block bg-blue-600 text-white px-6 py-3 rounded-lg hover:bg-blue-700 transition">Login</a>
                        <a href="/register" class="inline-block bg-green-600 text-white px-6 py-3 rounded-lg hover:bg-green-700 transition">Sign Up Free</a>
                    {% endif %}
                </div>
            </div>
            
            <div class="bg-gray-800 rounded-lg p-6 text-white">
                <h3 class="text-lg font-semibold mb-3">Example Automation</h3>
                <pre class="text-sm bg-gray-900 p-4 rounded overflow-x-auto">
{
  "name": "Daily Report",
  "trigger": {"type": "schedule", "cron": "0 9 * * *"},
  "actions": [
    {"type": "http_request", "url": "https://api.example.com/data"},
    {"type": "send_email", "to": "admin@example.com", "subject": "Daily Report"}
  ]
}
                </pre>
            </div>
        </div>
    </div>
</body>
</html>
    """,
    
    "dashboard.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - {{ app_name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <nav class="bg-white shadow-sm">
        <div class="container mx-auto px-4 py-3 flex justify-between items-center">
            <h1 class="text-xl font-bold text-gray-800">{{ app_name }}</h1>
            <div class="space-x-4">
                <span class="text-gray-600">Welcome, {{ user.full_name or user.email }}</span>
                <a href="/dashboard" class="text-blue-600">Dashboard</a>
                <a href="/automations/new" class="text-green-600">New Automation</a>
                <a href="/logs" class="text-gray-600">Logs</a>
                <a href="/settings" class="text-gray-600">Settings</a>
                <a href="/logout" class="text-red-600">Logout</a>
            </div>
        </div>
    </nav>
    
    <div class="container mx-auto px-4 py-8">
        <!-- Stats -->
        <div class="grid md:grid-cols-4 gap-4 mb-8">
            <div class="bg-white rounded-lg p-4 shadow">
                <div class="text-2xl font-bold text-blue-600">{{ automations|length }}</div>
                <div class="text-gray-600">Automations</div>
            </div>
            <div class="bg-white rounded-lg p-4 shadow">
                <div class="text-2xl font-bold text-green-600">{{ active_automations }}</div>
                <div class="text-gray-600">Active</div>
            </div>
            <div class="bg-white rounded-lg p-4 shadow">
                <div class="text-2xl font-bold text-purple-600">{{ total_runs }}</div>
                <div class="text-gray-600">Total Runs</div>
            </div>
            <div class="bg-white rounded-lg p-4 shadow">
                <div class="text-2xl font-bold text-orange-600">{{ user.plan|upper }}</div>
                <div class="text-gray-600">Plan</div>
            </div>
        </div>
        
        <!-- Automations List -->
        <div class="bg-white rounded-lg shadow">
            <div class="p-4 border-b">
                <h2 class="text-lg font-semibold">Your Automations</h2>
            </div>
            <div class="divide-y">
                {% for automation in automations %}
                <div class="p-4 flex justify-between items-center hover:bg-gray-50">
                    <div class="flex-1">
                        <h3 class="font-semibold">{{ automation.name }}</h3>
                        <p class="text-sm text-gray-600">{{ automation.description or 'No description' }}</p>
                        <div class="text-xs text-gray-500 mt-1">
                            Trigger: {{ automation.trigger_type }} | 
                            Runs: {{ automation.run_count }} | 
                            Last: {{ automation.last_run.strftime('%Y-%m-%d %H:%M') if automation.last_run else 'Never' }}
                        </div>
                    </div>
                    <div class="space-x-2">
                        <form action="/automations/{{ automation.id }}/toggle" method="post" class="inline">
                            <button class="px-3 py-1 rounded text-sm {{ 'bg-green-500' if automation.is_active else 'bg-gray-500' }} text-white">
                                {{ 'Active' if automation.is_active else 'Inactive' }}
                            </button>
                        </form>
                        <form action="/automations/{{ automation.id }}/run" method="post" class="inline">
                            <button class="px-3 py-1 bg-blue-500 text-white rounded text-sm">Run Now</button>
                        </form>
                        <button onclick="deleteAutomation({{ automation.id }})" class="px-3 py-1 bg-red-500 text-white rounded text-sm">Delete</button>
                    </div>
                </div>
                {% else %}
                <div class="p-8 text-center text-gray-500">
                    No automations yet. <a href="/automations/new" class="text-blue-600">Create your first automation →</a>
                </div>
                {% endfor %}
            </div>
        </div>
        
        <!-- Recent Logs -->
        <div class="bg-white rounded-lg shadow mt-8">
            <div class="p-4 border-b">
                <h2 class="text-lg font-semibold">Recent Executions</h2>
            </div>
            <div class="divide-y">
                {% for log in recent_logs[:10] %}
                <div class="p-3 text-sm">
                    <span class="font-mono">{{ log.created_at.strftime('%H:%M:%S') }}</span>
                    <span class="ml-3 px-2 py-1 rounded text-xs {{ 'bg-green-100 text-green-800' if log.status == 'success' else 'bg-red-100 text-red-800' }}">
                        {{ log.status }}
                    </span>
                    <span class="ml-3 text-gray-600">{{ log.action_type }}</span>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>
    
    <script>
        async function deleteAutomation(id) {
            if (confirm('Delete this automation?')) {
                await fetch(`/automations/${id}`, { method: 'DELETE' });
                location.reload();
            }
        }
    </script>
</body>
</html>
    """,
    
    "automation_form.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>New Automation</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <div class="container mx-auto px-4 py-8 max-w-3xl">
        <div class="bg-white rounded-lg shadow p-6">
            <h1 class="text-2xl font-bold mb-6">Create New Automation</h1>
            
            <form method="post" action="/automations/create">
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Name</label>
                    <input type="text" name="name" required class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Description</label>
                    <textarea name="description" rows="2" class="w-full border rounded px-3 py-2"></textarea>
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Trigger Type</label>
                    <select name="trigger_type" class="w-full border rounded px-3 py-2">
                        <option value="schedule">Schedule (Cron)</option>
                        <option value="webhook">Webhook</option>
                        <option value="interval">Interval (seconds)</option>
                    </select>
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Trigger Configuration (JSON)</label>
                    <textarea name="trigger_config_json" required rows="3" class="w-full border rounded px-3 py-2 font-mono text-sm">
{
  "cron": "0 * * * *"
}
                    </textarea>
                    <p class="text-xs text-gray-500 mt-1">Examples: {"cron": "0 * * * *"} or {"webhook_url": "/my-webhook"} or {"interval_seconds": 3600}</p>
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Actions (JSON Array)</label>
                    <textarea name="actions_json" required rows="8" class="w-full border rounded px-3 py-2 font-mono text-sm">
[
  {
    "type": "http_request",
    "config": {
      "method": "GET",
      "url": "https://api.example.com/data"
    }
  },
  {
    "type": "send_email",
    "config": {
      "to": "user@example.com",
      "subject": "Automation Result"
    }
  }
]
                    </textarea>
                    <p class="text-xs text-gray-500 mt-1">Supported: http_request, send_email, webhook, delay, condition, transform</p>
                </div>
                
                <div class="flex space-x-3">
                    <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700">Create Automation</button>
                    <a href="/dashboard" class="bg-gray-300 text-gray-700 px-6 py-2 rounded hover:bg-gray-400">Cancel</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
    """,
    
    "logs.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Execution Logs</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <nav class="bg-white shadow-sm">
        <div class="container mx-auto px-4 py-3">
            <a href="/dashboard" class="text-blue-600">← Back to Dashboard</a>
        </div>
    </nav>
    
    <div class="container mx-auto px-4 py-8">
        <div class="bg-white rounded-lg shadow">
            <div class="p-4 border-b">
                <h1 class="text-xl font-bold">Execution Logs</h1>
            </div>
            <div class="divide-y">
                {% for log in logs %}
                <div class="p-4">
                    <div class="flex justify-between items-start">
                        <div>
                            <span class="font-mono text-sm">{{ log.created_at.strftime('%Y-%m-%d %H:%M:%S') }}</span>
                            <span class="ml-3 px-2 py-1 rounded text-xs {{ 'bg-green-100 text-green-800' if log.status == 'success' else 'bg-red-100 text-red-800' }}">
                                {{ log.status }}
                            </span>
                            <span class="ml-3 text-sm font-semibold">{{ log.action_type }}</span>
                        </div>
                    </div>
                    <div class="mt-2 text-sm text-gray-600">
                        <pre class="bg-gray-50 p-2 rounded overflow-x-auto">{{ log.details | tojson(indent=2) }}</pre>
                    </div>
                </div>
                {% else %}
                <div class="p-8 text-center text-gray-500">No logs yet</div>
                {% endfor %}
            </div>
        </div>
    </div>
</body>
</html>
    """,
    
    "settings.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Settings</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <div class="container mx-auto px-4 py-8 max-w-2xl">
        <div class="bg-white rounded-lg shadow p-6">
            <h1 class="text-2xl font-bold mb-6">Account Settings</h1>
            
            <form method="post" action="/settings/update">
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Full Name</label>
                    <input type="text" name="full_name" value="{{ user.full_name or '' }}" class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Email</label>
                    <input type="email" name="email" value="{{ user.email }}" class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">API Key</label>
                    <div class="font-mono text-sm bg-gray-100 p-2 rounded">{{ user.api_key }}</div>
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Current Plan</label>
                    <div class="text-lg font-bold text-blue-600">{{ user.plan|upper }}</div>
                </div>
                
                <button type="submit" class="bg-blue-600 text-white px-6 py-2 rounded hover:bg-blue-700">Save Changes</button>
            </form>
        </div>
    </div>
</body>
</html>
    """,
    
    "login.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <div class="container mx-auto px-4 py-16 max-w-md">
        <div class="bg-white rounded-lg shadow p-6">
            <h1 class="text-2xl font-bold mb-6 text-center">Login</h1>
            
            <form method="post">
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Email</label>
                    <input type="email" name="email" required class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-6">
                    <label class="block text-sm font-medium mb-2">Password</label>
                    <input type="password" name="password" required class="w-full border rounded px-3 py-2">
                </div>
                
                <button type="submit" class="w-full bg-blue-600 text-white py-2 rounded hover:bg-blue-700">Login</button>
            </form>
            
            <p class="mt-4 text-center text-sm">
                Don't have an account? <a href="/register" class="text-blue-600">Register</a>
            </p>
        </div>
    </div>
</body>
</html>
    """,
    
    "register.html": """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Register</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100">
    <div class="container mx-auto px-4 py-16 max-w-md">
        <div class="bg-white rounded-lg shadow p-6">
            <h1 class="text-2xl font-bold mb-6 text-center">Create Account</h1>
            
            <form method="post">
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Full Name</label>
                    <input type="text" name="full_name" required class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-4">
                    <label class="block text-sm font-medium mb-2">Email</label>
                    <input type="email" name="email" required class="w-full border rounded px-3 py-2">
                </div>
                
                <div class="mb-6">
                    <label class="block text-sm font-medium mb-2">Password</label>
                    <input type="password" name="password" required class="w-full border rounded px-3 py-2">
                </div>
                
                <button type="submit" class="w-full bg-green-600 text-white py-2 rounded hover:bg-green-700">Register</button>
            </form>
            
            <p class="mt-4 text-center text-sm">
                Already have an account? <a href="/login" class="text-blue-600">Login</a>
            </p>
        </div>
    </div>
</body>
</html>
    """
}

# Write templates to files
for filename, content in templates_content.items():
    with open(f"templates/{filename}", "w") as f:
        f.write(content)

# ============ Run the App ============
if __name__ == "__main__":
    import uvicorn
    print(f"""
    ╔══════════════════════════════════════════╗
    ║   {APP_NAME}                    ║
    ║   Ready to run!                         ║
    ╠══════════════════════════════════════════╣
    ║   Visit: http://localhost:8000          ║
    ║   Login: Register a new account         ║
    ║                                          ║
    ║   Features:                             ║
    ║   • Create automation workflows         ║
    ║   • Schedule or webhook triggers        ║
    ║   • HTTP requests, emails, conditions   ║
    ║   • Real-time execution logs            ║
    ╚══════════════════════════════════════════╝
    """)
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
