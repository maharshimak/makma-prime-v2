"""Server-enforced authorization. An approval authorizes exactly one stored action."""
import json
from typing import Any
from .memory import _conn

TOOL_RISK = {
    'get_system_status': 0, 'top_processes': 0, 'list_files': 0,
    'read_text_file': 0, 'search_files': 0, 'recall': 0, 'list_missions': 0,
    'open_url': 1, 'open_app': 1, 'create_note': 1, 'create_folder': 1,
    'remember': 1, 'create_mission': 1, 'update_mission': 1,
    'type_text': 3, 'press_hotkey': 3, 'mouse_click': 3,
}


def risk_level(tool_name: str) -> int:
    return TOOL_RISK.get(tool_name, 4)


def decision(tool_name: str, approved: bool = False) -> str:
    from .store import get_settings
    mode = get_settings()['autonomy']
    risk = risk_level(tool_name)
    if mode == 'lockdown' and risk > 0:
        return 'deny'
    if approved:
        return 'allow'
    if risk >= 2 or (mode == 'manual' and risk > 0):
        return 'approve'
    return 'allow'


def create_approval(tool_name: str, args: dict[str, Any]) -> dict:
    from .store import emit
    encoded = json.dumps(args, sort_keys=True, ensure_ascii=False)
    with _conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute("SELECT id FROM approvals WHERE tool_name=? AND args_json=? AND status='pending'", (tool_name, encoded)).fetchone()
        if existing:
            aid = existing['id']
        else:
            aid = conn.execute('INSERT INTO approvals(tool_name,args_json,risk_level) VALUES (?,?,?)', (tool_name, encoded, risk_level(tool_name))).lastrowid
    if not existing:
        emit('APPROVAL_REQUIRED', f'Approval needed: {tool_name}', {'approval_id': aid})
    return {'ok': False, 'approval_required': True, 'approval_id': aid,
            'tool': tool_name, 'risk_level': risk_level(tool_name),
            'message': 'Review the exact action in Approvals before it runs.'}


def pending_approvals(limit: int = 20):
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY id DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
    return [_decode(row) for row in rows]


def _decode(row):
    item = dict(row)
    item['args'] = json.loads(item.pop('args_json'))
    return item


def get_approval(approval_id: int):
    with _conn() as conn:
        row = conn.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone()
    return _decode(row) if row else None


def claim_approval(approval_id: int):
    """Atomic compare-and-swap prevents double clicks/retries executing twice."""
    with _conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.execute("UPDATE approvals SET status='executing' WHERE id=? AND status='pending'", (approval_id,))
        if cur.rowcount != 1:
            return None
        return _decode(conn.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone())


def resolve_approval(approval_id: int, status: str, result: dict | None = None):
    expected = 'pending' if status == 'rejected' else 'executing'
    with _conn() as conn:
        cur = conn.execute('UPDATE approvals SET status=?,result_json=?,resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status=?',
                           (status, json.dumps(result, ensure_ascii=False), approval_id, expected))
    return cur.rowcount == 1
