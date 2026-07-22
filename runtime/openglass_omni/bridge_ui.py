"""bridge_ui.py — ESP32 duplex bridge 的 Web 观测/回放服务

URL:
  /              实时观测(摄像头 + 字幕 + 状态)
  /replay        session 列表
  /replay/<sid>  session 回放(直接播 live_session.mp4,字幕跟随)

依赖 recorder_live.py v5+ 写到 session_dir 的成品:
  live_session.mp4   ← 播放主体(ffmpeg 已对齐)
  live_session.m4a   ← 没有视频帧时的纯音频回退
  live_user.wav      ← 诊断下载
  live_ai.wav        ← 诊断下载
  events.jsonl       ← 字幕/事件源
  meta.json          ← 会话元信息
"""

from __future__ import annotations
from typing import Optional, Set, Callable
import json
import logging
from pathlib import Path
from typing import Optional, Set

from aiohttp import web

LOGGER = logging.getLogger("bridge_ui")

# 模板目录(和 bridge_ui.py 同级的 templates/)
TEMPLATES_DIR = Path(__file__).parent / "templates"

# 开发期想"改完刷新浏览器就生效"就设 True;生产环境设 False 只读一次
HOT_RELOAD_TEMPLATES = True

_template_cache: dict[str, str] = {}

def _load_template(name: str) -> str:
    """读取 templates/<name>。HOT_RELOAD_TEMPLATES=True 时每次都重读。"""
    if not HOT_RELOAD_TEMPLATES and name in _template_cache:
        return _template_cache[name]
    path = TEMPLATES_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        LOGGER.error("[UI] template not found: %s", path)
        return f"<h1>Template not found: {name}</h1>"
    _template_cache[name] = text
    return text


# ============================================================
# Server
# ============================================================

