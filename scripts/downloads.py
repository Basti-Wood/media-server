"""qBittorrent Web API via its container; no published port or Docker socket needed."""
import json
import subprocess
import time
from urllib.parse import urlencode

CATEGORIES = {'movies': '/data/torrents/movies', 'tv': '/data/torrents/tv'}
PREFERENCES = {'save_path': '/data/torrents', 'temp_path': '/data/torrents/incomplete',
               'temp_path_enabled': True, 'auto_tmm_enabled': True,
               # A changed default path must not relocate an existing collection.
               'save_path_changed_tmm_enabled': False, 'category_changed_tmm_enabled': False}


class DownloadError(RuntimeError):
    pass


class Qbit:
    def __init__(self, prefix):
        self.prefix = prefix

    def request(self, endpoint, data=None, json_response=True):
        config = ['url = ' + json.dumps('http://127.0.0.1:8080/api/v2/' + endpoint),
                  'header = "Origin: http://127.0.0.1:8080"']
        if data is not None:
            config += ['request = "POST"', 'data = ' + json.dumps(urlencode(data))]
        try:
            result = subprocess.run([*self.prefix, '--silent', '--show-error', '--fail', '--max-time', '20',
                                     '--config', '-'], input='\n'.join(config) + '\n', text=True,
                                    capture_output=True, check=True)
            return (json.loads(result.stdout) if result.stdout.strip() else None) if json_response else result.stdout
        except (OSError, subprocess.CalledProcessError, ValueError):
            raise DownloadError('qBittorrent API could not be reached from its own container. '
                                'Check that it is running and localhost API access is enabled; no host port is required.') from None

    def ready(self, attempts=30):
        for attempt in range(attempts):
            try:
                return self.request('app/preferences')
            except DownloadError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(2)


def configure(client, categories=None):
    wanted = {**CATEGORIES, **(categories or {})}
    current = client.request('torrents/categories')
    # Never move an existing category's files just to make a setup rerun pass.
    conflicts = [name for name, path in wanted.items()
                 if name in current and current[name].get('savePath', '').rstrip('/') != path]
    if conflicts:
        raise DownloadError('Existing qBittorrent categories use other save paths: ' + ', '.join(conflicts) +
                            '. Review/migrate them in qBittorrent first, then rerun ./start.sh. Existing files were not moved.')
    before = client.request('app/preferences')
    paths = ('save_path', 'temp_path', 'temp_path_enabled')
    changed_paths = [k for k in paths if (before.get(k, '').rstrip('/') if k != 'temp_path_enabled'
                                         else before.get(k)) != PREFERENCES[k]]
    if changed_paths and client.request('torrents/info'):
        raise DownloadError('qBittorrent already has torrents and its download/incomplete paths differ. '
                            'Review and migrate the paths in qBittorrent first, then rerun ./start.sh. '
                            'Existing files were not moved.')
    client.request('app/setPreferences', {'json': json.dumps(PREFERENCES)})
    for name, path in wanted.items():
        if name not in current:
            client.request('torrents/createCategory', {'category': name, 'savePath': path})
    saved = client.request('app/preferences')
    saved_categories = client.request('torrents/categories')
    if any((saved.get(k, '').rstrip('/') if k in {'save_path', 'temp_path'} else saved.get(k)) != v
           for k, v in PREFERENCES.items()) or any(
            saved_categories.get(k, {}).get('savePath', '').rstrip('/') != v for k, v in wanted.items()):
        raise DownloadError('qBittorrent did not confirm the configured local paths. Check its Downloads settings.')
    return wanted
