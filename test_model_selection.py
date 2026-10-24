import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import catalog_mcp as catalog


def metadata(model):
    return {'models': {'currentModelId': model, 'availableModels': [
        {'modelId': 'old', 'name': 'Old'}, {'modelId': 'new', 'name': 'New'}]}}


class ModelSelectionTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(catalog, 'workbuddy_set_model', None)), 'model selection tool is missing')
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / 'desktop_session.json'
        self.config.write_text(json.dumps({'model': '', 'take_writer': True}))
        self.writer = MagicMock()
        self.reader = MagicMock()
        self.reader.request.return_value = metadata('new')
        self.calls = []
        self.active_lock = False
        def request(method, params, **kwargs):
            self.assertTrue(self.active_lock)
            self.calls.append((method, params))
            if method == 'session/load':
                return metadata('old')
            self.assertEqual(method, 'session/set_model')
            self.assertEqual(params, {'sessionId': 'sid', 'modelId': 'new'})
            return {'modelId': 'new'}
        self.writer.request.side_effect = request
        @contextlib.contextmanager
        def session_lock():
            self.active_lock = True
            try:
                yield
            finally:
                self.active_lock = False
        replacements = [
            patch.object(catalog.bridge, 'desktop_turn_state', return_value='idle', create=True),
            patch.object(catalog.bridge, 'ROOT', self.root),
            patch.object(catalog.bridge, 'native_session_lock', session_lock),
            patch.object(catalog.bridge, 'discover_native_server', return_value=('server', 'sid', 'native-cwd')),
            patch.object(catalog, 'CatalogClient', side_effect=[self.writer, self.reader])]
        for p in replacements:
            p.start()
            self.addCleanup(p.stop)

    def run_change(self, model='new', expected='old'):
        return asyncio.run(catalog.workbuddy_set_model(model, expected))

    def test_switch_validates_and_reads_back_on_fresh_connection(self):
        result = self.run_change()
        self.assertTrue(result['ok'])
        self.assertTrue(result['changed'])
        self.assertEqual(result['previous_model_id'], 'old')
        self.assertEqual(result['current_model_id'], 'new')
        self.assertTrue(result['verified_after_reconnect'])
        self.assertEqual([name for name, _ in self.calls], ['session/load', 'session/set_model'])
        self.assertNotIn('_meta', self.calls[0][1])
        self.assertNotIn('_meta', self.reader.request.call_args.args[1])
        self.writer.close.assert_called_once()
        self.reader.close.assert_called_once()
        self.writer.prompt.assert_not_called()
        self.assertFalse(self.active_lock)
        self.assertEqual(json.loads(self.config.read_text())['model'], '')

    def test_unknown_model_never_sends_switch(self):
        result = self.run_change('unknown')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'model_not_available')
        self.assertEqual(len(self.calls), 1)

    def test_stale_expected_model_never_sends_switch(self):
        result = self.run_change(expected='stale')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'model_changed')
        self.assertEqual(result['current_model_id'], 'old')
        self.assertEqual(len(self.calls), 1)

    def test_same_model_is_noop(self):
        result = self.run_change('old')
        self.assertTrue(result['ok'])
        self.assertFalse(result['changed'])
        self.assertEqual(len(self.calls), 1)

    def test_binding_override_cannot_silently_undo_selection(self):
        self.config.write_text(json.dumps({'model': 'old', 'take_writer': True}))
        result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'binding_model_override')
        self.assertEqual(len(self.calls), 1)

    def test_no_writer_handoff_without_existing_opt_in(self):
        self.config.write_text(json.dumps({'model': ''}))
        self.assertTrue(self.run_change()['ok'])
        self.assertNotIn('_meta', self.calls[0][1])

    def test_uncertain_switch_is_not_retried_or_rolled_back(self):
        def request(method, params, **kwargs):
            self.calls.append((method, params))
            if method == 'session/load':
                return metadata('old')
            raise TimeoutError('DO_NOT_RETURN')
        self.writer.request.side_effect = request
        result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertTrue(result['change_may_have_applied'])
        self.assertFalse(result['retry_safe'])
        self.assertNotIn('current_model_id', result)
        self.assertNotIn('DO_NOT_RETURN', str(result))
        self.assertEqual([method for method, _ in self.calls].count('session/set_model'), 1)
        self.writer.close.assert_called_once()

    def test_wrong_readback_is_not_success(self):
        self.reader.request.return_value = metadata('old')
        result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'model_verification_failed')
        self.assertEqual(result['current_model_id'], 'old')
        self.assertTrue(result['change_may_have_applied'])

    def test_busy_rejected_before_connection_or_change(self):
        with patch.object(catalog.bridge, 'discover_native_server', side_effect=RuntimeError('busy')):
            result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertFalse(result['change_may_have_applied'])
        self.writer.connect.assert_not_called()

    def test_desktop_becomes_busy_after_load_blocks_model_change(self):
        with patch.object(catalog.bridge, 'desktop_turn_state', side_effect=['idle', 'busy'], create=True):
            result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertFalse(result['change_may_have_applied'])
        self.assertEqual([method for method, _ in self.calls], ['session/load'])

    def test_writer_conflict_is_never_taken_over_even_with_old_binding(self):
        self.writer.request.side_effect = RuntimeError('writer occupied')
        result = self.run_change()
        self.assertFalse(result['ok'])
        self.assertFalse(result['change_may_have_applied'])
        self.writer.request.assert_called_once()
        self.assertNotIn('_meta', self.writer.request.call_args.args[1])

    def test_bad_arguments_never_connect(self):
        for model, expected in [('', 'old'), ('new', ''), ('x\n', 'old'), ('x' * 201, 'old')]:
            with self.assertRaises(ValueError):
                self.run_change(model, expected)
        self.writer.connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
