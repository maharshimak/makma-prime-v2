"""Durable mission execution, local events and persistent reminder scheduling."""
import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from .memory import _conn, list_missions
from .store import emit
from .tools import execute_tool, validate_tool


def now():
    return datetime.now(timezone.utc).isoformat()


def mission_detail(mission_id):
    with _conn() as conn:
        row = conn.execute('SELECT * FROM missions WHERE id=?', (mission_id,)).fetchone()
        if not row:
            raise ValueError('Mission not found.')
        steps = [dict(r) for r in conn.execute('SELECT * FROM mission_steps WHERE mission_id=? ORDER BY id', (mission_id,))]
    for step in steps:
        step['args'] = json.loads(step.pop('args_json'))
        step['depends_on'] = json.loads(step.pop('depends_json'))
        step['result'] = json.loads(step.pop('result_json') or 'null')
    return {'mission': dict(row), 'steps': steps}


def add_step(mission_id, title, tool_name, args, depends_on):
    validate_tool(tool_name, args)
    with _conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        mission = conn.execute('SELECT status FROM missions WHERE id=?', (mission_id,)).fetchone()
        if not mission:
            raise ValueError('Mission not found.')
        if mission['status'] in {'completed', 'cancelled', 'failed'}:
            raise ValueError('Terminal missions cannot be edited.')
        if conn.execute("SELECT 1 FROM mission_steps WHERE mission_id=? AND status='running'", (mission_id,)).fetchone():
            raise ValueError('Pause this mission before editing its plan.')
        existing = {r['id'] for r in conn.execute('SELECT id FROM mission_steps WHERE mission_id=?', (mission_id,))}
        if not set(depends_on).issubset(existing):
            raise ValueError('Dependencies must be existing steps in this mission.')
        sid = conn.execute('INSERT INTO mission_steps(mission_id,title,tool_name,args_json,depends_json,idempotency_key) VALUES (?,?,?,?,?,?)',
                           (mission_id, title, tool_name, json.dumps(args), json.dumps(sorted(set(depends_on))), str(uuid.uuid4()))).lastrowid
    emit('MISSION_PLAN_CHANGED', f'Added step: {title}', {'mission_id': mission_id, 'step_id': sid})
    return {'ok': True, 'id': sid}


def attention():
    items = []
    with _conn() as conn:
        for r in conn.execute("SELECT id,tool_name FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT 5"):
            items.append({'priority': 9, 'title': 'Approval: ' + r['tool_name'], 'source': 'approval', 'id': r['id']})
        for r in conn.execute("SELECT id,title,status FROM missions WHERE status IN ('blocked','needs_approval','waiting') ORDER BY id DESC LIMIT 5"):
            items.append({'priority': 8, 'title': r['title'] + ' — ' + r['status'], 'source': 'mission', 'id': r['id']})
        for r in conn.execute("SELECT id,title,due_at FROM tasks WHERE status!='completed' AND due_at IS NOT NULL AND due_at<=? ORDER BY due_at LIMIT 5", ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),)):
            items.append({'priority': 7, 'title': r['title'], 'source': 'task', 'id': r['id'], 'due_at': r['due_at']})
        for r in conn.execute("SELECT id,title FROM notifications WHERE status='unread' ORDER BY id DESC LIMIT 5"):
            items.append({'priority': 6, 'title': r['title'], 'source': 'notification', 'id': r['id']})
    return sorted(items, key=lambda x: -x['priority'])[:8]


