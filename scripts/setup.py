#!/usr/bin/env python3
"""Create local authentication files without committing credentials."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import string

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('Invalid .env line. Use KEY=value, without shell commands.')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def validate_domain(domain):
    if not re.fullmatch(r'(?=.{1,200}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', domain) or len(domain.split('.')) < 3 or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in domain.split('.')):
        raise ValueError('Use a dedicated subdomain, e.g. media.home.arpa or media.example.com.')
    return domain


def generate(root, domain, username, digest, email):
    validate_domain(domain)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', username):
        raise ValueError('Username must contain only letters, digits, underscores or hyphens.')
    if not re.fullmatch(r'\$argon2id\$v=19\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+', digest):
        raise ValueError('Paste only the complete $argon2id$ digest from Authelia.')
    if '@' not in email or '\n' in email:
        raise ValueError('Enter a valid email address.')
    folder = root / 'provision/authelia'
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'configuration.yml').exists() or (folder / 'users_database.yml').exists():
        raise ValueError('Authentication is already configured. Edit the existing files; setup will not replace users or keys.')
    folder.chmod(0o700)
    def private(name, data):
        path = folder / name
        with path.open('x') as f:
            f.write(data)
        path.chmod(0o600)
    config = (root / 'deploy/authelia.template.yml').read_text().replace('${MEDIA_DOMAIN}', domain)
    private('configuration.yml', config)
    private('users_database.yml', json.dumps({'users': {username: {'displayname': username,
        'password': digest, 'email': email, 'groups': ['admins']}}}, indent=2) + '\n')
    for filename in ['session.secret', 'storage.secret', 'jwt.secret']:
        if not (folder / filename).exists():
            private(filename, ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(64)))
    for name in ['movies', 'tv', 'music', 'kids', 'movies favorite']:
        (root / 'content/media' / name).mkdir(parents=True, exist_ok=True)
    for name in ['movies', 'tv', 'music']:
        (root / 'content/torrents' / name).mkdir(parents=True, exist_ok=True)


# Gluetun runs (firewall up, downloads blocked) until real Surfshark credentials replace this.
PLACEHOLDER = 'not-configured'


def vpn_files(root, owner=None):
    """Folders the portal writes to: Surfshark credentials (read by Gluetun) and portal settings."""
    folder = root / 'provision/vpn'
    settings = root / 'provision/portal'
    for path in (folder, settings):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    key_file = folder / 'control.key'
    if not key_file.exists():
        with key_file.open('x') as f:
            f.write(secrets.token_urlsafe(32))
        key_file.chmod(0o600)
    for name in ('openvpn_user', 'openvpn_password'):
        if not (folder / name).exists():
            (folder / name).write_text(PLACEHOLDER)
            (folder / name).chmod(0o600)
    auth = root / 'provision/gluetun/auth'
    auth.mkdir(parents=True, exist_ok=True)
    (auth / 'config.toml').write_text('[[roles]]\nname = "portal"\nauth = "apikey"\napikey = "' + key_file.read_text() +
        '"\nroutes = ["GET /v1/vpn/status", "GET /v1/vpn/settings", "PUT /v1/vpn/settings", "GET /v1/publicip/ip"]\n')
    (auth / 'config.toml').chmod(0o600)
    if owner:  # run with sudo: the portal (PUID/PGID) must still be able to write here
        for path in (folder, settings, *folder.iterdir()):
            os.chown(path, *owner)


def check_owner(root):
    """Docker creates missing mounted folders as root when the stack starts before setup."""
    if os.geteuid() == 0:
        return
    wrong = [name for name in ('provision/authelia', 'provision/vpn', 'provision/portal', 'provision/gluetun')
             if (root / name).exists() and (root / name).stat().st_uid != os.geteuid()]
    if wrong:
        raise ValueError('Docker created these folders as root before setup ran. Fix them, then run ./start.sh again:\n'
                         '  sudo chown -R "$USER": ' + ' '.join(wrong))


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.umask(0o077)
    envfile = ROOT / '.env'
    if not envfile.exists():
        raise SystemExit('First copy .env.example to .env and set MEDIA_DOMAIN.')
    env = read_env(envfile)
    domain = validate_domain(env.get('MEDIA_DOMAIN', ''))
    check_owner(ROOT)
    vpn_files(ROOT, (int(env.get('PUID', 1000)), int(env.get('PGID', 1000))) if os.geteuid() == 0 else None)
    folder = ROOT / 'provision/authelia'
    try:
        if 'media_session' in (folder / 'configuration.yml').read_text():
            print('Authentication is already configured.')
            return
    except OSError:
        pass
    # Authelia writes its own example files when it starts before setup; keep them aside.
    for name in ('configuration.yml', 'users_database.yml'):
        if (folder / name).exists():
            (folder / name).rename(folder / (name + '.authelia-example'))
    print('Generate a password hash in another terminal (your password stays local):')
    print('docker run --rm -it authelia/authelia:4.39.28 authelia crypto hash generate argon2')
    username = input('Login username: ').strip()
    email = input('Account email: ').strip()
    digest = getpass.getpass('Paste the Digest value ($argon2id$…, without "Digest:"): ').strip()
    generate(ROOT, domain, username, digest, email)
    envfile.chmod(0o600)
    print('Authentication created.')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
