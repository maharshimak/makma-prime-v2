"""Bound real HTTP body bytes before application parsing, including chunked uploads."""
import asyncio
from starlette.responses import JSONResponse


class BoundedBodyMiddleware:
    def __init__(self, app, max_bytes=13_000_000, read_timeout=15.0):
        self.app = app
        self.max_bytes = max_bytes
        self.read_timeout = read_timeout

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        try:
            async with asyncio.timeout(self.read_timeout):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    if message['type'] != 'http.request':
                        continue
                    body = message.get('body', b'')
                    total += len(body)
                    if total > self.max_bytes:
                        response = JSONResponse({'detail': 'Request is too large.'}, status_code=413)
                        return await response(scope, receive, send)
                    chunks.append(body)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            response = JSONResponse({'detail': 'Request body timed out.'}, status_code=408)
            return await response(scope, receive, send)
        first = True

        async def replay():
            nonlocal first
            if first:
                first = False
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        return await self.app(scope, replay, send)
