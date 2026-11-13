#!/usr/bin/env python3
"""Connect the apps to each other. Safe to run again: it only changes what is missing.

- Sonarr, Radarr, Prowlarr and Bazarr serve under media.<domain>/<name>; Authelia does the login.
- qBittorrent saves to /data/torrents, uses only the VPN (tun0), and trusts the Docker network.
- Sonarr/Radarr get their media folder and qBittorrent; Prowlarr sends indexers to both and
  searches through the VPN proxy; Bazarr is connected to both.
- Jellyfin gets Movies and Shows libraries once it is connected on the admin page.
"""
import configparser
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from setup import read_env
from storage import container_checks, inspect as inspect_storage
from downloads import Qbit, configure as configure_downloads, DownloadError

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'provision'
ARR = {'sonarr': (8989, 'v3'), 'radarr': (7878, 'v3'), 'prowlarr': (9696, 'v1')}
MEDIA = {'sonarr': ('/data/media/tv', 'tvCategory', 'tv'), 'radarr': ('/data/media/movies', 'movieCategory', 'movies')}


class AppError(RuntimeError):
    pass


def compose_command():
    return ['docker', 'compose'] if subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode == 0 else ['docker-compose']


def compose(*args):
    try:
        return subprocess.run([*compose_command(), *args], cwd=ROOT, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise AppError((error.stderr or '').strip()[:500] or 'Docker Compose command failed.') from None


def wait_for(path, what):
    for _ in range(60):
        if path.exists():
            return True
        time.sleep(2)
    print(f'! {what} has not created its settings yet; run ./start.sh again in a minute.')
    return False


def api(url, key=None, body=None, method=None, header='X-Api-Key'):
    req = Request(url, data=None if body is None else json.dumps(body).encode(), method=method or ('GET' if body is None else 'POST'),
                  headers={'Content-Type': 'application/json', **({header: key} if key else {})})
    try:
        with urlopen(req, timeout=30) as response:
            data = response.read()
    except HTTPError as error:
        try:
            detail = '; '.join(e.get('errorMessage', '') for e in json.loads(error.read()))
        except (ValueError, TypeError, AttributeError):
            detail = error.reason
        hint = ' Check DATA_ROOT ownership/permissions with python3 scripts/storage.py.' if 'not writable' in detail else ''
        raise AppError(f'{url.split("?")[0]} refused: {detail}.{hint}') from None
    return json.loads(data) if data.strip() else None


def ready(url, key):
    for _ in range(60):
        try:
            return api(url, key)
        except (OSError, ValueError, AppError):
            time.sleep(2)
    raise SystemExit(f'{url} did not answer. Check the app with: docker-compose logs')


def while_stopped(service, path, change):
    """Edit a settings file only while its app is stopped, so the app cannot overwrite it."""
    if change(path.read_text(), dry=True):
        compose('stop', service)
        try:
            path.write_text(change(path.read_text(), dry=False))
        finally:
            compose('start', service)
        print(f'- {service}: settings updated')


def arr_config(name):
    def change(text, dry):
        root = ET.fromstring(text)
        wanted = {'UrlBase': '/' + name, 'AuthenticationMethod': 'External'}
        if dry:
            return any((root.findtext(k) or '') != v for k, v in wanted.items())
        for k, v in wanted.items():
            (root.find(k) if root.find(k) is not None else ET.SubElement(root, k)).text = v
        return ET.tostring(root, encoding='unicode') + '\n'
    return change


def ini(wanted):
    def change(text, dry):
        conf = configparser.RawConfigParser(strict=False, delimiters=('=',))
        conf.optionxform = str
        conf.read_string(text)
        if dry:
            return any(not conf.has_section(s) or conf[s].get(k) != v for s, values in wanted.items() for k, v in values.items())
        for section, values in wanted.items():
            if not conf.has_section(section):
                conf.add_section(section)
            conf[section].update(values)
        out = []
        for section in conf.sections():
            out += [f'[{section}]', *(f'{k}={v}' for k, v in conf[section].items()), '']
        return '\n'.join(out)
    return change


def yaml_keys(wanted):
    """Set plain values in Bazarr's two-level config.yaml without a YAML library."""
    def change(text, dry):
        lines, section, missing = text.split('\n'), None, {s: dict(v) for s, v in wanted.items()}
        for i, line in enumerate(lines):
            if re.match(r'^\S.*:\s*$', line):
                section = line.rstrip(':').strip()
            m = re.match(r'^  (\w+):', line)
            if m and section in missing and m.group(1) in missing[section]:
                lines[i] = f'  {m.group(1)}: {missing[section].pop(m.group(1))}'
        changed = '\n'.join(lines) != text
        if any(missing.values()):
            raise SystemExit(f'Bazarr settings file has an unexpected layout: {missing}')
        return changed if dry else '\n'.join(lines)
    return change


def add_missing(base, key, kind, implementation, values, name):
    """Repair this stack's existing integrations while preserving unrelated clients."""
    existing = [x for x in api(f'{base}/{kind}', key) if x.get('implementation') == implementation]
    def local(item):
        fields = {f['name']: f.get('value') for f in item.get('fields', [])}
        if implementation == 'QBittorrent':
            return fields.get('host') in {'gluetun', 'qbittorrent'}
        return fields.get('baseUrl', '').startswith(f'http://{implementation.lower()}:')
    item = next((x for x in existing if local(x)), None)
    creating = item is None
    if creating:
        item = next(x for x in api(f'{base}/{kind}/schema', key) if x.get('implementation') == implementation)
    changed = creating or not item.get('enable', True)
    for field in item['fields']:
        if field['name'] in values:
            wanted = values[field['name']]
            # Keep the category used by torrents that are already queued.
            if implementation == 'QBittorrent' and field['name'] in {'tvCategory', 'movieCategory'} and field.get('value'):
                continue
            changed |= field.get('value') != wanted
            field['value'] = wanted
    if creating:
        names = {x.get('name') for x in existing}
        candidate, number = name, 1
        while candidate in names:
            candidate = f'{name} (local {number})'
            number += 1
        item['name'] = candidate
    item['enable'] = True
    if changed:
        suffix = '' if creating else '/' + str(item['id'])
        item = api(f'{base}/{kind}{suffix}', key, item, 'POST' if creating else 'PUT')
        print(f'- {name}: {"added" if creating else "connection repaired"}')
    return item


def arr_import_settings(base, key):
    for route, wanted in [('mediamanagement', {'copyUsingHardlinks': True}),
                          ('downloadclient', {'enableCompletedDownloadHandling': True})]:
        current = api(f'{base}/config/{route}', key)
        if any(current.get(k) != v for k, v in wanted.items()):
            api(f'{base}/config/{route}/{current["id"]}', key, {**current, **wanted}, 'PUT')


def main():
    env = read_env(ROOT / '.env')
    storage = inspect_storage(ROOT, env)
    keys = {}
    for name in ARR:
        config = P / name / 'config.xml'
        if wait_for(config, name.capitalize()):
            while_stopped(name, config, arr_config(name))
            keys[name] = ET.fromstring(config.read_text()).findtext('ApiKey')

    qbit = P / 'qbittorrent/qBittorrent/qBittorrent.conf'
    if wait_for(qbit, 'qBittorrent'):
        while_stopped('qbittorrent', qbit, ini({
            'BitTorrent': {'Session\\Interface': 'tun0'},
            # No published Web UI port. This API is reached inside the downloader namespace.
            'WebUI': {'LocalHostAuth': 'false', 'AuthSubnetWhitelistEnabled': 'true',
                      'AuthSubnetWhitelist': downloader_subnets()}}))

    client = Qbit([*compose_command(), '--project-directory', str(ROOT), 'exec', '-T', 'qbittorrent', 'curl'])
    client.ready()
    # Create the default categories before Sonarr/Radarr validate their download client.
    configure_downloads(client)
    container_checks(compose, env.get('PUID', '1000'), env.get('PGID', '1000'))
    categories = {}

    bases = {n: f'http://127.0.0.1:{port}/{n}/api/{v}' for n, (port, v) in ARR.items()}
    for name, (folder, field, category) in MEDIA.items():
        if name not in keys:
            continue
        base, key = bases[name], keys[name]
        ready(f'{base}/system/status', key)
        if folder not in [r['path'].rstrip('/') for r in api(f'{base}/rootfolder', key)]:
            api(f'{base}/rootfolder', key, {'path': folder})
            print(f'- {name}: media folder {folder} added')
        download = add_missing(base, key, 'downloadclient', 'QBittorrent',
                    {'host': 'gluetun', 'port': 8080, 'username': '', 'password': '', 'useSsl': False,
                     'urlBase': '', 'tvDirectory' if name == 'sonarr' else 'movieDirectory': '', field: category}, 'qBittorrent')
        selected_category = next((f['value'] for f in download['fields'] if f['name'] == field), category)
        categories[selected_category] = '/data/torrents/' + ('tv' if name == 'sonarr' else 'movies')
        arr_import_settings(base, key)
    configure_downloads(client, categories)

    if 'prowlarr' in keys:
        base, key = bases['prowlarr'], keys['prowlarr']
        ready(f'{base}/system/status', key)
        for name in ('sonarr', 'radarr'):
            if name in keys:
                add_missing(base, key, 'applications', name.capitalize(),
                            {'prowlarrUrl': 'http://prowlarr:9696/prowlarr', 'baseUrl': f'http://{name}:{ARR[name][0]}/{name}',
                             'apiKey': keys[name]}, name.capitalize())
        host = api(f'{base}/config/host', key)
        proxy = {'proxyEnabled': True, 'proxyType': 'http', 'proxyHostname': 'gluetun', 'proxyPort': 8888,
                 'proxyBypassLocalAddresses': True}
        if any(host.get(k) != v for k, v in proxy.items()):
            api(f'{base}/config/host/{host["id"]}', key, {**host, **proxy}, 'PUT')
            print('- prowlarr: searches now go through the VPN')

    bazarr = P / 'bazarr/config/config.yaml'
    if keys.keys() >= {'sonarr', 'radarr'} and wait_for(bazarr, 'Bazarr'):
        while_stopped('bazarr', bazarr, yaml_keys({
            'general': {'base_url': '/bazarr', 'use_sonarr': 'true', 'use_radarr': 'true'},
            'sonarr': {'ip': 'sonarr', 'port': '8989', 'base_url': '/sonarr', 'apikey': keys['sonarr']},
            'radarr': {'ip': 'radarr', 'port': '7878', 'base_url': '/radarr', 'apikey': keys['radarr']}}))

    jellyfin(keys)
    print(f'\nDownloads stay on this server: {storage["root"]}/torrents')
    print(f'Jellyfin libraries: {storage["movies"]} and {storage["series"]}')
    print('\nApps connected. Still to do yourself:')
    print('- Prowlarr (media.<domain>/prowlarr): add the indexers you are allowed to use; they appear in Sonarr/Radarr.')
    print('- Jellyseerr (requests.<domain>): sign in with Jellyfin (URL http://jellyfin:8096), then add')
    for name in MEDIA:
        if name in keys:
            print(f'    {name.capitalize()}: host {name}, port {ARR[name][0]}, URL base /{name}, API key {keys[name]}')


def jellyfin(keys):
    try:
        saved = json.loads((P / 'portal/jellyfin.json').read_text())
    except (OSError, ValueError):
        print('- Jellyfin: connect it on the admin page, then run ./start.sh again to add its libraries.')
        return
    base = f"http://{read_env(ROOT / '.env').get('JELLYFIN_BIND_IP') or '127.0.0.1'}:8096"
    token = f'MediaBrowser Token="{saved.get("key", "")}"'
    try:
        folders = api(f'{base}/Library/VirtualFolders', token, header='Authorization')
        have = {path.rstrip('/') for f in folders for path in f.get('Locations', [])}
        for name, kind, path in [('Movies', 'movies', '/data/media/movies'), ('Shows', 'tvshows', '/data/media/tv')]:
            if path not in have:
                query = urlencode({'name': name, 'collectionType': kind, 'paths': path, 'refreshLibrary': 'true'})
                api(f'{base}/Library/VirtualFolders?{query}', token,
                    {'LibraryOptions': {'EnableRealtimeMonitor': True}}, header='Authorization')
                print(f'- jellyfin: library {name} ({path}) added')
    except (OSError, ValueError, KeyError, AppError) as error:
        print(f'! Jellyfin libraries not checked ({error}). Add Movies (/data/media/movies) and Shows (/data/media/tv) in Jellyfin.')


def downloader_subnets():
    """Trust only loopback and the actual Docker network, not every private network."""
    import ipaddress
    cid = compose('ps', '-q', 'gluetun').stdout.strip()
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .NetworkSettings.Networks}}', cid],
                            capture_output=True, text=True, check=True)
    networks = json.loads(result.stdout)
    allowed = ['127.0.0.1/32', '::1/128']
    for network in networks.values():
        for address_key, prefix_key in [('IPAddress', 'IPPrefixLen'), ('GlobalIPv6Address', 'GlobalIPv6PrefixLen')]:
            if network.get(address_key):
                allowed.append(str(ipaddress.ip_network(f'{network[address_key]}/{network[prefix_key]}', strict=False)))
    return ', '.join(allowed)


if __name__ == '__main__':
    try:
        main()
    except (AppError, DownloadError, ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'App setup stopped: {error}. Fix the reported issue and rerun ./start.sh.') from None

