import json
import os
import asyncio
import threading
import random
from contextlib import suppress
from typing import Callable, Optional, List, Union
import websockets
from connection_diagnostics import record_status, exception_reason

def get_default_discord_token() -> str:
    """尝试自动从系统已缓存的 discord-cli token 中读取可用凭据"""
    cache_path = os.path.expanduser("~/.local/state/discord-cli/token.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = data.get("token", "").strip()
                if token:
                    return token
        except Exception:
            pass
    return ""

class DiscordGatewayClient:
    """
    轻量级 Discord Gateway 客户端，专门用于通过用户 Token 实时接收指定频道的消息。
    支持同时监控多个频道 (target_channels: list/set/string)。
    支持自动心跳保持、会话恢复与掉线重连。
    """
    MAX_MESSAGE_BYTES = 16 * 1024 * 1024
    MAX_FAILURES = 5
    NETWORK_COOLDOWN_SECONDS = 300.0
    STABLE_SECONDS = 300.0

    GATEWAY_URL = "wss://gateway.discord.gg/?v=9&encoding=json"

    def __init__(self, token: str, target_channel_ids: Union[str, List[str]], on_message_callback: Callable[[dict], None], on_status_callback: Optional[Callable[[str], None]] = None, should_connect: Optional[Callable[[], bool]] = None):
        self.token = token.strip() if token else get_default_discord_token()
        if isinstance(target_channel_ids, str):
            parts = [c.strip() for c in target_channel_ids.replace(",", " ").split() if c.strip()]
            self.target_channels = set(parts)
        else:
            self.target_channels = set(str(c).strip() for c in target_channel_ids if str(c).strip())

        self.on_message_callback = on_message_callback
        self.on_status_callback = on_status_callback
        self.should_connect = should_connect or (lambda: True)
        self.session_id = None
        self.seq = None
        self.resume_url = None
        self._main_task = None
        self.is_running = False
        self.reconnect_count = 0
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _notify_status(self, msg: str):
        record_status(msg)
        if self.on_status_callback:
            self.on_status_callback(msg)

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False
        if self._loop and self._loop.is_running():
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._cancel_main)

    def _cancel_main(self):
        if self._main_task:
            self._main_task.cancel()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._main_task = self._loop.create_task(self._connect_gateway())
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._notify_status(f"🔴 已停止自动监听：连接线程异常 ({exception_reason(e)})，请点击恢复连接")
        finally:
            self.is_running = False
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _wait_before_retry(self, delay):
        deadline = asyncio.get_running_loop().time() + delay
        while self.is_running and self.should_connect():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(1.0, remaining))
        return False

    async def _connect_gateway(self):
        while self.is_running:
            if not self.should_connect():
                self._notify_status("非交易时段，监控休眠中")
                return
            self._notify_status("正在连接 Discord Gateway...")
            ready_at = None
            reason = "服务器要求重连"
            try:
                url = (self.resume_url.rstrip("/") + "/?v=9&encoding=json") if self.session_id and self.resume_url else self.GATEWAY_URL
                async with websockets.connect(url, ping_interval=None, open_timeout=15, close_timeout=3, max_size=self.MAX_MESSAGE_BYTES) as ws:
                    heartbeat_task = None
                    try:
                        hello_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        hello = json.loads(hello_raw)
                        if hello.get("op") != 10:
                            raise RuntimeError("Invalid Gateway HELLO")

                        interval_ms = hello["d"]["heartbeat_interval"]
                        heartbeat_interval = interval_ms / 1000.0

                        identify_payload = {
                            "op": 2,
                            "d": {
                                "token": self.token,
                                "capabilities": 8189,
                                "properties": {
                                    "os": "Mac OS X",
                                    "browser": "Discord Client",
                                    "release_channel": "stable",
                                    "client_version": "1.0.9168",
                                    "os_version": "23.6.0",
                                    "os_arch": "x64"
                                },
                                "presence": {
                                    "status": "online",
                                    "since": 0,
                                    "activities": [],
                                    "afk": False
                                }
                            }
                        }
                        if self.session_id and self.seq is not None:
                            identify_payload = {"op": 6, "d": {"token": self.token, "session_id": self.session_id, "seq": self.seq}}
                        await ws.send(json.dumps(identify_payload))
                        acknowledged = True

                        async def send_heartbeat():
                            nonlocal acknowledged
                            acknowledged = False
                            await ws.send(json.dumps({"op": 1, "d": self.seq}))

                        async def heartbeat_loop():
                            await asyncio.sleep(heartbeat_interval * random.random())
                            while self.is_running:
                                if not acknowledged:
                                    self._notify_status("🟡 心跳未确认，恢复连接中...")
                                    await ws.close(code=4000)
                                    return
                                try:
                                    await send_heartbeat()
                                except Exception:
                                    await ws.close(code=4000)
                                    return
                                await asyncio.sleep(heartbeat_interval)

                        heartbeat_task = asyncio.create_task(heartbeat_loop())

                        while self.is_running:
                            raw_msg = await ws.recv()
                            msg = json.loads(raw_msg)
                            op = msg.get("op")
                            s = msg.get("s")
                            t = msg.get("t")

                            if s is not None:
                                self.seq = s

                            if op == 11:
                                acknowledged = True
                            elif op == 1:
                                await send_heartbeat()
                            elif op == 0 and t == "READY":
                                self.session_id = msg["d"]["session_id"]
                                self.resume_url = msg["d"].get("resume_gateway_url")
                                if ready_at is None:
                                    ready_at = asyncio.get_running_loop().time()
                                self._notify_status("🟢 已连接，Discord 频道实时监控中")
                            elif op == 0 and t == "RESUMED":
                                if ready_at is None:
                                    ready_at = asyncio.get_running_loop().time()
                                self._notify_status("🟢 会话已恢复，Discord 频道实时监控中")
                            elif op == 0 and t == "MESSAGE_CREATE":
                                data = msg.get("d", {})
                                channel_id = str(data.get("channel_id", ""))
                                if not self.target_channels or channel_id in self.target_channels:
                                    self.on_message_callback(data)

                            elif op in (7, 9):
                                self._notify_status("🟡 收到重新鉴权信号，重连中...")
                                if op == 9:
                                    if not msg.get("d"):
                                        self.session_id = self.seq = self.resume_url = None
                                        self._notify_status("🟡 会话失效，重新建立连接")
                                break

                    finally:
                        if heartbeat_task:
                            heartbeat_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat_task

            except ImportError:
                self.is_running = False
                self._notify_status("🔴 已停止自动监听：缺少代理依赖 python-socks，请安装依赖并重新打包应用")
                return
            except Exception as e:
                code = getattr(getattr(e, 'rcvd', None), 'code', None)
                http_status = getattr(getattr(e, 'response', None), 'status_code', None)
                if http_status is None:
                    http_status = getattr(e, 'status_code', None)
                if http_status in (401, 403, 429):
                    self.is_running = False
                    self._notify_status(f"🔴 已停止自动监听：Gateway HTTP {http_status}，请检查后在设置中保存以重试")
                    return
                if code in (4004, 4008, 4010, 4011, 4012, 4013, 4014):
                    self._notify_status(f"🔴 已停止自动监听：Gateway {code}，请检查后在设置中保存以重试")
                    self.is_running = False
                    return
                if code in (4007, 4009):
                    self.session_id = self.seq = self.resume_url = None
                    self._notify_status("🟡 会话失效，重新连接中")

                reason = exception_reason(e)

            if not self.is_running or not self.should_connect():
                return
            # A short READY/RESUMED must not reset a reconnect storm.
            if ready_at is not None and asyncio.get_running_loop().time() - ready_at >= self.STABLE_SECONDS:
                self.reconnect_count = 0
            self.reconnect_count += 1
            if self.reconnect_count >= self.MAX_FAILURES:
                self._notify_status(f"🟡 等待网络恢复：连续失败 {self.reconnect_count} 次 ({reason})，5分钟后自动尝试")
                if not await self._wait_before_retry(self.NETWORK_COOLDOWN_SECONDS):
                    self.is_running = False
                    return
                self.reconnect_count = 0
                continue
            delay = min(60.0, 10.0 * 2 ** (self.reconnect_count - 1) + random.uniform(0, 3))
            self._notify_status(f"🟡 断开 ({reason})，{delay:.0f}秒后重连 ({self.reconnect_count}/{self.MAX_FAILURES})")
            if not await self._wait_before_retry(delay):
                return
