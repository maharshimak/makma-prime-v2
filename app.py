"""Mak'ma local API. The application factory makes isolated testing possible."""
import asyncio
import base64
import binascii
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import psutil
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from makma.body_limits import BoundedBodyMiddleware
from makma.config import settings
from makma import memory
from makma.store import get_settings, patch_settings, emit, rows, backup, export_data
from makma.policy import pending_approvals, claim_approval, get_approval, resolve_approval
from makma.tools import execute_tool, get_system_status, tool_catalog
from makma.runtime import Runtime, mission_detail, add_step, attention
from makma.brain import MakmaBrain

BASE = Path(__file__).resolve().parent
log = logging.getLogger('makma.api')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ChatIn(Input):
    message: str = Field(min_length=1, max_length=30000)
    conversation_id: str = Field(default='default', min_length=1, max_length=100, pattern=r'^[\w-]+$')


class VisionIn(Input):
    image: str = Field(max_length=12_000_000)
    prompt: str = Field(default='Describe this screen and suggest the next action.', max_length=10000)

    @field_validator('image')
    @classmethod
    def image_data(cls, value):
        prefix, separator, encoded = value.partition(',')
        if prefix not in {'data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64'} or not separator:
            raise ValueError('Expected a base64 PNG, JPEG or WebP image.')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('Invalid base64 image.')
        valid = (prefix.startswith('data:image/png') and data.startswith(b'\x89PNG\r\n\x1a\n')) or (prefix.startswith('data:image/jpeg') and data.startswith(b'\xff\xd8\xff')) or (prefix.startswith('data:image/webp') and data.startswith(b'RIFF') and data[8:12] == b'WEBP')
        if not valid:
            raise ValueError('Image contents do not match the declared format.')
        return value


class MemoryIn(Input):
    content: str = Field(min_length=1, max_length=30000)
    kind: str = Field(default='semantic', min_length=1, max_length=40)


class MissionIn(Input):
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=10000)
    priority: Literal['low', 'normal', 'high', 'critical'] = 'normal'


class MissionPatch(Input):
    status: Literal['draft', 'active', 'paused', 'waiting', 'blocked', 'needs_approval', 'completed', 'failed', 'cancelled'] | None = None
    progress: int | None = Field(default=None, ge=0, le=100)


class StepIn(Input):
    title: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=80)
    args: dict = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list, max_length=100)


class ToolIn(Input):
    args: dict = Field(default_factory=dict)


class TaskIn(Input):
    title: str = Field(min_length=1, max_length=300)
    priority: Literal['low', 'normal', 'high', 'critical'] = 'normal'
    due_at: datetime | None = None


class TaskPatch(Input):
    status: Literal['pending', 'completed']


class ScheduleIn(Input):
    title: str = Field(min_length=1, max_length=300)
    run_at: datetime
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)

    @field_validator('run_at')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError('Include a timezone offset.')
        return value


class Preferences(Input):
    name: str | None = Field(default=None, max_length=100)
    autonomy: Literal['manual', 'assisted', 'autonomous', 'lockdown'] | None = None
    simulation: bool | None = None
    speak: bool | None = None
    reduced_motion: bool | None = None
    provider: Literal['offline', 'openai'] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=120)
    allowed_directories: list[str] | None = Field(default=None, max_length=20)


def checked(result):
    if isinstance(result, dict) and result.get('ok') is False:
        raise HTTPException(400, result.get('error', 'Operation failed.'))
    return result


def utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(422, 'Include a timezone offset.')
    return value.astimezone(timezone.utc).isoformat()


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        memory.init_db()
        app.state.runtime = Runtime()
        app.state.brain = MakmaBrain(control=app.state.runtime.control)
        app.state.token = secrets.token_urlsafe(32)
        app.state.started = time.monotonic()
        app.state.chats = set()
        await app.state.runtime.start()
        yield
        for task in list(app.state.chats):
            task.cancel()
        await app.state.runtime.close()
        close = getattr(app.state.brain, 'close', None)
        if close:
            await close()

    app = FastAPI(title="Mak'ma Prime", version='3.0.0', lifespan=lifespan)
    app.add_middleware(BoundedBodyMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]', 'testserver'])
    app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

    def origin_allowed(headers):
        origin = headers.get('origin')
        return not origin or origin == f"http://{headers.get('host')}"

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        if not origin_allowed(request.headers) or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Cross-origin requests are not allowed.'}, status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not secrets.compare_digest(request.headers.get('x-makma-token', ''), getattr(app.state, 'token', '-')):
                return JSONResponse({'detail': 'A valid local session token is required.'}, status_code=403)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.url.path not in {'/api/settings', '/api/stop'} and get_settings()['autonomy'] == 'lockdown':
            return JSONResponse({'detail': 'Lockdown is read-only. Change autonomy in Settings to enable actions.'}, status_code=403)
        try:
            length = int(request.headers.get('content-length', '0') or '0')
        except ValueError:
            return JSONResponse({'detail': 'Invalid Content-Length.'}, status_code=400)
        if length > 13_000_000:
            return JSONResponse({'detail': 'Request is too large.'}, status_code=413)
        request_id = secrets.token_hex(8)
        response = await call_next(request)
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control'] = 'no-store'
        log.info(json.dumps({'request_id': request_id, 'method': request.method, 'path': request.url.path, 'status': response.status_code}))
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.get('/')
    def home():
        return FileResponse(BASE / 'static' / 'index.html')

    @app.get('/api/session')
    def session():
        return {'token': app.state.token}

    @app.get('/health')
    @app.get('/api/health')
    def health():
        with memory._conn() as conn:
            db_ok = conn.execute('SELECT 1').fetchone()[0] == 1
        prefs = get_settings()
        ready = bool(settings.api_key and settings.api_key != 'your_openai_api_key_here' and prefs['provider'] == 'openai')
        return {'ok': db_ok, 'version': '3.0.0', 'name': "Mak'ma", 'brain_ready': ready,
                'model': prefs['model'], 'provider': prefs['provider'],
                'model_status': ('connected' if app.state.brain.model_verified else 'configured; connection unverified') if ready else 'offline; local commands available',
                'database': 'online', 'scheduler': 'online' if app.state.runtime.scheduler and not app.state.runtime.scheduler.done() else 'offline',
                'voice': 'browser dependent', 'vision': 'provider required', 'browser': 'launch only',
                'uptime_seconds': round(time.monotonic() - app.state.started)}

    @app.get('/api/status')
    def status():
        from makma.agents import AGENTS
        data = get_system_status()
        data.update(missions=memory.list_missions(status='active', limit=8), approvals=pending_approvals(8),
                    agents=AGENTS, process_count=len(psutil.pids()), attention=attention())
        return data

    @app.get('/api/system')
    def system():
        return get_system_status()

    @app.get('/api/attention')
    def needs_attention():
        return {'items': attention()}

    @app.post('/api/chat')
    async def chat(body: ChatIn):
        task = asyncio.current_task()
        app.state.chats.add(task)
        try:
            return {'reply': await app.state.brain.ask(body.message, body.conversation_id)}
        finally:
            app.state.chats.discard(task)

    @app.post('/api/chat/stream')
    async def chat_stream(body: ChatIn, request: Request):
        async def events():
            task = asyncio.current_task()
            app.state.chats.add(task)
            try:
                async for event in app.state.brain.stream(body.message, body.conversation_id):
                    if await request.is_disconnected():
                        break
                    yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
            except asyncio.CancelledError:
                yield 'data: ' + json.dumps({'type': 'error', 'message': 'Stopped. Already dispatched actions may finish.'}) + '\n\n'
            finally:
                app.state.chats.discard(task)
        return StreamingResponse(events(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/stop')
    async def stop():
        current = asyncio.current_task()
        for task in list(app.state.chats):
            if task != current:
                task.cancel()
        return await app.state.runtime.stop_all()

    @app.post('/api/vision')
    async def vision(body: VisionIn):
        if not health()['brain_ready']:
            raise HTTPException(503, 'Configure an API key and model to analyze images. Screen sharing stays under your control.')
        return {'reply': await app.state.brain.analyze_image(body.image, body.prompt)}

    @app.get('/api/messages')
    def messages(conversation_id: str = 'default'):
        with memory._conn() as conn:
            return {'items': [dict(r) for r in conn.execute('SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 100) ORDER BY id', (conversation_id,))]}

    @app.post('/api/reset')
    async def reset():
        app.state.brain.reset()
        return {'ok': True}

    @app.get('/api/memory')
    def memories(query: str = '', limit: int = 50, kind: str | None = None):
        return {'items': memory.recall(query, min(100, max(1, limit)), category=kind)}

    @app.post('/api/memory')
    def remember(body: MemoryIn):
        return checked(memory.remember(body.content, body.kind))

    @app.patch('/api/memory/{memory_id}')
    def edit_memory(memory_id: int, body: MemoryIn):
        return checked(memory.update_memory(memory_id, body.content, body.kind))

    @app.delete('/api/memory/{memory_id}')
    def forget(memory_id: int):
        return checked(memory.forget(memory_id))

    @app.get('/api/missions')
    def missions():
        return {'items': memory.list_missions(limit=100)}

    @app.post('/api/missions')
    def create_mission(body: MissionIn):
        return checked(memory.create_mission(**body.model_dump()))

    @app.get('/api/missions/{mission_id}')
    def detail(mission_id: int):
        return mission_detail(mission_id)

    @app.patch('/api/missions/{mission_id}')
    def edit_mission(mission_id: int, body: MissionPatch):
        detail = mission_detail(mission_id)
        if body.status == 'completed' and any(s['status'] != 'completed' for s in detail['steps']):
            raise HTTPException(409, 'Executable steps must finish before this mission can be completed.')
        if body.status in {'paused', 'cancelled'}:
            return app.state.runtime.pause(mission_id, body.status == 'cancelled')
        return checked(memory.update_mission(mission_id, **body.model_dump(exclude_none=True)))

    @app.post('/api/missions/{mission_id}/steps')
    def step(mission_id: int, body: StepIn):
        return add_step(mission_id, **body.model_dump())

    @app.post('/api/missions/{mission_id}/run')
    async def run(mission_id: int):
        return app.state.runtime.launch(mission_id)

    @app.post('/api/missions/{mission_id}/pause')
    async def pause(mission_id: int):
        return app.state.runtime.pause(mission_id)

    @app.post('/api/missions/{mission_id}/cancel')
    async def cancel(mission_id: int):
        return app.state.runtime.pause(mission_id, True)

    @app.get('/api/tools')
    def tools():
        return {'items': tool_catalog()}

    @app.post('/api/tools/{name}')
    async def tool(name: str, body: ToolIn):
        return await asyncio.to_thread(execute_tool, name, body.args)

    @app.get('/api/agents')
    def agents():
        from makma.agents import AGENTS
        return {'items': AGENTS}

    @app.get('/api/approvals')
    def approvals():
        return {'items': pending_approvals()}

    @app.post('/api/approvals/{approval_id}/approve')
    async def approve(approval_id: int):
        item = claim_approval(approval_id)
        if not item:
            raise HTTPException(409, 'Approval is no longer pending.')
        result = await asyncio.to_thread(execute_tool, item['tool_name'], item['args'], True, 'approval:' + str(approval_id))
        resolve_approval(approval_id, 'approved' if result.get('ok') else 'failed', result)
        return {'ok': bool(result.get('ok')), 'result': result}

    @app.post('/api/approvals/{approval_id}/reject')
    def reject(approval_id: int):
        if not resolve_approval(approval_id, 'rejected', {'ok': False, 'rejected': True}):
            raise HTTPException(409, 'Approval is no longer pending.')
        return {'ok': True}

    @app.get('/api/tasks')
    def tasks():
        return {'items': rows('tasks')}

    @app.post('/api/tasks')
    def create_task(body: TaskIn):
        with memory._conn() as conn:
            tid = conn.execute('INSERT INTO tasks(title,priority,due_at) VALUES (?,?,?)', (body.title, body.priority, utc(body.due_at))).lastrowid
        emit('TASK_CREATED', body.title)
        return {'ok': True, 'id': tid}

    @app.patch('/api/tasks/{task_id}')
    def update_task(task_id: int, body: TaskPatch):
        with memory._conn() as conn:
            count = conn.execute('UPDATE tasks SET status=? WHERE id=?', (body.status, task_id)).rowcount
        if not count:
            raise HTTPException(404, 'Task not found.')
        return {'ok': True}

    @app.get('/api/schedules')
    def schedules():
        return {'items': rows('schedules')}

    @app.post('/api/schedules')
    def create_schedule(body: ScheduleIn):
        with memory._conn() as conn:
            sid = conn.execute('INSERT INTO schedules(title,run_at,interval_seconds) VALUES (?,?,?)', (body.title, utc(body.run_at), body.interval_seconds)).lastrowid
        return {'ok': True, 'id': sid}

    @app.delete('/api/schedules/{schedule_id}')
    def cancel_schedule(schedule_id: int):
        with memory._conn() as conn:
            conn.execute("UPDATE schedules SET status='cancelled' WHERE id=?", (schedule_id,))
        return {'ok': True}

    @app.get('/api/notifications')
    def notifications():
        return {'items': rows('notifications')}

    @app.post('/api/notifications/{notification_id}/dismiss')
    def dismiss(notification_id: int):
        with memory._conn() as conn:
            conn.execute("UPDATE notifications SET status='dismissed' WHERE id=?", (notification_id,))
        return {'ok': True}

    @app.get('/api/events')
    def events():
        return {'items': rows('events')}

    @app.get('/api/audit')
    def audit():
        return {'items': rows('audit_log')}

    @app.get('/api/settings')
    def preferences():
        return get_settings()

    @app.patch('/api/settings')
    def update_settings(body: Preferences):
        return patch_settings(body.model_dump(exclude_none=True))

    @app.get('/api/export')
    def export():
        return JSONResponse(export_data(), headers={'Content-Disposition': 'attachment; filename="makma-export.json"'})

    @app.post('/api/backup')
    def save_backup():
        return {'ok': True, 'path': str(backup())}

    @app.get('/api/integrations')
    def integrations():
        return {'items': [], 'message': 'No external services connected.'}

    @app.websocket('/ws')
    async def websocket(ws: WebSocket):
        if not origin_allowed(ws.headers):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            auth = await asyncio.wait_for(ws.receive_json(), 5)
            if not secrets.compare_digest(str(auth.get('token', '')), app.state.token):
                await ws.close(code=1008)
                return
            cursor = 0
            while True:
                with memory._conn() as conn:
                    records = [dict(r) for r in conn.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100', (cursor,))]
                for record in records:
                    await ws.send_json({'type': 'event', **record})
                    cursor = record['id']
                await ws.send_json({'type': 'heartbeat', 'active_missions': list(app.state.runtime.jobs)})
                await asyncio.sleep(2)
        except (WebSocketDisconnect, TimeoutError, RuntimeError):
            pass
    return app


app = create_app()

if __name__ == '__main__':
    if settings.host not in {'127.0.0.1', 'localhost', '::1'}:
        raise SystemExit('Makma must bind to loopback. Remote access is not implemented.')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)

