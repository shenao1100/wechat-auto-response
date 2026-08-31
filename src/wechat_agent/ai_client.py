from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import AIConfig

logger = logging.getLogger(__name__)


class AIError(RuntimeError):
    pass


class ChatClient(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible Chat Completions client using the stdlib."""

    def __init__(self, config: AIConfig):
        self.config = config
        self.api_key = os.environ.get(config.api_key_env, "")
        if not self.api_key:
            raise AIError(f"Environment variable {config.api_key_env} is not set")
        self.ssl_context: ssl.SSLContext | None = None
        if not config.verify_ssl:
            self.ssl_context = ssl._create_unverified_context()
            logger.warning(
                "AI upstream TLS certificate verification is DISABLED for %s; use only with a trusted network/upstream",
                config.base_url,
            )

    def _endpoint(self) -> str:
        if self.config.base_url.endswith("/chat/completions"):
            return self.config.base_url
        return f"{self.config.base_url}/chat/completions"

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "thinking": {"type": "enabled" if self.config.thinking_enabled else "disabled"},
            "reasoning_effort": self.config.reasoning_effort,
        }
        if self.config.log_requests:
            logger.info(
                "Full AI upstream request payload (API key excluded):\n%s",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        total_attempts = self.config.retry_attempts + 1
        for attempt in range(total_attempts):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.config.timeout_seconds,
                    context=self.ssl_context,
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:2000]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= total_attempts - 1:
                    raise AIError(f"AI upstream returned HTTP {exc.code}: {body}") from exc
                logger.warning("AI upstream HTTP %s; retrying attempt %d/%d", exc.code, attempt + 1, total_attempts - 1)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= total_attempts - 1:
                    raise AIError(f"AI upstream request failed: {exc}") from exc
                logger.warning("AI upstream request failed (%s); retrying attempt %d/%d", exc, attempt + 1, total_attempts - 1)
            except json.JSONDecodeError as exc:
                if attempt >= total_attempts - 1:
                    raise AIError(f"AI upstream returned invalid JSON: {exc}") from exc
                logger.warning("AI upstream returned invalid JSON; retrying attempt %d/%d", attempt + 1, total_attempts - 1)
            if attempt < total_attempts - 1:
                time.sleep(min(2 ** attempt, 8))

        try:
            return result["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Unexpected AI response: {str(result)[:2000]}") from exc
