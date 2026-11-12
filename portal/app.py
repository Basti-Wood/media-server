"""Language selector for a single Jellyfin account, behind Caddy + Authelia.

No media files, API keys or arbitrary upstream URLs are exposed to the browser.
Jellyfin itself remains the player. Language preferences apply to the Jellyfin user
set on the admin page.
"""
import hmac
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

LANGUAGES = {'de': {'de', 'deu', 'ger'}, 'en': {'en', 'eng'}, 'ja': {'ja', 'jpn'}}
NAMES = {'de': 'German', 'en': 'English', 'ja': 'Japanese'}
PLACEHOLDER = 'not-configured'
SETTINGS = Path(os.environ.get('SETTINGS_DIR', '/settings'))
# Reachability only: any HTTP answer, even 401, means the app is running.
SERVICES = {'Jellyfin': 'http://jellyfin:8096/health', 'Series (Sonarr)': 'http://sonarr:8989/ping',
            'Movies (Radarr)': 'http://radarr:7878/ping', 'Sources (Prowlarr)': 'http://prowlarr:9696/ping',
            'Subtitles (Bazarr)': 'http://bazarr:6767/', 'Requests (Jellyseerr)': 'http://jellyseerr:5055/api/v1/status',
            'Downloads (qBittorrent)': 'http://gluetun:8080/', 'Login (Authelia)': 'http://authelia:9091/api/health'}
# Jellyfin matches a preference against both ISO 639-2 forms (ger and deu).
CODES = {'de': 'ger', 'en': 'eng', 'ja': 'jpn'}


class UpstreamError(Exception):
    pass


def ident(value):
    value = str(value)
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid item identifier.')
    return value


def language(stream):
    code = (stream.get('Language') or '').lower()
    for key, aliases in LANGUAGES.items():
        if code in aliases:
            return key
    # Known other languages must not be mistaken for untagged audio.
    return 'unknown' if code in {'', 'und'} else code


def extra(stream):
    """Commentary and audio description tracks are not a dub."""
    title = (stream.get('Title') or '').lower()
    return 'commentary' in title or 'descri' in title


def stream_match(source, kind, lang):
    candidates = [s for s in source.get('MediaStreams') or []
                  if s.get('Type') == kind and language(s) == lang
                  and not (kind == 'Subtitle' and s.get('IsForced'))
                  and not (kind == 'Audio' and extra(s))]
    candidates.sort(key=lambda s: not s.get('IsDefault'))
    return candidates[0] if candidates else None


def availability(item, audio, subtitle):
    """A version is playable only when it has the requested tracks."""
    if audio not in LANGUAGES or subtitle not in {*LANGUAGES, 'none'}:
        raise ValueError('Choose German, English or Japanese and a subtitle language.')
    if audio == 'ja' and subtitle == 'none':
        raise ValueError('Choose a subtitle language for Japanese audio.')
    versions = []
    unknown = False
    for source in item.get('MediaSources') or []:
        streams = source.get('MediaStreams') or []
        unknown |= not any(s.get('Type') == 'Audio' for s in streams)
        unknown |= any(s.get('Type') == 'Audio' and language(s) == 'unknown' for s in streams)
        if subtitle != 'none':
            unknown |= any(s.get('Type') == 'Subtitle' and language(s) == 'unknown' for s in streams)
        audio_ok = stream_match(source, 'Audio', audio) is not None
        subtitle_ok = subtitle == 'none' or stream_match(source, 'Subtitle', subtitle) is not None
        height = next((s.get('Height') for s in streams if s.get('Type') == 'Video' and s.get('Height')), None)
        versions.append({'id': source.get('Id'), 'resolution': f'{height}p' if height else 'unknown',
                         'available': audio_ok and subtitle_ok, 'audioAvailable': audio_ok,
                         'subtitlesAvailable': subtitle_ok})
    playable = [v for v in versions if v['available']]
    if playable:
        state, message = 'available', 'Available in your library.'
    elif unknown or not versions:
        state, message = 'unknown', 'Language availability is unknown: some tracks are untagged or not analyzed. Refresh the metadata in Jellyfin.'
    elif any(v['audioAvailable'] for v in versions):
        state, message = 'missing_subtitles', f'{NAMES[subtitle]} subtitles are not available with {NAMES[audio]} audio in your library.'
    else:
        state, message = 'missing_audio', f'{NAMES[audio]} audio is not available in your library.'
    return {'state': state, 'message': message, 'versions': versions}


