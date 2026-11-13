import importlib.util
from pathlib import Path
import tempfile
import os
import sys
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


setup = module('setup')
profiles = module('language_profiles')
# Public fixture only, never a deployment credential.
DIGEST = '$argon2id$v=19$m=65536,t=3,p=4$Hjc8e7WYcBFcJmEDUOsS9A$ozM7RyZR1EyDR8cuyVpDDfmLrGPGFgo5E2NNqRumui4'


class SetupTests(unittest.TestCase):
    def test_generates_private_config_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'deploy').mkdir()
            (root / 'deploy/authelia.template.yml').write_text((ROOT / 'deploy/authelia.template.yml').read_text())
            setup.generate(root, 'media.home.arpa', 'admin', DIGEST, 'admin@example.com')
            folder = root / 'provision/authelia'
            config = yaml.safe_load((folder / 'configuration.yml').read_text())
            self.assertEqual(config['session']['remember_me'], '30d')
            self.assertEqual(config['access_control']['default_policy'], 'deny')
            self.assertEqual((folder / 'storage.secret').stat().st_mode & 0o777, 0o600)
            self.assertEqual(len((folder / 'session.secret').read_text()), 64)
            with self.assertRaises(ValueError):
                setup.generate(root, 'media.home.arpa', 'admin', DIGEST, 'admin@example.com')

    def test_vpn_files_share_one_key_and_keep_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup.vpn_files(root)
            vpn = root / 'provision/vpn'
            key = (vpn / 'control.key').read_text()
            self.assertIn(f'apikey = "{key}"', (root / 'provision/gluetun/auth/config.toml').read_text())
            self.assertIn('"GET /v1/vpn/settings"', (root / 'provision/gluetun/auth/config.toml').read_text())
            for folder in (vpn, root / 'provision/portal'):
                self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((vpn / 'openvpn_user').read_text(), setup.PLACEHOLDER)
            (vpn / 'openvpn_user').write_text('real')
            setup.vpn_files(root)
            self.assertEqual((vpn / 'control.key').read_text(), key)
            self.assertEqual((vpn / 'openvpn_user').read_text(), 'real')

    def test_folders_owned_by_someone_else_are_reported_before_any_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'provision/authelia').mkdir(parents=True)
            owner = (root / 'provision/authelia').stat().st_uid
            with patch.object(setup.os, 'geteuid', return_value=owner):
                setup.check_owner(root)
            with patch.object(setup.os, 'geteuid', return_value=owner + 1), \
                    self.assertRaisesRegex(ValueError, 'sudo chown -R "\\$USER": provision/authelia$'):
                setup.check_owner(root)

    def test_example_files_from_authelia_are_set_aside_and_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'deploy').mkdir()
            (root / 'deploy/authelia.template.yml').write_text((ROOT / 'deploy/authelia.template.yml').read_text())
            (root / '.env').write_text(f'MEDIA_DOMAIN=media.example.com\nPUID={os.geteuid()}\nPGID={os.getegid()}\n')
            folder = root / 'provision/authelia'
            folder.mkdir(parents=True)
            (folder / 'configuration.yml').write_text('# Authelia example\n')
            (folder / 'users_database.yml').write_text('users: {}\n')
            with patch.object(setup, 'ROOT', root), patch('sys.argv', ['setup.py']), patch.object(setup.os, 'umask'), \
                    patch('builtins.input', side_effect=['admin', 'admin@example.com']), \
                    patch.object(setup.getpass, 'getpass', return_value=DIGEST):
                setup.main()
                self.assertIn('media_session', (folder / 'configuration.yml').read_text())
                self.assertEqual((folder / 'configuration.yml.authelia-example').read_text(), '# Authelia example\n')
                with patch('builtins.input', side_effect=AssertionError('asked again')):
                    setup.main()

    def test_authelia_config_follows_the_template_on_every_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'deploy').mkdir()
            (root / 'deploy/authelia.template.yml').write_text((ROOT / 'deploy/authelia.template.yml').read_text())
            (root / 'provision/authelia').mkdir(parents=True)
            (root / 'provision/authelia/configuration.yml').write_text('old media_session')
            setup.write_config(root, 'media.example.com')
            config = yaml.safe_load((root / 'provision/authelia/configuration.yml').read_text())
            self.assertEqual(config['session']['cookies'][0]['authelia_url'], 'https://media.example.com/auth')
            self.assertEqual(config['server']['address'], 'tcp://:9091/auth')

    def test_login_check_runs_before_identity_headers_are_trusted(self):
        caddy = (ROOT / 'deploy/Caddyfile').read_text()
        snippet = caddy[caddy.index('(protected) {'):caddy.index('(app) {')]
        # Outside `route`, Caddy strips the headers after forward_auth set them, so the portal saw no user.
        self.assertLess(snippet.index('route {'), snippet.index('request_header -Remote-User'))
        self.assertLess(snippet.index('request_header -Remote-Name'), snippet.index('forward_auth authelia'))

    def test_domain_injection_rejected(self):
        for value in ['localhost', 'example.com', 'x\npolicy: bypass', 'media.*.com', 'media.bad-.com']:
            with self.assertRaises(ValueError):
                setup.validate_domain(value)

    def test_env_is_data_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / '.env'
            p.write_text("JELLYFIN_API_KEY='$(touch unexpected)'\n")
            self.assertEqual(setup.read_env(p)['JELLYFIN_API_KEY'], '$(touch unexpected)')


