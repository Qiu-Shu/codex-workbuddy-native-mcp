import asyncio
import contextlib
import importlib
import importlib.util
import unittest
from unittest.mock import MagicMock, patch


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('catalog_mcp'), 'read-only catalog entrypoint is missing')
        self.catalog = importlib.import_module('catalog_mcp')
        fallback = patch.object(self.catalog, 'read_background_catalog', return_value={
            'ok': False, 'error_code': 'catalog_unavailable'})
        fallback.start()
        self.addCleanup(fallback.stop)
        self.client = MagicMock()
        self.client.request.return_value = {'models': {
            'currentModelId': 'model-1', 'apiKey': 'DO_NOT_RETURN',
            'availableModels': [{'modelId': 'model-1', 'name': 'Model One', 'description': 'Available',
                                 'apiKey': 'DO_NOT_RETURN', '_meta': {'token': 'DO_NOT_RETURN'}}]}}
        self.events = [{'params': {'sessionId': 'sid', 'update': {
            'sessionUpdate': 'available_commands_update', 'availableCommands': [
                {'name': 'clear', 'description': 'Delete context', '_meta': {'type': 'command'}},
                {'name': 'legal-review', 'description': '合同审查',
                 '_meta': {'type': 'skill', 'source': 'user', 'isBuiltIn': False, 'secret': 'DO_NOT_RETURN'},
                 'body': 'DO_NOT_RETURN'},
                {'name': 'workbuddy-docx', 'description': 'Create documents',
                 '_meta': {'type': 'skill', 'source': 'builtin', 'isBuiltIn': True}}]}}}]
        self.locked = False
        @contextlib.contextmanager
        def session_lock():
            self.locked = True
            try:
                yield
            finally:
                self.locked = False
        def make_client(server, **kwargs):
            self.assertTrue(self.locked)
            callback = kwargs['event_callback']
            def load(method, params, **options):
                self.assertEqual(method, 'session/load')
                self.assertEqual(params, {'sessionId': 'sid', 'cwd': 'native-cwd', 'mcpServers': []})
                for event in self.events:
                    callback(event)
                return self.client.request.return_value
            self.client.request.side_effect = load
            return self.client
        for name, replacement in [
            ('desktop_turn_state', lambda sid: 'idle'),
            ('native_session_lock', session_lock),
            ('discover_native_server', lambda: ('server', 'sid', 'native-cwd'))]:
            p = patch.object(self.catalog.bridge, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(self.catalog, 'CatalogClient', make_client)
        p.start()
        self.addCleanup(p.stop)

    def test_model_catalog_exposes_only_public_fields_without_prompt_or_switch(self):
        result = asyncio.run(self.catalog.workbuddy_models())
        self.assertTrue(result['ok'])
        self.assertEqual(result['current_model_id'], 'model-1')
        self.assertEqual(result['models'], [{'modelId': 'model-1', 'name': 'Model One', 'description': 'Available'}])
        self.assertNotIn('DO_NOT_RETURN', str(result))
        self.client.prompt.assert_not_called()
        self.client.configure_session.assert_not_called()
        self.client.close.assert_called_once()
        self.assertFalse(self.locked)

    def test_skill_search_excludes_commands_and_keeps_origin(self):
        result = asyncio.run(self.catalog.workbuddy_skills(query='合同'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['total_skills'], 2)
        self.assertEqual(result['matched_skills'], 1)
        self.assertEqual(result['skills'][0], {'name': 'legal-review', 'description': '合同审查',
                                              'source': 'user', 'is_builtin': False})
        self.assertNotIn('DO_NOT_RETURN', str(result))

    def test_skill_list_preserves_builtin_skills_and_applies_limit(self):
        result = asyncio.run(self.catalog.workbuddy_skills(limit=1))
        self.assertEqual(result['matched_skills'], 2)
        self.assertEqual(len(result['skills']), 1)
        self.assertTrue(result['truncated'])

    def test_missing_or_other_session_events_are_not_reported_as_empty_catalog(self):
        self.events[0]['params']['sessionId'] = 'other-session'
        result = asyncio.run(self.catalog.workbuddy_skills())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'catalog_unavailable')
        self.client.close.assert_called_once()

    def test_empty_explicit_catalog_is_valid(self):
        self.events[0]['params']['update']['availableCommands'] = []
        result = asyncio.run(self.catalog.workbuddy_skills())
        self.assertTrue(result['ok'])
        self.assertEqual(result['skills'], [])

    def test_bad_models_fail_without_exposing_raw_payload(self):
        self.client.request.return_value = {'models': {'availableModels': 'DO_NOT_RETURN'}}
        result = asyncio.run(self.catalog.workbuddy_models())
        self.assertFalse(result['ok'])
        self.assertNotIn('DO_NOT_RETURN', str(result))

    def test_invalid_search_is_rejected_before_native_connection(self):
        for kwargs in ({'query': 'x' * 201}, {'limit': 0}, {'limit': 101}):
            with self.assertRaises(ValueError):
                asyncio.run(self.catalog.workbuddy_skills(**kwargs))
        self.client.connect.assert_not_called()

    def test_native_busy_is_reported_without_opening_client(self):
        with patch.object(self.catalog.bridge, 'discover_native_server', side_effect=RuntimeError('busy')):
            result = asyncio.run(self.catalog.workbuddy_models())
        self.assertFalse(result['ok'])
        self.client.connect.assert_not_called()
        self.assertFalse(self.locked)

    def test_transport_failure_closes_connection_without_returning_payload(self):
        self.client.connect.side_effect = RuntimeError('DO_NOT_RETURN')
        result = asyncio.run(self.catalog.workbuddy_models())
        self.assertFalse(result['ok'])
        self.assertNotIn('DO_NOT_RETURN', str(result))
        self.client.close.assert_called_once()
        self.assertFalse(self.locked)


class ReadOnlyPermissionsTest(unittest.TestCase):
    def test_catalog_client_denies_even_file_write_permission(self):
        catalog = importlib.import_module('catalog_mcp')
        client = object.__new__(catalog.CatalogClient)
        client._http = MagicMock()
        client.server = MagicMock(acp_endpoint='http://127.0.0.1/acp')
        client._headers = lambda: {}
        with self.assertRaisesRegex(RuntimeError, 'Unexpected permission'):
            client._grant_permission({'id': 7, 'params': {'toolCall': {'title': 'Write'},
                                     'options': [{'kind': 'allow_once', 'optionId': 'allow'}]}})
        sent = client._http.post.call_args.kwargs['json']
        self.assertEqual(sent['result'], {'outcome': {'outcome': 'cancelled'}})


if __name__ == '__main__':
    unittest.main()