class Runtime:
    def __init__(self):
        self.jobs = {}
        self.scheduler = None
        self.stopping = False

    async def start(self):
        if self.scheduler and not self.scheduler.done():
            return
        self.stopping = False
        # A crash between dispatch and receipt has an unknown outcome. Never replay it.
        with _conn() as conn:
            conn.execute("UPDATE mission_steps SET status='uncertain' WHERE status='running'")
            conn.execute("UPDATE missions SET status='blocked' WHERE id IN (SELECT mission_id FROM mission_steps WHERE status='uncertain')")
            conn.execute("UPDATE approvals SET status='uncertain' WHERE status='executing'")
            conn.execute("UPDATE tool_runs SET status='uncertain' WHERE status='running'")
        self.scheduler = asyncio.create_task(self._scheduler())
        emit('CORE_STARTED', 'Mak\'ma Core is online. Unfinished actions require review.')

    async def close(self):
        self.stopping = True
        await self.stop_all()
        if self.scheduler:
            self.scheduler.cancel()
            await asyncio.gather(self.scheduler, return_exceptions=True)

    async def stop_all(self):
        for mid in list(self.jobs):
            self.pause(mid)
        tasks = list(self.jobs.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return {'ok': True, 'message': 'Stopped dispatching new actions. An already dispatched OS action may finish.'}

    def launch(self, mission_id):
        if self.stopping:
            raise ValueError('Runtime is stopping.')
        detail = mission_detail(mission_id)
        if mission_id in self.jobs and not self.jobs[mission_id].done():
            return {'ok': True, 'status': 'running'}
        if detail['mission']['status'] in {'completed', 'cancelled', 'failed'}:
            raise ValueError('Terminal missions cannot run.')
        if not detail['steps']:
            raise ValueError('Add an executable step before running this mission.')
        if any(s['status'] in {'uncertain', 'failed'} for s in detail['steps']):
            raise ValueError('Inspect failed or uncertain actions before creating a revised mission; they will not be replayed.')
        with _conn() as conn:
            conn.execute("UPDATE missions SET status='active' WHERE id=?", (mission_id,))
        self.jobs[mission_id] = asyncio.create_task(self._run(mission_id))
        return {'ok': True, 'status': 'running'}

    def pause(self, mission_id, cancel=False):
        mission_detail(mission_id)
        status = 'cancelled' if cancel else 'paused'
        with _conn() as conn:
            conn.execute("UPDATE missions SET status=? WHERE id=? AND status NOT IN ('completed','cancelled','failed')", (status, mission_id))
        task = self.jobs.get(mission_id)
        if task and not task.done():
            task.cancel()
        emit('MISSION_' + status.upper(), f'Mission {mission_id} {status}', {'mission_id': mission_id})
        return {'ok': True, 'status': status}

    async def control(self, action):
        if action in {'stop', 'pause'}:
            return await self.stop_all()
        missions = [m for m in list_missions() if m['status'] not in {'completed', 'failed', 'cancelled'}]
        if not missions:
            return {'ok': False, 'message': 'No unfinished mission to resume.'}
        return self.launch(missions[0]['id'])

    async def _run(self, mid):
        current = None
        try:
            while True:
                detail = mission_detail(mid)
                if detail['mission']['status'] != 'active':
                    return
                steps = detail['steps']
                reconciled = False
                for step in steps:
                    if step['status'] == 'needs_approval':
                        with _conn() as conn:
                            approval = conn.execute('SELECT status,result_json FROM approvals WHERE id=?', (step['approval_id'],)).fetchone()
                            if approval and approval['status'] == 'approved':
                                result = json.loads(approval['result_json'] or '{}')
                                state = 'simulated' if result.get('simulated') else 'completed' if result.get('ok') else 'failed'
                                conn.execute("UPDATE mission_steps SET status=?,result_json=? WHERE id=?", (state, approval['result_json'], step['id']))
                                reconciled = True
                            elif approval and approval['status'] in {'rejected', 'failed', 'uncertain'}:
                                state = 'uncertain' if approval['status'] == 'uncertain' else 'failed'
                                conn.execute("UPDATE mission_steps SET status=?,result_json=? WHERE id=?", (state, approval['result_json'], step['id']))
                                reconciled = True
                            elif not approval:
                                conn.execute("UPDATE mission_steps SET status='uncertain' WHERE id=?", (step['id'],))
                                reconciled = True
                if reconciled:
                    continue
                done = {s['id'] for s in steps if s['status'] == 'completed'}
                with _conn() as conn:
                    conn.execute('UPDATE missions SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', (int(len(done) / len(steps) * 100), mid))
                if len(done) == len(steps):
                    with _conn() as conn:
                        changed = conn.execute("UPDATE missions SET status='completed',progress=100,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'", (mid,)).rowcount
                    if changed:
                        emit('MISSION_COMPLETED', detail['mission']['title'], {'mission_id': mid})
                    return
                ready = next((s for s in steps if s['status'] == 'pending' and set(s['depends_on']).issubset(done)), None)
                if not ready:
                    state = 'needs_approval' if any(s['status'] == 'needs_approval' for s in steps) else 'blocked'
                    with _conn() as conn:
                        conn.execute("UPDATE missions SET status=? WHERE id=? AND status='active'", (state, mid))
                    return
                current = ready['id']
                with _conn() as conn:
                    claimed = conn.execute("""UPDATE mission_steps SET status='running' WHERE id=? AND status='pending'
                        AND EXISTS(SELECT 1 FROM missions WHERE id=? AND status='active')""", (current, mid)).rowcount
                if not claimed:
                    current = None
                    return
                emit('MISSION_STEP_STARTED', ready['title'], {'mission_id': mid, 'step_id': current})
                result = await asyncio.wait_for(asyncio.to_thread(execute_tool, ready['tool_name'], ready['args'], idempotency_key=ready['idempotency_key']), 30)
                state = 'needs_approval' if result.get('approval_required') else 'simulated' if result.get('simulated') else 'completed' if result.get('ok') else 'uncertain' if result.get('uncertain') else 'failed'
                with _conn() as conn:
                    conn.execute("UPDATE mission_steps SET status=?,result_json=?,approval_id=? WHERE id=? AND status='running'", (state, json.dumps(result), result.get('approval_id'), current))
                emit('MISSION_STEP_' + state.upper(), ready['title'], {'mission_id': mid, 'step_id': current})
                current = None
                # Pending approval or a failed branch must not stop independent work.
        except (asyncio.CancelledError, TimeoutError):
            if current:
                with _conn() as conn:
                    conn.execute("UPDATE mission_steps SET status='uncertain' WHERE id=? AND status='running'", (current,))
                    conn.execute("UPDATE missions SET status='blocked' WHERE id=? AND status!='cancelled'", (mid,))
                emit('MISSION_BLOCKED', 'An interrupted action needs review; it will not be repeated.', {'mission_id': mid})
        except Exception:
            with _conn() as conn:
                if current:
                    conn.execute("UPDATE mission_steps SET status='uncertain' WHERE id=?", (current,))
                conn.execute("UPDATE missions SET status='blocked' WHERE id=? AND status!='cancelled'", (mid,))
            emit('MISSION_BLOCKED', 'Mission execution failed; inspect its steps.', {'mission_id': mid})
        finally:
            if self.jobs.get(mid) is asyncio.current_task():
                self.jobs.pop(mid, None)

    def tick(self):
        instant = datetime.now(timezone.utc)
        fired = []
        with _conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT * FROM schedules WHERE status='active' AND run_at<=?", (instant.isoformat(),)).fetchall():
                conn.execute('INSERT INTO notifications(title,summary,priority) VALUES (?,?,?)', (row['title'], 'Scheduled reminder', 'important'))
                if row['interval_seconds']:
                    previous = datetime.fromisoformat(row['run_at'])
                    elapsed = (instant - previous).total_seconds()
                    next_run = previous + timedelta(seconds=(int(elapsed // row['interval_seconds']) + 1) * row['interval_seconds'])
                    conn.execute('UPDATE schedules SET run_at=? WHERE id=?', (next_run.isoformat(), row['id']))
                else:
                    conn.execute("UPDATE schedules SET status='completed' WHERE id=?", (row['id'],))
                fired.append(row['title'])
        for title in fired:
            emit('REMINDER_DUE', title)
        return len(fired)

    async def _scheduler(self):
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                emit('SCHEDULER_ERROR', 'Reminder delivery failed; retrying on the next tick.')
            await asyncio.sleep(2)