class ProfileTests(unittest.TestCase):
    def test_strict_profile_has_no_fallback_and_does_not_mutate_template(self):
        template = {'id': 1, 'items': [{'quality': {'id': 4, 'name': 'HDTV-1080p'}, 'allowed': True},
            {'id': 20, 'items': [{'quality': {'id': 5, 'name': 'WEBDL-1080p'}, 'allowed': False}]}]}
        result = profiles.strict_profile(template, 9, 'German', True)
        self.assertEqual(result['minFormatScore'], 10000)
        self.assertEqual(result['formatItems'][0]['score'], 10000)
        self.assertFalse(result['items'][0]['allowed'])
        self.assertTrue(result['items'][1]['items'][0]['allowed'])
        self.assertEqual(result['cutoff'], 20)
        self.assertEqual(result['language']['name'], 'Any')
        self.assertFalse(template['items'][1]['items'][0]['allowed'])

    def test_unknown_quality_schema_fails_safely(self):
        with self.assertRaises(ValueError):
            profiles.strict_profile({'items': []}, 9, 'German', False)

    def test_rerun_preserves_existing_profiles(self):
        def call(endpoint, data=None):
            self.assertIsNone(data)
            return {'language': [{'name': x, 'id': i} for i, x in enumerate(profiles.LANGUAGES)],
                'customformat/schema': [{'implementation': 'LanguageSpecification'}],
                'qualityprofile/schema': [{}], 'customformat': [],
                'qualityprofile': [{'name': profiles.PREFIX + x} for x in profiles.LANGUAGES]}[endpoint]
        self.assertTrue(all('unchanged' in x for x in profiles.create_profiles(call)))


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stack = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
        cls.services = cls.stack['services']

    def test_downloads_cannot_bypass_vpn_namespace(self):
        q = self.services['qbittorrent']
        self.assertEqual(q['network_mode'], 'service:gluetun')
        self.assertNotIn('networks', q)
        self.assertNotIn('ports', q)
        self.assertEqual(q['depends_on']['gluetun']['condition'], 'service_healthy')
        # Optional only so the stack starts before Surfshark is connected; the firewall still blocks.
        self.assertFalse(q['depends_on']['gluetun']['required'])
        self.assertEqual(self.services['gluetun']['environment']['FIREWALL'], 'on')

    def test_portal_and_authentication_are_not_published(self):
        for key in ['portal', 'authelia', 'redis']:
            self.assertNotIn('ports', self.services[key])
        self.assertEqual(self.services['caddy']['ports'], ['${CADDY_BIND_IP:-127.0.0.1}:80:80'])
        self.assertTrue(self.stack['networks']['auth-network']['internal'])

    def test_admin_ports_are_loopback_only(self):
        for name, service in self.services.items():
            if name in {'jellyfin', 'caddy'}:
                continue
            for port in service.get('ports', []):
                self.assertTrue(port.startswith('127.0.0.1:'), (name, port))

    def test_optional_services_are_opt_in(self):
        for name in ['ombi', 'jackett', 'huntarr', 'suggestarr', 'heimdall', 'lidarr', 'flaresolverr']:
            self.assertEqual(self.services[name]['profiles'], ['extras'])

    def test_shared_data_mount_and_least_privilege(self):
        for name in ('qbittorrent', 'sonarr', 'radarr', 'bazarr'):
            mount = next(v for v in self.services[name]['volumes'] if isinstance(v, dict) and v['target'] == '/data')
            self.assertEqual(mount['source'], '${DATA_ROOT:-./content}')
            self.assertFalse(mount['bind']['create_host_path'])
            self.assertEqual(self.services[name]['environment']['UMASK'], '002')
        media = next(v for v in self.services['jellyfin']['volumes'] if isinstance(v, dict))
        self.assertEqual(media['source'], '${DATA_ROOT:-./content}/media')
        self.assertTrue(media['read_only'])
        self.assertNotIn('privileged', self.services['jellyfin'])


if __name__ == '__main__':
    unittest.main()