def write_private(path, text):
    tmp = path.with_name(path.name + '.tmp')
    tmp.unlink(missing_ok=True)
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def up(url):
    try:
        with urlopen(url, timeout=3):
            return True
    except HTTPError:
        return True
    except (OSError, ValueError):
        return False


def status():
    with ThreadPoolExecutor(len(SERVICES)) as pool:
        return dict(zip(SERVICES, pool.map(up, SERVICES.values())))


class Jellyfin:
    def __init__(self, key=None, username=None):
        self.base = os.environ.get('JELLYFIN_URL', 'http://jellyfin:8096').rstrip('/')
        if key is None:
            try:
                saved = json.loads((SETTINGS / 'jellyfin.json').read_text())
            except (OSError, ValueError):
                saved = {}
            key, username = saved.get('key', ''), saved.get('user', '')
        self.key, self.username, self.uid = key, username, None

    def save(self):
        """Check the key and user against Jellyfin, then keep them for the language page."""
        if not re.fullmatch(r'[0-9a-f]{32}', self.key) or not 0 < len(self.username) <= 100:
            raise ValueError('Enter a Jellyfin API key (32 characters) and your Jellyfin username.')
        self.user()
        try:
            write_private(SETTINGS / 'jellyfin.json', json.dumps({'key': self.key, 'user': self.username}))
        except OSError:
            raise UpstreamError('Could not save. Run ./start.sh so provision/portal is created for the portal.') from None
        return {'message': f'Connected to Jellyfin as {self.username}.'}

    def request(self, path, params=None, body=None):
        if not self.key or not self.username:
            raise UpstreamError('Jellyfin is not connected yet. Open the admin page and enter its API key.')
        url = self.base + path + ('?' + urlencode(params) if params else '')
        req = Request(url, data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'POST',
                      headers={'Authorization': f'MediaBrowser Token="{self.key}"', 'Accept': 'application/json', 'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=20) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise UpstreamError('Jellyfin response is too large. Narrow your search.')
            return json.loads(data) if data.strip() else {}
        except (HTTPError, URLError, TimeoutError, ValueError):
            raise UpstreamError('Jellyfin could not be reached or rejected the API key. Check the Jellyfin connection on the admin page.') from None

    def user(self):
        if self.uid is None:
            match = [u for u in self.request('/Users') if u.get('Name', '').lower() == self.username.lower()]
            if not match:
                raise UpstreamError(f'Jellyfin has no user named {self.username}.')
            self.uid = ident(match[0]['Id'])
        return self.uid

    def metadata(self, item):
        data = self.request('/Items/' + ident(item), {'userId': self.user()})
        if data.get('Type') not in {'Movie', 'Episode'}:
            raise ValueError('This item has no playable movie or episode.')
        return data

    def libraries(self):
        return [{'id': d.get('Id'), 'title': d.get('Name'), 'type': d.get('CollectionType')}
                for d in self.request('/UserViews', {'userId': self.user()}).get('Items', [])
                if d.get('CollectionType') in {'movies', 'tvshows'}]

    def items(self, section, start, search):
        library = next((s for s in self.libraries() if s['id'] == section), None)
        if not library:
            raise ValueError('Library not found.')
        params = {'userId': self.user(), 'parentId': ident(section), 'recursive': 'true', 'sortBy': 'SortName',
                  'includeItemTypes': 'Movie' if library['type'] == 'movies' else 'Episode', 'startIndex': start, 'limit': 50}
        if search:
            params['searchTerm'] = search[:200]
        data = self.request('/Items', params)
        items = []
        for v in data.get('Items', []):
            title = v.get('Name', 'Untitled')
            if v.get('Type') == 'Episode':
                title = f"{v.get('SeriesName', '')} · S{v.get('ParentIndexNumber', '?')} E{v.get('IndexNumber', '?')} · {title}"
            items.append({'id': v.get('Id'), 'title': title, 'year': v.get('ProductionYear', '')})
        return {'items': items, 'total': int(data.get('TotalRecordCount', len(items))), 'start': start}

    def select(self, item, audio, subtitle, media_id):
        result = availability(self.metadata(item), audio, subtitle)
        version = next((v for v in result['versions'] if v['id'] == media_id and v['available']), None)
        if not version:
            raise ValueError('The requested audio/subtitles are not available in this version. Nothing was changed.')
        uid = self.user()
        config = self.request('/Users/' + uid).get('Configuration') or {}
        wanted = {'AudioLanguagePreference': CODES[audio], 'PlayDefaultAudioTrack': False,
                  'SubtitleLanguagePreference': CODES.get(subtitle, ''), 'SubtitleMode': 'None' if subtitle == 'none' else 'Always'}
        self.request('/Users/Configuration', {'userId': uid}, {**config, **wanted})
        saved = self.request('/Users/' + uid).get('Configuration') or {}
        if any((saved.get(k) or '') != (v or '') for k, v in wanted.items()):
            raise UpstreamError('Jellyfin did not confirm the language preference. Check it in Jellyfin → Settings → Playback.')
        return {'message': f"Saved as your Jellyfin preference: {NAMES[audio]} audio, {'no subtitles' if subtitle == 'none' else NAMES[subtitle] + ' subtitles'}. Jellyfin uses it for every title that has these tracks. If a title has several versions, choose {version['resolution']} in the player.",
                'jellyfinUrl': f"https://jellyfin.{os.environ.get('MEDIA_DOMAIN', '')}/web/#/details?id={ident(item)}"}


class VPN:
    """Surfshark credentials saved for Gluetun, applied live through its control server."""
    def __init__(self):
        self.folder = Path(os.environ.get('VPN_DIR', '/vpn'))
        self.base = os.environ.get('GLUETUN_URL', 'http://gluetun:8000')

    def request(self, path, body=None):
        key = (self.folder / 'control.key').read_text().strip()
        req = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'PUT',
                      headers={'X-API-Key': key, 'Content-Type': 'application/json'})
        with urlopen(req, timeout=10) as response:
            return response.read(65536)

    def status(self):
        try:
            configured = (self.folder / 'openvpn_user').read_text().strip() not in {'', PLACEHOLDER}
        except OSError:
            configured = False
        try:
            state = json.loads(self.request('/v1/vpn/status')).get('status', 'unknown')
            ip = json.loads(self.request('/v1/publicip/ip')) if state == 'running' else {}
        except (OSError, ValueError):
            state, ip = 'unreachable', {}
        try:
            countries = json.loads(self.request('/v1/vpn/settings'))['provider']['server_selection']['countries'] or []
        except (OSError, ValueError, KeyError, TypeError):
            countries = []
        return {'configured': configured, 'status': state, 'ip': ip.get('public_ip', ''), 'country': ip.get('country', ''),
                'countries': countries}

    def set_country(self, country):
        country = country.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,59}", country):
            raise ValueError('Enter a country name, e.g. Netherlands.')
        try:
            self.request('/v1/vpn/settings', {'provider': {'server_selection': {'countries': [country]}}})
        except HTTPError:
            raise ValueError(f'Surfshark has no servers in "{country}". Check the spelling, e.g. Netherlands.') from None
        except OSError:
            raise UpstreamError('The VPN is not reachable right now. Try again in a minute.') from None
        try:
            # Gluetun reads this file before .env, so the choice survives restarts.
            write_private(self.folder / 'server_countries', country)
        except OSError:
            raise UpstreamError('Country applied, but it could not be saved for restarts.') from None
        return {'message': f'VPN now connects through {country}.'}

    def save(self, username, password):
        username, password = username.strip(), password.strip()
        if not all(0 < len(v) <= 200 and v.isprintable() for v in (username, password)) or username == PLACEHOLDER:
            raise ValueError('Enter the Surfshark manual-setup username and password.')
        try:
            for name, value in (('openvpn_user', username), ('openvpn_password', password)):
                write_private(self.folder / name, value)
        except OSError:
            raise UpstreamError('Could not save the credentials. Run ./start.sh so provision/vpn is created for the portal.') from None
        try:
            self.request('/v1/vpn/settings', {'openvpn': {'user': username, 'password': password}})
        except HTTPError:
            raise UpstreamError('Saved, but the VPN rejected them. Check the Surfshark manual-setup credentials.') from None
        except URLError:
            return {'message': 'Saved. The VPN starts with these credentials within a minute.'}
        except OSError:
            raise UpstreamError('Saved. Run ./start.sh, then: docker compose restart gluetun') from None
        return {'message': 'Saved. The VPN is reconnecting with these credentials.'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'MediaPortal'

    def log_message(self, fmt, *args):
        # Do not log URLs, cookies or request bodies.
        print('portal request', self.command, flush=True)

    def send(self, code, value, content_type='application/json'):
        data = json.dumps(value).encode() if content_type == 'application/json' else value
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, write):
        self.connection.settimeout(30)
        parsed = urlsplit(self.path)
        if parsed.path == '/healthz' and not write:
            return self.send(200, {'status': 'ok'})
        if not self.headers.get('Remote-User'):
            return self.send(401, {'error': 'Sign in through the secure media address.'})
        if write:
            origin = os.environ.get('PORTAL_ORIGIN', '')
            if not origin or not hmac.compare_digest(self.headers.get('Origin', ''), origin) or self.headers.get('X-Media-Request') != '1':
                return self.send(403, {'error': 'Request origin was rejected.'})
        admin = parsed.path == '/admin' or parsed.path.startswith(('/api/status', '/api/vpn', '/api/jellyfin'))
        if admin and 'admins' not in [g.strip() for g in self.headers.get('Remote-Groups', '').split(',')]:
            return self.send(403, {'error': 'Only admins can open the admin page.'})
        q = parse_qs(parsed.query)
        get = lambda key, default='': q.get(key, [default])[0]
        try:
            jellyfin = Jellyfin()
            if not write and parsed.path in {'/', '/admin'}:
                page = 'admin.html' if parsed.path == '/admin' else 'index.html'
                return self.send(200, Path(__file__).with_name(page).read_bytes(), 'text/html; charset=utf-8')
            if not write and parsed.path == '/api/config':
                domain = os.environ.get('MEDIA_DOMAIN', '')
                return self.send(200, {'apps': {key: f'https://{sub}.{domain}' for key, sub in
                    [('Watch', 'jellyfin'), ('Series', 'sonarr'), ('Movies', 'radarr'), ('Sources', 'prowlarr'), ('Subtitles', 'bazarr'), ('Requests', 'requests'), ('Downloads', 'downloads'), ('Account / sign out', 'auth')]}})
            if not write and parsed.path == '/api/libraries':
                return self.send(200, jellyfin.libraries())
            if not write and parsed.path == '/api/items':
                start = int(get('start', '0'))
                if start < 0 or start > 1000000:
                    raise ValueError('Invalid page.')
                return self.send(200, jellyfin.items(ident(get('section')), start, get('search')))
            if not write and parsed.path == '/api/availability':
                return self.send(200, availability(jellyfin.metadata(get('item')), get('audio', 'de'), get('subtitle', 'none')))
            if not write and parsed.path == '/api/status':
                return self.send(200, {'services': status(), 'vpn': VPN().status(),
                                       'jellyfin': {'configured': bool(jellyfin.key), 'user': jellyfin.username}})
            if write:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 4096 or self.headers.get('Content-Type') != 'application/json':
                    raise ValueError('Invalid request body.')
                data = json.loads(self.rfile.read(size))
                if parsed.path == '/api/select':
                    return self.send(200, jellyfin.select(ident(data['item']), data['audio'], data['subtitle'], ident(data['media'])))
                forms = {'/api/vpn': lambda: VPN().save(str(data['username']), str(data['password'])),
                         '/api/vpn/country': lambda: VPN().set_country(str(data['country'])),
                         '/api/jellyfin': lambda: Jellyfin(str(data['key']).strip(), str(data['user']).strip()).save()}
                if parsed.path in forms:
                    try:
                        return self.send(200, forms[parsed.path]())
                    except ValueError as error:
                        return self.send(400, {'error': str(error)})
            return self.send(404, {'error': 'Not found.'})
        except (ValueError, KeyError, TypeError, UnicodeError):
            return self.send(400, {'error': 'Invalid selection. Refresh the page and choose a listed item, language and version.'})
        except UpstreamError as error:
            return self.send(502, {'error': str(error)})


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