class WebUIServer:
    def __init__(self, port: int = 8080, host: str = "0.0.0.0",
                 sessions_root: Path = Path("./sessions"),
                 stop_callback: Optional[Callable[[], None]] = None,
                 mode_info: Optional[dict] = None):
        self.port = port
        self.host = host
        self.sessions_root = Path(sessions_root)
        self.live_clients: Set[web.WebSocketResponse] = set()
        self._runner: Optional[web.AppRunner] = None
        self._stop_callback = stop_callback  # ← 新增
        self._stop_fired = False  # ← 新增,防重入
        self.mode_info = mode_info or {"mode": "live"}

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get('/', self._h_live)
        app.router.add_get('/replay', self._h_replay_index)
        app.router.add_get('/replay/{sid}', self._h_replay)
        app.router.add_get('/live_ws', self._h_live_ws)
        app.router.add_get('/api/sessions', self._h_list)
        app.router.add_get('/api/session/{sid}/events', self._h_events)
        app.router.add_get('/api/session/{sid}/meta', self._h_meta)
        # —— v2: 直接吐成品文件,FileResponse 自带 HTTP Range 支持 ——
        app.router.add_get('/api/session/{sid}/video', self._h_video)
        app.router.add_get('/api/session/{sid}/audio', self._h_audio_only)
        app.router.add_get('/api/session/{sid}/user_audio.wav', self._h_user_wav)
        app.router.add_get('/api/session/{sid}/ai_audio.wav', self._h_ai_wav)
        # 兼容老路由(可能旧版 events.jsonl 引用 images/xxx.jpg)
        app.router.add_get('/api/session/{sid}/{path:images/.+}', self._h_image_legacy)
        app.router.add_get('/api/session/{sid}/{path:live_images/.+}', self._h_image_live)
        app.router.add_post('/api/stop', self._h_stop)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        LOGGER.info("[UI] http://%s:%d  (live + replay)", self.host, self.port)

    async def stop(self) -> None:
        for ws in list(self.live_clients):
            try: await ws.close()
            except Exception: pass
        if self._runner:
            await self._runner.cleanup()

    async def emit(self, evt: dict) -> None:
        if not self.live_clients:
            return
        msg = json.dumps(evt, ensure_ascii=False)
        dead = []
        for ws in list(self.live_clients):
            try: await ws.send_str(msg)
            except Exception: dead.append(ws)
        for ws in dead:
            self.live_clients.discard(ws)

    # ---- handlers ----
    async def _h_live(self, request):
        return web.Response(text=_load_template("live.html"), content_type='text/html')

    async def _h_stop(self, request):
        if self._stop_fired:
            return web.json_response({"ok": True, "already": True})
        self._stop_fired = True
        LOGGER.info("[UI] /api/stop triggered by browser")
        # 通知所有 live 客户端"要关了",前端可以把按钮改成"Saving..."
        try:
            await self.emit({"type": "stopping"})
        except Exception:
            pass
        if self._stop_callback is not None:
            try:
                self._stop_callback()
            except Exception as e:
                LOGGER.warning("[UI] stop_callback err: %s", e)
        return web.json_response({"ok": True})

    async def _h_replay_index(self, request):
        return web.Response(text=_load_template("replay_index.html"), content_type='text/html')

    async def _h_replay(self, request):
        return web.Response(text=_load_template("replay.html"), content_type='text/html')

    async def _h_live_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.live_clients.add(ws)

        LOGGER.info("[UI] live client +1 (total=%d)", len(self.live_clients))
        try:
            await ws.send_str(json.dumps(
                {"type": "mode", **self.mode_info}, ensure_ascii=False))
        except Exception:
            pass

        try:
            async for _ in ws: pass
        finally:
            self.live_clients.discard(ws)
            LOGGER.info("[UI] live client -1 (total=%d)", len(self.live_clients))
        return ws

    async def _h_list(self, request):
        sessions = []
        if self.sessions_root.exists():
            for d in sorted(self.sessions_root.iterdir(), reverse=True):
                if not d.is_dir():
                    continue
                meta_p = d / "meta.json"
                meta = {}
                if meta_p.exists():
                    try:
                        meta = json.loads(meta_p.read_text(encoding='utf-8'))
                    except Exception:
                        pass
                if (d / "live_session.mp4").exists():
                    media = "mp4"
                elif (d / "live_session.m4a").exists():
                    media = "m4a"
                else:
                    media = None
                sessions.append({
                    "id": d.name,
                    "tag": meta.get("session_tag", d.name),
                    "start_time": meta.get("start_time"),
                    "end_time": meta.get("end_time"),
                    "stats": meta.get("stats", {}),
                    "media": media,
                })
        return web.json_response({"sessions": sessions})

    def _safe(self, sid: str) -> Optional[Path]:
        if "/" in sid or ".." in sid:
            return None
        p = (self.sessions_root / sid).resolve()
        try:
            p.relative_to(self.sessions_root.resolve())
        except ValueError:
            return None
        if not p.is_dir():
            return None
        return p

    async def _h_meta(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.json_response({"error": "not found"}, status=404)
        meta = p / "meta.json"
        if not meta.exists():
            return web.json_response({"error": "no meta"}, status=404)
        return web.json_response(json.loads(meta.read_text(encoding='utf-8')))

    async def _h_events(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.json_response({"error": "not found"}, status=404)
        ev = p / "events.jsonl"
        if not ev.exists():
            return web.json_response({"events": []})
        out = []
        for line in ev.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return web.json_response({"events": out})

    # ---- 媒体:直接 FileResponse(支持 Range,可拖动进度条)----

    async def _h_video(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.Response(status=404)
        mp4 = p / "live_session.mp4"
        if not mp4.exists():
            return web.Response(status=404, text="no live_session.mp4")
        return web.FileResponse(mp4, headers={"Content-Type": "video/mp4"})

    async def _h_audio_only(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.Response(status=404)
        m4a = p / "live_session.m4a"
        if not m4a.exists():
            return web.Response(status=404, text="no live_session.m4a")
        return web.FileResponse(m4a, headers={"Content-Type": "audio/mp4"})

    async def _h_user_wav(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.Response(status=404)
        w = p / "live_user.wav"
        if not w.exists():
            return web.Response(status=404)
        return web.FileResponse(w, headers={"Content-Type": "audio/wav"})

    async def _h_ai_wav(self, request):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.Response(status=404)
        w = p / "live_ai.wav"
        if not w.exists():
            return web.Response(status=404)
        return web.FileResponse(w, headers={"Content-Type": "audio/wav"})

    async def _h_image_live(self, request):
        return await self._serve_image(request, subdir="live_images")

    async def _h_image_legacy(self, request):
        return await self._serve_image(request, subdir="images")

    async def _serve_image(self, request, subdir: str):
        p = self._safe(request.match_info['sid'])
        if not p:
            return web.Response(status=404)
        rel = request.match_info['path']
        # rel 形如 "live_images/img_00001.jpg" 或 "images/xxx.jpg"
        # 这里只取末尾文件名,防穿越
        name = Path(rel).name
        if "/" in name or ".." in name or not name:
            return web.Response(status=400)
        img = p / subdir / name
        if not img.exists():
            return web.Response(status=404)
        return web.FileResponse(img, headers={"Content-Type": "image/jpeg"})


# ============================================================
# Standalone entry
# ============================================================

def _main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="Bridge UI — standalone replay server (no ESP32 / no model)"
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--sessions", default="./sessions",
                        help="sessions 根目录(默认 ./sessions)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sessions_root = Path(args.sessions).resolve()
    if not sessions_root.exists():
        LOGGER.warning("[UI] sessions dir 不存在: %s(之后录到这里就能看见)",
                       sessions_root)

    server = WebUIServer(
        port=args.port,
        host=args.host,
        sessions_root=sessions_root
    )

    async def _run():
        await server.start()
        LOGGER.info("[UI] standalone mode — replay only")
        LOGGER.info("[UI] sessions root: %s", sessions_root)
        LOGGER.info("[UI] open http://localhost:%d/replay", args.port)
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[UI] bye.")


if __name__ == "__main__":
    _main()