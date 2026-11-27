"""The analytics replacement must preserve scheduling without opening sockets."""
import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch


class LocalAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_errors_and_scheduling_need_no_network(self):
        root = Path(__file__).resolve().parents[1]
        src = root / 'pkg/patch/windows_mcp/infrastructure/analytics.py'
        spec = importlib.util.spec_from_file_location('kuding_local_analytics_test', src)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'UIA_DESKTOP_CALL_LOG': ''}), patch.object(socket.socket, 'connect', side_effect=AssertionError('Unexpected network connection')):
            spec.loader.exec_module(module)
            self.assertNotIn('posthog', sys.modules)
            local = module.PostHogAnalytics()
            self.assertIsNone(local.client)
            self.assertEqual(local.user_id, 'local')
            main_thread = threading.get_ident()
            observed = []
            def work():
                observed.append(threading.get_ident())
                return 42
            result = await module.with_analytics(local, 'test')(work)()
            self.assertEqual(result, 42)
            self.assertNotEqual(observed[0], main_thread)
            def fail():
                raise ValueError('synthetic test error')
            with self.assertRaisesRegex(ValueError, 'synthetic test error'):
                await module.with_analytics(local, 'test')(fail)()
            self.assertFalse(await local.is_feature_enabled('anything'))


if __name__ == '__main__':
    unittest.main()
