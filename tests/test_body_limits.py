import asyncio
from makma.body_limits import BoundedBodyMiddleware


def exercise(messages, limit=4):
    called, sent = [], []
    async def app(scope, receive, send):
        called.append((await receive())['body'])
    iterator = iter(messages)
    async def receive():
        return next(iterator)
    async def send(message):
        sent.append(message)
    asyncio.run(BoundedBodyMiddleware(app, max_bytes=limit)({'type': 'http'}, receive, send))
    return called, sent


def test_chunked_oversize_stops_before_application():
    called, sent = exercise([{'type': 'http.request', 'body': b'abc', 'more_body': True}, {'type': 'http.request', 'body': b'de'}])
    assert called == []
    assert sent[0]['status'] == 413


def test_exact_limit_replays_complete_body():
    called, sent = exercise([{'type': 'http.request', 'body': b'ab', 'more_body': True}, {'type': 'http.request', 'body': b'cd'}])
    assert called == [b'abcd'] and sent == []


def test_disconnect_never_dispatches():
    called, sent = exercise([{'type': 'http.disconnect'}])
    assert called == [] and sent == []


def test_slow_body_timeout():
    called, sent = [], []
    async def app(scope, receive, send): called.append(True)
    async def receive(): await asyncio.sleep(0.02)
    async def send(message): sent.append(message)
    asyncio.run(BoundedBodyMiddleware(app, read_timeout=0.001)({'type': 'http'}, receive, send))
    assert not called and sent[0]['status'] == 408
