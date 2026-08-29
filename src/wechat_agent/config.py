from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AIConfig, AppConfig, ChainRuleConfig, GroupConfig


class ConfigError(ValueError):
    pass


def _require(data: dict[str, Any], key: str, location: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ConfigError(f"{location}.{key} is required")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    base = config_path.parent
    ai_raw = raw.get("ai") or {}
    ai = AIConfig(
        base_url=str(_require(ai_raw, "base_url", "ai")).rstrip("/"),
        model=str(_require(ai_raw, "model", "ai")),
        api_key_env=str(ai_raw.get("api_key_env", "AI_API_KEY")),
        timeout_seconds=float(ai_raw.get("timeout_seconds", 60)),
        temperature=float(ai_raw.get("temperature", 0.1)),
        max_steps=max(1, int(ai_raw.get("max_steps", 10))),
        verify_ssl=bool(ai_raw.get("verify_ssl", True)),
        log_requests=bool(ai_raw.get("log_requests", False)),
    )

    defaults = raw.get("defaults") or {}
    groups: list[GroupConfig] = []
    for index, item in enumerate(raw.get("groups") or []):
        location = f"groups[{index}]"
        prompt = str(item.get("system_prompt", ""))
        if item.get("system_prompt_file"):
            prompt_path = (base / str(item["system_prompt_file"])).resolve()
            try:
                prompt = prompt_path.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise ConfigError(f"Prompt file not found: {prompt_path}") from exc
        targets = tuple(dict.fromkeys(str(x).strip() for x in (item.get("forward_to") or []) if str(x).strip()))
        if not targets:
            raise ConfigError(f"{location}.forward_to must not be empty")
        chain_raw = item.get("chain") or {}
        chain_rules: list[ChainRuleConfig] = []
        for rule_index, rule in enumerate(chain_raw.get("rules") or []):
            rule_location = f"{location}.chain.rules[{rule_index}]"
            keywords = tuple(
                dict.fromkeys(str(value).strip() for value in rule.get("match_keywords") or [] if str(value).strip())
            )
            template = str(rule.get("entry_template") or "").strip()
            if not keywords:
                raise ConfigError(f"{rule_location}.match_keywords must not be empty")
            if not template:
                raise ConfigError(f"{rule_location}.entry_template must not be empty")
            chain_rules.append(
                ChainRuleConfig(
                    name=str(rule.get("name") or f"接龙规则 {rule_index + 1}"),
                    match_keywords=keywords,
                    exclude_keywords=tuple(
                        dict.fromkeys(
                            str(value).strip() for value in rule.get("exclude_keywords") or [] if str(value).strip()
                        )
                    ),
                    entry_template=template,
                    self_identifiers=tuple(
                        dict.fromkeys(
                            str(value).strip() for value in rule.get("self_identifiers") or [] if str(value).strip()
                        )
                    ),
                    enabled=bool(rule.get("enabled", True)),
                )
            )
        groups.append(
            GroupConfig(
                id=str(_require(item, "id", location)),
                name=str(item.get("name") or item["id"]),
                forward_to=targets,
                system_prompt=prompt,
                history_limit=max(1, int(item.get("history_limit", defaults.get("history_limit", 30)))),
                aggregation_seconds=max(
                    0.1,
                    float(item.get("aggregation_seconds", defaults.get("aggregation_seconds", 8))),
                ),
                importance_threshold=max(
                    0,
                    min(100, int(item.get("importance_threshold", defaults.get("importance_threshold", 70)))),
                ),
                enabled=bool(item.get("enabled", True)),
                chain_enabled=bool(chain_raw.get("enabled", False)),
                chain_rules=tuple(chain_rules),
            )
        )
    if not groups:
        raise ConfigError("At least one group must be configured")

    def relative_path(value: str) -> str:
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else base / candidate)

    return AppConfig(
        ai=ai,
        groups=tuple(groups),
        timezone=str(raw.get("timezone", "Asia/Shanghai")),
        database_path=relative_path(str(raw.get("database_path", "data/app.db"))),
        log_path=relative_path(str(raw.get("log_path", "logs/app.log"))),
        poll_interval=max(0.2, float(raw.get("poll_interval", 1.0))),
        schedule_poll_seconds=max(1.0, float(raw.get("schedule_poll_seconds", 5.0))),
        send_verify=bool(raw.get("send_verify", True)),
        duplicate_window_hours=max(1, int(raw.get("duplicate_window_hours", 24))),
    )
