import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from app import create_app
from makma import memory, store
from makma.policy import claim_approval, create_approval
from makma.tools import execute_tool


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MAKMA_DB_PATH', str(tmp_path / 'api.db'))
    with TestClient(create_app()) as client:
        client.headers['X-Makma-Token'] = client.get('/api/session').json()['token']
        client.patch('/api/settings', json={'provider': 'offline'})
        yield client


def test_health_hud_and_static(client):
    health = client.get('/health').json()
    assert health['ok'] and not health['brain_ready']
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/openapi.json').json()['info']['version'] == '3.0.0'


def test_local_origin_and_token_boundary(client):
    assert client.post('/api/memory', json={'content': 'bad'}, headers={'X-Makma-Token': ''}).status_code == 403
    assert client.get('/api/session', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/session', headers={'Host': 'evil.example'}).status_code == 400
    assert client.post('/api/memory', json={'content': 'bad'}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_memory_crud_and_export(client):
    first = client.post('/api/memory', json={'content': 'The project name is Cedar'}).json()
    assert first['ok']
    assert client.get('/api/memory?query=Cedar').json()['items'][0]['id'] == first['id']
    assert client.patch(f"/api/memory/{first['id']}", json={'content': 'The project name is Birch'}).json()['ok']
    assert client.get('/api/memory?query=Birch').json()['items']
    exported = client.get('/api/export').json()
    assert exported['tables']['memories'][0]['content'] == 'The project name is Birch'
    assert client.delete(f"/api/memory/{first['id']}").json()['ok']
    assert client.get('/api/memory').json()['items'] == []
    assert client.post('/api/backup').json()['ok']


def test_offline_chat_persistence_and_sse(client):
    reply = client.post('/api/chat', json={'message': 'remember: Cedar ships Friday'}).json()['reply']
    assert 'Remembered' in reply
    reply = client.post('/api/chat', json={'message': 'recall Cedar'}).json()['reply']
    assert 'Friday' in reply
    with client.stream('POST', '/api/chat/stream', json={'message': 'status'}) as response:
        chunks = ''.join(response.iter_text())
    assert '"type": "done"' in chunks and 'CPU' in chunks
    assert len(client.get('/api/messages').json()['items']) >= 4
    assert client.post('/api/reset').json()['ok']
    assert client.get('/api/messages').json()['items'] == []


def test_lockdown_blocks_actions_even_approved(client):
    assert client.patch('/api/settings', json={'autonomy': 'lockdown'}).status_code == 200
    assert client.post('/api/memory', json={'content': 'blocked'}).status_code == 403
    assert not execute_tool('remember', {'content': 'also blocked'}, approved=True)['ok']
    assert execute_tool('get_system_status', {})['ok']
    assert client.patch('/api/settings', json={'autonomy': 'assisted'}).status_code == 200


def test_invalid_tool_never_creates_approval(client):
    for args in ({'x': 'bad', 'y': 3}, {'x': 3, 'y': 4, 'approved': True}, {'x': True, 'y': 4}):
        assert not execute_tool('mouse_click', args)['ok']
    assert not execute_tool('missing', {})['ok']
    assert client.get('/api/approvals').json()['items'] == []


def test_approval_exactly_once_in_simulation(client):
    client.patch('/api/settings', json={'simulation': True})
    pending = client.post('/api/tools/type_text', json={'args': {'text': 'simulation only'}}).json()
    assert pending['approval_required']
    aid = pending['approval_id']
    result = client.post(f'/api/approvals/{aid}/approve').json()
    assert result['ok'] and result['result']['simulated']
    assert client.post(f'/api/approvals/{aid}/approve').status_code == 409
    assert client.get('/api/audit').json()['items']


def test_atomic_claim_under_concurrency(client):
    aid = create_approval('mouse_click', {'x': 1, 'y': 2})['approval_id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim_approval(aid), range(8)))
    assert sum(r is not None for r in results) == 1


def test_task_and_schedule_validation(client):
    task = client.post('/api/tasks', json={'title': 'Review changes', 'due_at': '2026-09-06T08:00:00+02:00'}).json()
    assert client.patch(f"/api/tasks/{task['id']}", json={'status': 'completed'}).json()['ok']
    assert client.post('/api/schedules', json={'title': 'Bad', 'run_at': '2026-09-06T08:00:00'}).status_code == 422
    assert client.post('/api/schedules', json={'title': 'Fine', 'run_at': '2026-09-06T08:00:00Z'}).json()['ok']
    assert client.post('/api/schedules', json={'title': 'Too frequent', 'run_at': '2026-09-06T08:00:00Z', 'interval_seconds': 1}).status_code == 422


def test_vision_validation_without_provider(client):
    assert client.post('/api/vision', json={'image': 'data:image/svg+xml;base64,PHN2Zz4='}).status_code == 422
    assert client.post('/api/vision', json={'image': 'data:image/png;base64,aGVsbG8='}).status_code == 422
    assert client.post('/api/vision', json={'image': 'data:image/png;base64,iVBORw0KGgo='}).status_code == 503


def test_websocket_auth_and_events(client):
    with client.websocket_connect('/ws') as ws:
        ws.send_json({'token': client.headers['X-Makma-Token']})
        payload = ws.receive_json()
        assert payload['type'] in {'event', 'CORE_STARTED', 'heartbeat'}


def test_mission_plan_validation(client):
    m = client.post('/api/missions', json={'title': 'Check system', 'objective': 'Read telemetry'}).json()
    mid = m['id']
    assert client.post(f'/api/missions/{mid}/run').status_code == 400
    assert client.post(f'/api/missions/{mid}/steps', json={'title': 'Bad dependency', 'tool_name': 'get_system_status', 'depends_on': [999]}).status_code == 400
    assert client.post(f'/api/missions/{mid}/steps', json={'title': 'Observe', 'tool_name': 'get_system_status'}).json()['ok']
    assert client.patch(f'/api/missions/{mid}', json={'status': 'completed'}).status_code == 409
