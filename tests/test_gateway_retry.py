import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord_listener import DiscordGatewayClient


class Socket:
    def __init__(self, messages):
        self.recv = AsyncMock(side_effect=[json.dumps(m) for m in messages])
        self.send = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestGatewayRetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        diagnostic = patch('discord_listener.record_status')
        diagnostic.start()
        self.addCleanup(diagnostic.stop)

    async def test_final_status_keeps_close_codes(self):
        exc = RuntimeError('secret-token must not be logged')
        exc.sent = SimpleNamespace(code=1009)
        exc.rcvd = SimpleNamespace(code=1000)
        statuses = []
        client = DiscordGatewayClient('offline', '123', lambda m: None, statuses.append)
        client.is_running = True
        with patch('discord_listener.websockets.connect', side_effect=exc), \
             patch.object(client, '_wait_before_retry', AsyncMock(side_effect=[True, True, True, True, False])):
            await client._connect_gateway()
        self.assertIn('client=1009', statuses[-1])
        self.assertIn('server=1000', statuses[-1])
        self.assertNotIn('secret-token', '\n'.join(statuses))

    def test_uncaught_thread_error_is_terminal(self):
        statuses = []
        client = DiscordGatewayClient('offline', '123', lambda m: None, statuses.append)
        client.is_running = True
        with patch.object(client, '_connect_gateway', AsyncMock(side_effect=RuntimeError('private'))):
            client._run_loop()
        self.assertFalse(client.is_running)
        self.assertIn('已停止自动监听', statuses[-1])
        self.assertNotIn('private', statuses[-1])

    async def run_failures(self, factory):
        statuses = []
        client = DiscordGatewayClient('offline-test', ['123', '456'], lambda msg: None, statuses.append)
        client.is_running = True
        with patch('discord_listener.websockets.connect', side_effect=factory) as connect, \
             patch.object(client, '_wait_before_retry', AsyncMock(side_effect=[True, True, True, True, False])) as wait, \
             patch('discord_listener.random.uniform', return_value=0):
            await client._connect_gateway()
        self.assertFalse(client.is_running)
        self.assertEqual(connect.call_count, 5)
        self.assertEqual(connect.call_args.kwargs["max_size"], 16 * 1024 * 1024)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [10, 20, 40, 60, 300])
        self.assertIn('等待网络恢复', statuses[-1])
        return client

    async def test_missing_proxy_dependency_stops_without_retry(self):
        statuses = []
        client = DiscordGatewayClient('offline', '123', lambda m: None, statuses.append)
        client.is_running = True
        with patch('discord_listener.websockets.connect', side_effect=ImportError('python-socks')), \
             patch.object(client, '_wait_before_retry', AsyncMock()) as wait:
            await client._connect_gateway()
        wait.assert_not_called()
        self.assertFalse(client.is_running)
        self.assertIn('python-socks', statuses[-1])

    async def test_network_failures_cool_down(self):
        await self.run_failures(OSError)

    async def test_network_retries_after_cooldown_without_manual_reset(self):
        client = DiscordGatewayClient('offline', '123', lambda m: None)
        client.is_running = True
        with patch('discord_listener.websockets.connect', side_effect=OSError) as connect, \
             patch.object(client, '_wait_before_retry', AsyncMock(side_effect=[True] * 5 + [False])) as wait, \
             patch('discord_listener.random.uniform', return_value=0):
            await client._connect_gateway()
        self.assertEqual(connect.call_count, 6)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [10, 20, 40, 60, 300, 10])

    async def test_invalid_hello_uses_same_budget(self):
        await self.run_failures(lambda *a, **k: Socket([{'op': 0}]))

    async def test_server_reconnects_and_short_ready_do_not_reset(self):
        for opcode in (7, 9):
            with self.subTest(opcode=opcode):
                await self.run_failures(lambda *a, **k: Socket([
                    {'op': 10, 'd': {'heartbeat_interval': 45000}},
                    {'op': 0, 't': 'READY', 'd': {'session_id': 'offline'}},
                    {'op': opcode, 'd': False},
                ]))

    async def test_rate_limit_and_auth_stop_immediately(self):
        for code in (4004, 4008):
            exc = RuntimeError('offline')
            exc.rcvd = SimpleNamespace(code=code)
            client = DiscordGatewayClient('offline', '123', lambda m: None)
            client.is_running = True
            with patch('discord_listener.websockets.connect', side_effect=exc) as connect, \
                 patch.object(client, '_wait_before_retry', AsyncMock()) as wait:
                await client._connect_gateway()
            self.assertFalse(client.is_running)
            connect.assert_called_once()
            wait.assert_not_called()

    async def test_gateway_http_429_stops_without_retry(self):
        exc = RuntimeError('offline')
        exc.response = SimpleNamespace(status_code=429)
        client = DiscordGatewayClient('offline', '123', lambda m: None)
        client.is_running = True
        with patch('discord_listener.websockets.connect', side_effect=exc) as connect, \
             patch.object(client, '_wait_before_retry', AsyncMock()) as wait:
            await client._connect_gateway()
        connect.assert_called_once()
        wait.assert_not_called()
        self.assertFalse(client.is_running)

    async def test_stop_or_offhours_interrupt_wait(self):
        client = DiscordGatewayClient('offline', '123', lambda m: None)
        client.is_running = False
        self.assertFalse(await client._wait_before_retry(60))
        client.is_running = True
        client.should_connect = lambda: False
        self.assertFalse(await client._wait_before_retry(60))

    async def test_stable_session_resets_failure_count(self):
        client = DiscordGatewayClient('offline', '123', lambda m: None)
        client.is_running = True
        client.reconnect_count = 4
        client.STABLE_SECONDS = 0
        socket = Socket([
            {'op': 10, 'd': {'heartbeat_interval': 45000}},
            {'op': 0, 't': 'RESUMED', 'd': {}},
            {'op': 7},
        ])
        with patch('discord_listener.websockets.connect', return_value=socket), \
             patch.object(client, '_wait_before_retry', AsyncMock(return_value=False)) as wait:
            await client._connect_gateway()
        self.assertEqual(client.reconnect_count, 1)
        wait.assert_awaited_once()
