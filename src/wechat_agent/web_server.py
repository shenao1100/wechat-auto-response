from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from .config import load_config
from .config_manager import ConfigManager
from .store import Store
from .wechat_gateway import WeChatGateway


class WebContext:
    def __init__(self, config_path: str, gateway: WeChatGateway | None = None):
        self.config_path = str(Path(config_path).resolve())
        self.manager = ConfigManager(self.config_path)
        self.config = load_config(self.config_path)
        self.store = Store(self.config.database_path)
        self._gateway: WeChatGateway | None = gateway
        self._gateway_lock = asyncio.Lock()

    async def gateway(self) -> WeChatGateway:
        async with self._gateway_lock:
            if self._gateway is None:
                self._gateway = await asyncio.to_thread(WeChatGateway, self.config.poll_interval)
            return self._gateway


def create_app(
    config_path: str = "config.json",
    on_config_changed: Any | None = None,
    gateway: WeChatGateway | None = None,
    on_manual_trigger: Any | None = None,
) -> Any:
    try:
        from fastapi import Body, FastAPI, HTTPException, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError("WebUI requires: python -m pip install -e .[web]") from exc

    context = WebContext(config_path, gateway)
    app = FastAPI(title="WeChat Important Agent", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def api_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    async def apply_hot_reload() -> dict[str, Any]:
        if on_config_changed is None:
            return {"applied": False, "reason": "Agent service is not running in this process"}
        return await asyncio.to_thread(on_config_changed)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": "0.2.0"}

    @app.get("/api/settings")
    async def settings() -> dict[str, Any]:
        return context.manager.public_settings()

    @app.put("/api/settings/groups")
    async def replace_groups(payload: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        try:
            await asyncio.to_thread(context.manager.replace_groups, payload)
            hot_reload = await apply_hot_reload()
            return {"ok": True, "groups": context.manager.public_settings()["groups"], "hot_reload": hot_reload}
        except Exception as exc:
            raise api_error(exc)

    @app.post("/api/groups/{group_id}/evaluate")
    async def evaluate_group_history(group_id: str) -> dict[str, Any]:
        if on_manual_trigger is None:
            raise HTTPException(status_code=409, detail="Agent 服务未在此进程运行，无法触发判断")
        try:
            return await asyncio.to_thread(on_manual_trigger, group_id)
        except Exception as exc:
            raise api_error(exc)

    @app.get("/api/wechat/groups")
    async def wechat_groups() -> list[dict[str, Any]]:
        gateway = await context.gateway()
        return await asyncio.to_thread(gateway.list_groups)

    @app.get("/api/wechat/contacts")
    async def wechat_contacts(q: str = Query(default="", max_length=100)) -> list[dict[str, Any]]:
        gateway = await context.gateway()
        return await asyncio.to_thread(gateway.search_contacts, q)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(context.store.queue_status)

    @app.get("/api/memories")
    async def memories(group_id: str = Query(default="__shared__")) -> list[dict[str, Any]]:
        return await asyncio.to_thread(context.store.get_memories, group_id)

    @app.put("/api/memories")
    async def put_memory(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            await asyncio.to_thread(
                context.store.remember,
                str(payload.get("group_id") or "__shared__"),
                str(payload["key"]),
                str(payload["value"]),
                payload.get("expires_at"),
            )
            return {"ok": True}
        except Exception as exc:
            raise api_error(exc)

    @app.delete("/api/memories")
    async def delete_memory(group_id: str = Query(default="__shared__"), key: str = Query(...)) -> dict[str, Any]:
        return {"ok": await asyncio.to_thread(context.store.delete_memory, group_id, key)}

    @app.get("/api/prompts")
    async def prompts() -> list[dict[str, Any]]:
        return await asyncio.to_thread(context.manager.list_prompts)

    @app.get("/api/prompts/{name}")
    async def prompt(name: str) -> dict[str, Any]:
        try:
            return {"name": name, "content": await asyncio.to_thread(context.manager.read_prompt, name)}
        except Exception as exc:
            raise api_error(exc)

    @app.put("/api/prompts/{name}")
    async def put_prompt(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            await asyncio.to_thread(context.manager.write_prompt, name, str(payload.get("content") or ""))
            return {"ok": True, "hot_reload": await apply_hot_reload()}
        except Exception as exc:
            raise api_error(exc)

    @app.delete("/api/prompts/{name}")
    async def delete_prompt(name: str) -> dict[str, Any]:
        try:
            await asyncio.to_thread(context.manager.delete_prompt, name)
            return {"ok": True, "hot_reload": await apply_hot_reload()}
        except Exception as exc:
            raise api_error(exc)

    @app.get("/api/clarifications")
    async def clarifications(status: str | None = None, group_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(context.store.list_clarifications, status, group_id)

    @app.post("/api/clarifications/{clarification_id}/answer")
    async def answer_clarification(clarification_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                context.store.answer_clarification,
                clarification_id,
                str(payload.get("answer") or ""),
                "webui",
            )
            return {"ok": True, "queued_for_reevaluation": bool(result["inserted"])}
        except Exception as exc:
            raise api_error(exc)

    @app.get("/api/schedules")
    async def schedules(group_id: str = Query(...)) -> list[dict[str, Any]]:
        return await asyncio.to_thread(context.store.list_schedules, group_id, True)

    @app.get("/api/runs")
    async def runs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return await asyncio.to_thread(context.store.recent_runs, limit)

    @app.get("/api/failed")
    async def failed() -> dict[str, Any]:
        return await asyncio.to_thread(context.store.failed_items)

    @app.post("/api/failed/retry")
    async def retry_failed() -> dict[str, Any]:
        return {
            "inbox_requeued": await asyncio.to_thread(context.store.retry_failed_incoming),
            "deliveries_requeued": await asyncio.to_thread(context.store.retry_failed_deliveries),
        }

    web_dist = Path(__file__).resolve().parent / "web_dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str) -> Any:
            requested = (web_dist / path).resolve()
            if path and requested.is_file() and web_dist in requested.parents:
                return FileResponse(str(requested))
            return FileResponse(str(web_dist / "index.html"))

    return app


def run_web(config_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("WebUI requires: python -m pip install -e .[web]") from exc
    uvicorn.run(create_app(config_path), host=host, port=port, log_level="info")


class EmbeddedWebServer:
    """Run uvicorn beside the Agent and participate in its shutdown lifecycle."""

    def __init__(
        self,
        config_path: str,
        on_config_changed: Any,
        gateway: WeChatGateway,
        on_manual_trigger: Any,
        host: str = "127.0.0.1",
        port: int = 8765,
    ):
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("WebUI requires: python -m pip install -e .[web]") from exc
        uvicorn_config = uvicorn.Config(
            create_app(config_path, on_config_changed, gateway, on_manual_trigger),
            host=host,
            port=port,
            log_level="info",
        )
        self.server = uvicorn.Server(uvicorn_config)
        self.thread = threading.Thread(target=self.server.run, name="webui-server", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
