from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any


PROMPT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.md$")


class ConfigManager:
    def __init__(self, config_path: str):
        self.path = Path(config_path).resolve()
        self.root = self.path.parent
        self.prompts_dir = self.root / "prompts"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def read_raw(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def public_settings(self) -> dict[str, Any]:
        raw = self.read_raw()
        ai = raw.get("ai") or {}
        return {
            "timezone": raw.get("timezone", "Asia/Shanghai"),
            "poll_interval": raw.get("poll_interval", 1.0),
            "schedule_poll_seconds": raw.get("schedule_poll_seconds", 5.0),
            "send_verify": raw.get("send_verify", True),
            "duplicate_window_hours": raw.get("duplicate_window_hours", 24),
            "ai": {
                "base_url": ai.get("base_url", ""),
                "model": ai.get("model", ""),
                "api_key_env": ai.get("api_key_env", "AI_API_KEY"),
                "temperature": ai.get("temperature", 0.0),
                "max_steps": ai.get("max_steps", 10),
                "verify_ssl": ai.get("verify_ssl", True),
                "log_requests": ai.get("log_requests", False),
                "api_key_present": bool(os.environ.get(str(ai.get("api_key_env", "AI_API_KEY")))),
            },
            "defaults": raw.get("defaults") or {},
            "groups": raw.get("groups") or [],
        }

    def replace_groups(self, groups: list[dict[str, Any]]) -> None:
        if not groups:
            raise ValueError("At least one monitored group is required")
        clean_groups: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in groups:
            group_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            targets = list(dict.fromkeys(str(value).strip() for value in item.get("forward_to") or [] if str(value).strip()))
            if not group_id or not name or not targets:
                raise ValueError("Every group requires id, name and at least one forward_to")
            if group_id in seen:
                raise ValueError(f"Duplicate group id: {group_id}")
            seen.add(group_id)
            prompt_file = str(item.get("system_prompt_file") or "").strip()
            if prompt_file:
                prompt_name = Path(prompt_file).name
                prompt_path = self._prompt_path(prompt_name)
                if not prompt_path.is_file():
                    raise ValueError(f"Prompt file not found: {prompt_name}")
                prompt_file = f"prompts/{prompt_name}"
            chain = item.get("chain") or {}
            clean_chain_rules: list[dict[str, Any]] = []
            for rule_index, rule in enumerate(chain.get("rules") or []):
                keywords = list(dict.fromkeys(
                    str(value).strip() for value in rule.get("match_keywords") or [] if str(value).strip()
                ))
                template = str(rule.get("entry_template") or "").strip()
                if not keywords:
                    raise ValueError(f"Chain rule {rule_index + 1} in {name} requires match_keywords")
                if not template:
                    raise ValueError(f"Chain rule {rule_index + 1} in {name} requires entry_template")
                clean_chain_rules.append({
                    "name": str(rule.get("name") or f"接龙规则 {rule_index + 1}").strip()[:100],
                    "enabled": bool(rule.get("enabled", True)),
                    "match_keywords": keywords[:20],
                    "exclude_keywords": list(dict.fromkeys(
                        str(value).strip() for value in rule.get("exclude_keywords") or [] if str(value).strip()
                    ))[:20],
                    "entry_template": template[:1000],
                    "self_identifiers": list(dict.fromkeys(
                        str(value).strip() for value in rule.get("self_identifiers") or [] if str(value).strip()
                    ))[:20],
                })
            clean_groups.append(
                {
                    "id": group_id,
                    "name": name,
                    "forward_to": targets,
                    "system_prompt_file": prompt_file,
                    "enabled": bool(item.get("enabled", True)),
                    "history_limit": max(1, min(200, int(item.get("history_limit", 30)))),
                    "aggregation_seconds": max(0.1, min(60, float(item.get("aggregation_seconds", 8)))),
                    "importance_threshold": max(0, min(100, int(item.get("importance_threshold", 70)))),
                    "chain": {
                        "enabled": bool(chain.get("enabled", False)),
                        "rules": clean_chain_rules,
                    },
                }
            )
        with self._lock:
            raw = self.read_raw()
            raw["groups"] = clean_groups
            self._atomic_write(raw)

    def list_prompts(self) -> list[dict[str, Any]]:
        return [
            {"name": path.name, "size": path.stat().st_size, "updated_at": path.stat().st_mtime}
            for path in sorted(self.prompts_dir.glob("*.md"), key=lambda value: value.name.casefold())
        ]

    def read_prompt(self, name: str) -> str:
        return self._prompt_path(name).read_text(encoding="utf-8")

    def write_prompt(self, name: str, content: str) -> None:
        path = self._prompt_path(name)
        if not content.strip():
            raise ValueError("Prompt content must not be empty")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    def delete_prompt(self, name: str) -> None:
        path = self._prompt_path(name)
        raw = self.read_raw()
        used = [group.get("name") for group in raw.get("groups") or [] if Path(str(group.get("system_prompt_file") or "")).name == name]
        if used:
            raise ValueError(f"Prompt is used by groups: {', '.join(str(item) for item in used)}")
        path.unlink()

    def _prompt_path(self, name: str) -> Path:
        if not PROMPT_NAME.fullmatch(name):
            raise ValueError("Prompt name must use letters, numbers, underscore or hyphen and end in .md")
        path = (self.prompts_dir / name).resolve()
        if path.parent != self.prompts_dir.resolve():
            raise ValueError("Invalid prompt path")
        return path

    def _atomic_write(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix="config-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(raw, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
