"""Small OpenAI-compatible text client; credentials remain on the local server."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from schemas import MemoryError


class ModelClient:
    """Extract patient statements or draft an answer with a configured remote model."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.key = env.get("DEEPSEEK_API_KEY", "").strip()
        self.model = env.get("ORAL_MODEL", "deepseek-chat").strip()
        base = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
        parts = urlsplit(base)
        if (parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))) or parts.username or parts.password or parts.query or parts.fragment:
            raise MemoryError("invalid_config", "模型地址须使用 HTTPS 或本机 HTTP 地址。")
        self.url = base + "/chat/completions"

    @property
    def enabled(self):
        """Whether a server-side key enables natural language conversations."""
        return bool(self.key)

    def complete(self, messages, json_output=False):
        """Return bounded text; surface actionable failures without exposing API bodies."""
        if not self.enabled:
            raise MemoryError("model_not_configured", "尚未配置 API Key，请先使用逐项问询或在 .env 中配置。")
        payload = {"model": self.model, "messages": messages, "temperature": 0,
                   "max_tokens": 2500}
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        request = Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=45) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("oversized response")
            content = json.loads(raw)["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model response")
            return content
        except HTTPError as error:
            raise MemoryError("model_unavailable", f"模型服务返回 HTTP {error.code}，请检查模型配置。") from error
        except (URLError, TimeoutError, OSError):
            raise MemoryError("model_unavailable", "模型连接失败或超时，请稍后重试。") from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise MemoryError("invalid_model_response", "模型返回格式异常，本次提取未完成。") from None
