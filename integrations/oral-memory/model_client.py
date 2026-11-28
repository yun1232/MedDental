"""Direct OpenAI-compatible multimodal JSON calls; no application/server process."""
import base64
import json
import mimetypes
import os
from pathlib import Path
import re
import time
from urllib import request, error


def load_env(path):
    """Read literal KEY=VALUE settings without evaluating shell or Python expressions."""
    path = Path(path)
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.removeprefix('export ').partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'Invalid environment entry at line {number}')
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].strip()
        os.environ.setdefault(key, value)


class ModelClient:
    def __init__(self, base_url, model, api_key='', timeout=120, retries=2):
        self.url = base_url.rstrip('/') + '/chat/completions'
        self.model, self.api_key = model, api_key
        self.timeout, self.retries = timeout, retries
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError('Model base URL must use http(s)')

    def complete(self, stage, instruction, payload, images):
        content = [{'type': 'text', 'text': json.dumps({'stage': stage, **payload}, ensure_ascii=False)}]
        for image in images:
            path = Path(image['path'])
            media_type = mimetypes.guess_type(path.name)[0] or 'image/png'
            content.append({'type': 'text', 'text': json.dumps({k: v for k, v in image.items() if k != 'path'}, ensure_ascii=False)})
            content.append({'type': 'image_url', 'image_url': {'url': 'data:' + media_type + ';base64,' + base64.b64encode(path.read_bytes()).decode()}})
        body = json.dumps({'model': self.model, 'temperature': 0,
                           'messages': [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': content}],
                           'response_format': {'type': 'json_object'}, 'max_tokens': 4000}).encode()
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        for attempt in range(self.retries + 1):
            try:
                req = request.Request(self.url, data=body, headers=headers, method='POST')
                with request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError('Model response exceeds 4 MiB')
                result = json.loads(raw)
                choice = result['choices'][0]
                if choice.get('finish_reason') not in (None, 'stop'):
                    raise ValueError('Model response was truncated or incomplete')
                text = choice['message']['content']
                if isinstance(text, list):
                    text = '\n'.join(p.get('text', '') for p in text if p.get('type') == 'text')
                if not isinstance(text, str):
                    raise ValueError('Model did not return JSON text')
                if text.strip().startswith('```'):
                    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip())
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError('Model result must be a JSON object')
                return value
            except error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise RuntimeError(f'Model API HTTP {exc.code}') from None
            except (error.URLError, TimeoutError):
                if attempt == self.retries:
                    raise RuntimeError('Model API connection failed or timed out') from None
            time.sleep(min(2 ** attempt, 4))
        raise RuntimeError('Model API retries exhausted')
