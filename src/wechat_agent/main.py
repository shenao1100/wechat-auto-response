from __future__ import annotations

import argparse
import json
import signal
import sys

from .config import ConfigError, load_config
from .service import AgentService, configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor WeChat groups and forward important events")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--check", action="store_true", help="Validate config without logging into WeChat")
    parser.add_argument("--list-sessions", action="store_true", help="List recent WeChat sessions and exit")
    parser.add_argument("--status", action="store_true", help="Show persistent queue status and exit")
    parser.add_argument("--memory", nargs="?", const="__shared__", metavar="IGNORED_GROUP_ID", help="Show shared active memory and exit")
    parser.add_argument("--schedules", nargs="?", const="__all__", metavar="IGNORED_GROUP_ID", help="Show global schedules and exit")
    parser.add_argument("--runs", type=int, nargs="?", const=20, help="Show recent Agent runs and exit")
    parser.add_argument("--failed", action="store_true", help="Show failed inbox/deliveries and exit")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed inbox and deliveries, then exit")
    parser.add_argument("--web", action="store_true", help="Deprecated compatibility flag; Agent and WebUI are started together by default")
    parser.add_argument("--web-only", action="store_true", help="Start only the local WebUI server (maintenance mode)")
    parser.add_argument("--agent-only", action="store_true", help="Start the Agent without the embedded WebUI")
    parser.add_argument("--web-host", default="127.0.0.1", help="WebUI bind host")
    parser.add_argument("--web-port", type=int, default=8765, help="WebUI bind port")
    return parser


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"Configuration OK: {len(config.groups)} group(s), model={config.ai.model}")
        return 0
    if args.list_sessions:
        from wechatauto import WeChatDB

        db = WeChatDB()
        for item in db.get_sessions(limit=200):
            print(item)
        return 0
    if args.status or args.memory is not None or args.schedules is not None or args.runs is not None or args.failed or args.retry_failed:
        from .store import Store

        store = Store(config.database_path)
        try:
            if args.retry_failed:
                result = {
                    "inbox_requeued": store.retry_failed_incoming(),
                    "deliveries_requeued": store.retry_failed_deliveries(),
                }
            elif args.memory is not None:
                result = store.get_memories(args.memory)
            elif args.schedules is not None:
                result = store.list_schedules(None, include_done=True)
            elif args.runs is not None:
                result = store.recent_runs(max(1, min(500, args.runs)))
            elif args.failed:
                result = store.failed_items()
            else:
                result = store.queue_status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return 0
    if args.web_only:
        from .web_server import run_web

        run_web(args.config, args.web_host, args.web_port)
        return 0

    configure_logging(config.log_path)
    service = AgentService(config, config_path=args.config)
    web_server = None
    if not args.agent_only:
        from .web_server import EmbeddedWebServer

        web_server = EmbeddedWebServer(
            args.config,
            service.reload_config,
            service.gateway,  # type: ignore[arg-type]
            service.trigger_history_review,
            args.web_host,
            args.web_port,
        )

    def stop(_signum: int, _frame: object) -> None:
        service.stop_event.set()
        service.aggregator.wake()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    if web_server is not None:
        web_server.start()
    try:
        service.run_forever()
    finally:
        if web_server is not None:
            web_server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
