import asyncio
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import background_runtime as runtime
import catalog_mcp as catalog
import local_bridge as bridge


class CrossChatRoutesTest(unittest.TestCase):
    def test_status_reports_background_ready_when_shared_chat_has_no_endpoint(self):
        with patch.object(bridge, 'discover_native_server', side_effect=RuntimeError('no endpoint')), \
             patch.object(runtime, 'find_sidecar', return_value=('pipe', 10, Path('WorkBuddy.exe'))):
            result = asyncio.run(bridge.workbuddy_status())
        self.assertTrue(result.get('available'), result)
        self.assertFalse(result['shared_session']['connected'])
        self.assertTrue(result['background']['ready'])
        self.assertEqual(result['recommended_tool'], 'workbuddy_background_start')

    def test_no_arg_model_catalog_works_without_shared_chat_and_releases_worker(self):
        observed = {'released': False}
        @contextmanager
        def worker(cwd, **kwargs):
            try:
                yield object(), {}
            finally:
                observed['released'] = True
        client = MagicMock()
        client.new_session.return_value = 'probe-sid'
        client.request.return_value = {'models': {'currentModelId': 'auto', 'availableModels': [
            {'modelId': 'kimi-k3-2', 'name': 'Kimi', 'apiKey': 'SECRET'}]}}
        with TemporaryDirectory() as tmp, \
             patch.object(bridge, 'ROOT', Path(tmp)), \
             patch.object(bridge, 'discover_native_server', side_effect=RuntimeError('no endpoint')), \
             patch.object(runtime, 'owned_worker', worker), \
             patch.object(catalog, 'CatalogClient', return_value=client):
            result = asyncio.run(catalog.workbuddy_models())
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['scope'], 'background_catalog')
        self.assertEqual(result['current_model_applies_to'], 'catalog_probe_only')
        self.assertEqual(result['models'][0]['modelId'], 'kimi-k3-2')
        self.assertNotIn('SECRET', str(result))
        client.prompt.assert_not_called()
        self.assertTrue(observed['released'])
        client.close.assert_called_once()

    def test_update_guard_cannot_be_bypassed_by_catalog_fallback(self):
        with patch.object(bridge, 'SOURCE_SHA256', 'old'):
            result = asyncio.run(catalog.workbuddy_models())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'bridge_update_required')

    def test_explicit_cwd_never_accesses_shared_chat(self):
        with patch.object(catalog, 'read_catalog') as shared, \
             patch.object(catalog, 'read_background_catalog', return_value={'ok': True}) as independent:
            result = asyncio.run(catalog.workbuddy_models(cwd=str(Path.cwd())))
        self.assertTrue(result['ok'])
        shared.assert_not_called()
        independent.assert_called_once_with('models', str(Path.cwd()), '', 30)

    def test_metadata_failure_releases_worker_and_redacts_upstream_error(self):
        observed = []
        @contextmanager
        def worker(cwd, **kwargs):
            try:
                yield object(), {}
            finally:
                observed.append('released')
        client = MagicMock()
        client.connect.side_effect = RuntimeError('SECRET')
        with TemporaryDirectory() as tmp, patch.object(runtime, 'owned_worker', worker), \
             patch.object(catalog, 'CatalogClient', return_value=client):
            result = asyncio.run(catalog.workbuddy_models(cwd=tmp))
        self.assertFalse(result['ok'])
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(observed, ['released'])
        client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
