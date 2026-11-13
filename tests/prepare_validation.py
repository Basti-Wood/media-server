"""Generate public dummy credentials for CI config validation, never for use."""
from pathlib import Path
import os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_setup import setup, DIGEST
from storage import prepare

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    if (ROOT / '.env').exists() or (ROOT / 'provision/authelia/configuration.yml').exists():
        raise SystemExit('Refusing to replace local configuration. Use a clean CI checkout.')
    (ROOT / '.env').write_text('MEDIA_DOMAIN=media.home.arpa\nJELLYFIN_DOMAIN=jf.home.arpa\nREQUESTS_DOMAIN=requests.home.arpa\n')
    setup.vpn_files(ROOT)
    setup.generate(ROOT, 'media.home.arpa', 'ci-only', DIGEST, 'ci@example.com')
    prepare(ROOT, {'PUID': str(os.geteuid()), 'PGID': str(os.getegid())})

