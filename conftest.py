"""Migration-only guardrails: deny network and desktop side effects."""
import os
import sys
import socket
import subprocess
import webbrowser
import pytest
sys.dont_write_bytecode = True
os.environ['OPENAI_API_KEY'] = ''
os.environ['MAKMA_EMBEDDING_MODEL'] = ''


@pytest.fixture(autouse=True)
def prohibit_external_effects(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('External network/desktop process operation forbidden in migration tests')
    original_connect = socket.socket.connect
    def local_only(sock, address):
        # Windows asyncio implements its self-pipe using a loopback socketpair.
        if isinstance(address, tuple) and address[0] in {'127.0.0.1', '::1'}:
            return original_connect(sock, address)
        return denied(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setattr(webbrowser, 'open', denied)
