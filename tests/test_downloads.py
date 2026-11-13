import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import downloads
import storage
import connect_apps as apps


class FakeClient:
    def __init__(self, categories=None):
        self.categories = copy.deepcopy(categories or {})
        self.preferences = {}
        self.writes = []

    def request(self, endpoint, data=None):
        import json
        if data is not None:
            self.writes.append((endpoint, data))
        if endpoint == 'torrents/categories':
            return self.categories
        if endpoint == 'app/setPreferences':
            self.preferences.update(json.loads(data['json']))
        if endpoint == 'app/preferences':
            return self.preferences
        if endpoint == 'torrents/createCategory':
            self.categories[data['category']] = {'savePath': data['savePath']}


class DownloadTests(unittest.TestCase):
    def test_completed_movies_series_and_incomplete_paths_are_verified(self):
        client = FakeClient()
        downloads.configure(client)
        self.assertEqual(client.categories['movies']['savePath'], '/data/torrents/movies')
        self.assertEqual(client.categories['tv']['savePath'], '/data/torrents/tv')
        self.assertTrue(client.preferences['temp_path_enabled'])
        self.assertEqual(client.preferences['temp_path'], '/data/torrents/incomplete')
        self.assertFalse(client.preferences['save_path_changed_tmm_enabled'])

    def test_existing_custom_category_is_not_moved(self):
        client = FakeClient({'movies': {'savePath': '/existing/downloads'}})
        with self.assertRaisesRegex(downloads.DownloadError, 'Existing files were not moved'):
            downloads.configure(client)
        self.assertFalse(client.writes)

    def test_existing_queued_category_can_be_preserved(self):
        client = FakeClient()
        downloads.configure(client, {'tv-sonarr': '/data/torrents/tv'})
        self.assertEqual(client.categories['tv-sonarr']['savePath'], '/data/torrents/tv')

    def test_missing_readback_fails_instead_of_claiming_success(self):
        client = FakeClient()
        real = client.request
        client.request = lambda endpoint, data=None: {} if endpoint == 'app/preferences' else real(endpoint, data)
        with self.assertRaises(downloads.DownloadError):
            downloads.configure(client)


class StorageTests(unittest.TestCase):
    def test_local_override_keeps_one_root_and_handles_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {'DATA_ROOT': 'my media', 'PUID': str(os.geteuid()), 'PGID': str(os.getegid())}
            report = storage.prepare(root, env)
            self.assertEqual(report['root'], str(root / 'my media'))
            self.assertTrue(report['hardlinks'])
            self.assertTrue((root / 'my media/torrents/incomplete').is_dir())
            self.assertFalse(list((root / 'my media').rglob('.media-check-*')))
            existing = root / 'my media/media/movies/keep.mkv'
            existing.write_bytes(b'keep this media')
            storage.prepare(root, env)
            self.assertEqual(existing.read_bytes(), b'keep this media')

    def test_default_storage_is_backwards_compatible(self):
        self.assertEqual(storage.data_root(ROOT, {}), ROOT / 'content')

    def test_ambiguous_path_is_rejected(self):
        for value in ['', '$HOME/media', '~/media', 'media\nother']:
            with self.assertRaises(ValueError):
                storage.data_root(ROOT, {'DATA_ROOT': value})

    def test_probe_runs_as_media_user_in_containers(self):
        compose = Mock()
        storage.container_checks(compose, '1234', '5678')
        self.assertEqual(compose.call_count, 4)
        for call in compose.call_args_list:
            self.assertIn('1234:5678', call.args)
        self.assertEqual(compose.call_args_list[-1].args[4], 'jellyfin')


class ConnectionTests(unittest.TestCase):
    def test_repair_old_downloader_host_without_changing_queued_category(self):
        original = {'id': 7, 'name': 'Existing local downloader', 'implementation': 'QBittorrent', 'enable': True,
                    'fields': [{'name': 'host', 'value': 'qbittorrent'}, {'name': 'port', 'value': 8080},
                               {'name': 'tvCategory', 'value': 'sonarr'}]}
        def api(url, key=None, body=None, method=None):
            if body is None:
                return [original]
            self.assertEqual(method, 'PUT')
            self.assertTrue(url.endswith('/downloadclient/7'))
            return body
        with patch.object(apps, 'api', side_effect=api):
            saved = apps.add_missing('http://local/sonarr/api/v3', 'key', 'downloadclient', 'QBittorrent',
                                     {'host': 'gluetun', 'port': 8080, 'tvCategory': 'tv'}, 'qBittorrent')
        self.assertEqual(saved['name'], original['name'])
        fields = {f['name']: f['value'] for f in saved['fields']}
        self.assertEqual(fields['host'], 'gluetun')
        self.assertEqual(fields['tvCategory'], 'sonarr')

    def test_local_app_url_is_repaired_after_base_path_change(self):
        item = {'id': 4, 'name': 'Sonarr', 'implementation': 'Sonarr', 'enable': True,
                'fields': [{'name': 'baseUrl', 'value': 'http://sonarr:8989'}]}
        with patch.object(apps, 'api', side_effect=[[item], {**item, 'fields': [{'name': 'baseUrl', 'value': 'http://sonarr:8989/sonarr'}]}]) as api:
            apps.add_missing('http://local/prowlarr/api/v1', 'key', 'applications', 'Sonarr',
                             {'baseUrl': 'http://sonarr:8989/sonarr'}, 'Sonarr')
        self.assertEqual(api.call_args.args[-1], 'PUT')

    def test_completed_import_and_hardlinks_are_enabled(self):
        with patch.object(apps, 'api', side_effect=[{'id': 1, 'copyUsingHardlinks': False}, {},
                                                   {'id': 1, 'enableCompletedDownloadHandling': False}, {}]) as api:
            apps.arr_import_settings('http://local/api/v3', 'key')
        writes = [c for c in api.call_args_list if len(c.args) > 2]
        self.assertTrue(writes[0].args[2]['copyUsingHardlinks'])
        self.assertTrue(writes[1].args[2]['enableCompletedDownloadHandling'])

    def test_app_restarts_even_if_config_edit_fails(self):
        path = Mock()
        path.read_text.return_value = 'config'
        path.write_text.side_effect = OSError('read only')
        with patch.object(apps, 'compose') as compose, self.assertRaises(OSError):
            apps.while_stopped('sonarr', path, lambda text, dry: True if dry else 'new config')
        self.assertEqual([c.args for c in compose.call_args_list], [('stop', 'sonarr'), ('start', 'sonarr')])


if __name__ == '__main__':
    unittest.main()
