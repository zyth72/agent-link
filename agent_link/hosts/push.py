"""On-demand delivery to a session-owned authenticated loopback listener."""
from __future__ import annotations
import json
import http.client
import os
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from ..errors import DeliveryFailure
from ..storage import validate_identifier
from ..shared import build_parent_prompt, build_child_prompt

PROTOCOL = 'agent-link-push/1'


def session_dir():
    configured = os.environ.get('AGENT_LINK_SESSIONS') or str(Path.home() / '.agent-link' / 'sessions')
    return Path(configured).expanduser().resolve()


def deliver(root, binding, message, *, role):
    framework = binding[f'{role}_framework']
    session_id = binding[f'{role}_session_id']
    validate_identifier(framework, 'framework')
    validate_identifier(session_id, 'session_id')
    path = session_dir() / framework / f'{session_id}.json'
    try:
        descriptor = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(descriptor, dict):
            raise ValueError('descriptor must be an object')
        if (descriptor.get('protocol'), descriptor.get('framework'), descriptor.get('session_id')) != (PROTOCOL, framework, session_id):
            raise ValueError('session descriptor identity mismatch')
        endpoint = descriptor['endpoint']
        if not isinstance(endpoint, str):
            raise ValueError('endpoint must be a string')
        url = urlsplit(endpoint)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('endpoint must be a loopback HTTP origin')
        token = descriptor['token']
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('invalid token')
        payload = {'protocol': PROTOCOL, 'framework': framework, 'session_id': session_id,
                   'binding_id': binding['binding_id'], 'run_id': binding['run_id'],
                   'role': role, 'message_id': message['message_id'],
                   'prompt': (build_parent_prompt if role == 'parent' else build_child_prompt)(root, binding, message)}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(body) > 65536:
            raise ValueError('notification exceeds 64 KiB')
        request = urllib.request.Request(endpoint.rstrip('/') + '/v1/notify',
            data=body,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=10) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError('response too large')
            result = json.loads(data)
            if not isinstance(result, dict) or response.status != 200 or result.get('accepted') is not True or result.get('session_id') != session_id:
                raise ValueError('listener did not acknowledge the bound session')
    except urllib.error.HTTPError as error:
        error.close()
        raise DeliveryFailure(f'{framework} push rejected (HTTP {error.code}); no retry') from None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as error:
        raise DeliveryFailure(f'{framework} session push unavailable or uncertain: {error}; no file fallback') from error
    return f'push:{framework}/{session_id}#{message["message_id"]}'
