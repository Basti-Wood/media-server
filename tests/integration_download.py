"""Download owned fixture torrents into real local bind mounts using the pinned images.

The disposable qBittorrent container uses a local web seed without any public
tracker. It has no published port and no production VPN credentials. Production
VPN isolation is checked separately in test_setup.py.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from downloads import Qbit, configure
from storage import prepare


def bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(map(bencode, value)) + b'e'
    return b'd' + b''.join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b'e'


FILES = {'QA-Movie.mkv': b'owned movie fixture\n' * 60000,
         'QA-Series-S01E01.mkv': b'owned episode fixture\n' * 60000}
PAYLOADS = dict(FILES)


class Seed(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = PAYLOADS.get(self.path.removeprefix('/'))
        if data is None:
            self.send_error(404)
            return
        start, end = 0, len(data) - 1
        partial = self.headers.get('Range', '')
        if partial:
            start_text, _, end_text = partial.removeprefix('bytes=').partition('-')
            start, end = int(start_text or 0), int(end_text or end)
            end = min(end, len(data) - 1)
        self.send_response(206 if partial else 200)
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
        self.end_headers()
        self.wfile.write(data[start:end + 1])


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def main():
    stack = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    container = f'media-download-test-{os.getpid()}'
    server = ThreadingHTTPServer(('0.0.0.0', 0), Seed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://host.docker.internal:{server.server_port}'
    piece_length = 16384
    for name, data in FILES.items():
        pieces = b''.join(hashlib.sha1(data[i:i + piece_length]).digest() for i in range(0, len(data), piece_length))
        PAYLOADS[name + '.torrent'] = bencode({'url-list': [base + '/' + name],
            'info': {'name': name, 'length': len(data), 'piece length': piece_length, 'pieces': pieces, 'private': 1}})
    try:
        with tempfile.TemporaryDirectory(prefix='media-download-') as tmp:
            root = Path(tmp)
            env = {'DATA_ROOT': './data', 'PUID': str(os.geteuid()), 'PGID': str(os.getegid())}
            report = prepare(root, env)
            data = Path(report['root'])
            config = root / 'config/qBittorrent'
            config.mkdir(parents=True)
            (config / 'qBittorrent.conf').write_text('[WebUI]\nLocalHostAuth=false\n[LegalNotice]\nAccepted=true\n')
            run('docker', 'run', '-d', '--rm', '--name', container,
                '--add-host', 'host.docker.internal:host-gateway',
                '-e', f'PUID={os.geteuid()}', '-e', f'PGID={os.getegid()}', '-e', 'UMASK=002',
                '-v', f'{root / "config"}:/config', '-v', f'{data}:/data', stack['services']['qbittorrent']['image'])
            client = Qbit(['docker', 'exec', '-i', container, 'curl'])
            client.ready()
            configure(client)
            for name, category in [('QA-Movie.mkv', 'movies'), ('QA-Series-S01E01.mkv', 'tv')]:
                client.request('torrents/add', {'urls': base + '/' + name + '.torrent',
                    'category': category, 'autoTMM': 'true', 'contentLayout': 'Original'}, json_response=False)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                torrents = client.request('torrents/info')
                if len(torrents) == 2 and all(t['progress'] == 1 for t in torrents):
                    break
                time.sleep(1)
            else:
                raise AssertionError('Fixture torrents did not finish: ' + str([(t['name'], t['state'], t['progress']) for t in torrents]))
            for name, category in [('QA-Movie.mkv', 'movies'), ('QA-Series-S01E01.mkv', 'tv')]:
                downloaded = data / 'torrents' / category / name
                assert downloaded.read_bytes() == FILES[name], f'Wrong or missing local download: {downloaded}'
                target = data / 'media' / category / name
                os.link(downloaded, target)
                assert downloaded.stat().st_ino == target.stat().st_ino
            # Use Jellyfin's actual image and media user to check the read-only library bind.
            run('docker', 'run', '--rm', '--network', 'none', '--user', f'{os.geteuid()}:{os.getegid()}',
                '--entrypoint', '/bin/sh', '-v', f'{data / "media"}:/data/media:ro',
                stack['services']['jellyfin']['image'], '-c',
                'test -r /data/media/movies/QA-Movie.mkv && test -r /data/media/tv/QA-Series-S01E01.mkv')
            print('PASS: real fixture torrents downloaded to local movie/series folders; hardlinked library files are readable in the Jellyfin image.')
    finally:
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
