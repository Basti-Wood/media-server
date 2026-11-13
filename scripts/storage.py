#!/usr/bin/env python3
"""Prepare and inspect the local bind-mounted media disk, never move existing media."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
from setup import read_env

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ('torrents', 'torrents/incomplete', 'torrents/movies', 'torrents/tv',
               'torrents/music', 'media', 'media/movies', 'media/tv', 'media/music',
               'media/kids', 'media/movies favorite')


def data_root(root, env):
    value = env.get('DATA_ROOT', './content').strip()
    if not value:
        raise ValueError('DATA_ROOT cannot be empty; use ./content or an absolute local directory.')
    # Do not expand shell variables: Docker and this script must resolve the same path.
    if '$' in value or value.startswith('~') or '\n' in value:
        raise ValueError('Use a literal DATA_ROOT path, without ~ or shell variables.')
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def prepare(root, env):
    base = data_root(root, env)
    uid, gid = int(env.get('PUID', '1000')), int(env.get('PGID', '1000'))
    if min(uid, gid) < 0:
        raise ValueError('PUID and PGID must be non-negative integers.')
    # Existing directories/ACLs are preserved. Only new folders get explicit ownership/mode.
    for path in (base, *(base / name for name in DIRECTORIES)):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o2775)
            if os.geteuid() == 0:
                os.chown(path, uid, gid)
        if not path.is_dir():
            raise ValueError(f'Expected a directory: {path}')
    if os.geteuid() != 0 and uid != os.geteuid():
        raise ValueError(f'PUID={uid} differs from the user running setup ({os.geteuid()}). '
                         'Run setup as the media owner, or use sudo to create new directories for PUID/PGID.')
    return inspect(root, env)


def inspect(root, env):
    base = data_root(root, env)
    required = [base / name for name in DIRECTORIES]
    missing = [str(p) for p in required if not p.is_dir()]
    if missing:
        raise ValueError('Storage folders are missing. Run ./start.sh first: ' + ', '.join(missing))
    try:
        with tempfile.TemporaryDirectory(prefix='.media-check-', dir=base / 'torrents/incomplete') as tmp:
            source = Path(tmp) / 'probe'
            source.write_bytes(b'local storage probe\n')
            for name in ('torrents/movies', 'torrents/tv', 'media/movies', 'media/tv'):
                with tempfile.NamedTemporaryFile(prefix='.media-check-', dir=base / name):
                    pass
            with tempfile.TemporaryDirectory(prefix='.media-check-', dir=base / 'media') as target:
                try:
                    os.link(source, Path(target) / 'probe')
                    hardlinks = True
                except OSError:
                    hardlinks = False
    except OSError as error:
        quoted = shlex.quote(str(base))
        raise ValueError(f'Storage is not writable: {base}. Check ownership for '
                         f'{env.get("PUID", "1000")}:{env.get("PGID", "1000")} and directory/ACL permissions on {quoted}.') from error
    disk = shutil.disk_usage(base)
    return {'root': str(base), 'hardlinks': hardlinks, 'free_bytes': disk.free,
            'movies': str(base / 'media/movies'), 'series': str(base / 'media/tv')}


def container_checks(compose, uid, gid):
    """Verify with the apps' real UID/GID, not Docker's default root user."""
    script = '''set -eu
for dir in /data/torrents/incomplete /data/torrents/movies /data/torrents/tv /data/media/movies /data/media/tv; do
  test -d "$dir" && test -w "$dir" || { echo "Not writable: $dir" >&2; exit 1; }
done
probe=$(mktemp /data/torrents/incomplete/.media-check-XXXXXX)
trap 'rm -f "$probe"' EXIT
'''
    for service in ('qbittorrent', 'sonarr', 'radarr'):
        compose('exec', '-T', '--user', f'{uid}:{gid}', service, 'sh', '-c', script)
    compose('exec', '-T', '--user', f'{uid}:{gid}', 'jellyfin', 'sh', '-c',
            'test -r /data/media/movies && test -x /data/media/movies && '
            'test -r /data/media/tv && test -x /data/media/tv')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='Create missing media directories before containers start.')
    args = parser.parse_args()
    env = read_env(ROOT / '.env')
    report = prepare(ROOT, env) if args.prepare else inspect(ROOT, env)
    print(json.dumps(report, indent=2))
    if not report['hardlinks']:
        print('Hardlinks are unavailable; imports can copy files and need additional disk space.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
