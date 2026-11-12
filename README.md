# Home media server with language choices

Jellyfin playback (free and open source), Sonarr/Radarr media management, Prowlarr sources, Bazarr subtitles,
and qBittorrent routed through Surfshark. A new language page shows whether your
files contain German, English, or Japanese audio and your chosen subtitles.
Caddy and Authelia protect the browser apps with HTTPS and a 30-day Remember Me
session. Redis persists sessions through container restarts.

## What language selection does

1. Open the media homepage and choose **German**, **English**, or **Japanese**.
2. Choose subtitles: English, German, Japanese, or Off. Japanese requires subtitles.
3. Pick a library and search for a movie or episode. Choose **Check languages**.
4. If a file version contains the requested tracks, choose **Use these languages**,
   then **Open in Jellyfin**. Select the matching version if Jellyfin asks.

The page checks actual Jellyfin stream metadata for every file version.
It never combines audio from one version with subtitles from another. Full
subtitles are required; a forced-only track does not satisfy the subtitle choice.
Commentary and audio-description tracks do not count as a dub.

- **Available in your library:** the requested audio and subtitles were found.
- **German/English/Japanese audio is not available in your library:** no matching
  tagged audio track was found. No fallback is selected.
- **Subtitles are not available with this audio:** the audio exists, but the
  requested full subtitles are missing from that version.
- **Availability unknown:** audio or requested subtitles are untagged/unanalysed.
  If Jellyfin cannot be reached, a connection error is shown separately and is never
  presented as a missing dub.

This cannot prove that an edition does not exist anywhere. It checks your own
library, not every streaming service or release source. It does not generate dubs,
translate audio, download missing tracks, or splice separately acquired audio.
Bazarr can fetch subtitles once you configure its providers.

**Jellyfin is the player.** The portal is for one Jellyfin account (set on the admin page).
**Use these languages** saves your preferred audio and subtitle language on that
account, so Jellyfin picks those tracks in every title that has them. Use that same
account in Jellyfin. Your browser remembers your language choice. Tracks you pick
by hand in the Jellyfin player can override the preference for that title.

## First installation

Requires Linux Docker Engine, Docker Compose **v2.20+** (the `docker compose` plugin
or the standalone `docker-compose`; the scripts use whichever is installed, and the
commands below work with either spelling), Python 3, a Surfshark subscription, and
your own media/sources. Do this on the machine hosting the server. No VPN
credentials or Jellyfin API keys belong in GitHub or in chat.

### 1. Configure the server address and VPN

```sh
cp .env.example .env
```

Edit `.env` locally:

- `MEDIA_DOMAIN`: the admin address, e.g. `media.example.com`. Its pages:
  `/` languages, `/admin`, `/sonarr`, `/radarr`, `/prowlarr`, `/bazarr`,
  `/downloads` (qBittorrent) and the login at `/auth`, all behind your login.
- `JELLYFIN_DOMAIN` (e.g. `jf.example.com`) and `REQUESTS_DOMAIN`
  (e.g. `requests.example.com`): Jellyfin and Jellyseerr, each with its own login,
  so TV and phone apps can connect. They are kept off `MEDIA_DOMAIN` so a bug in
  them cannot reach the admin apps.
- `CADDY_BIND_IP`: where your cloudflared reaches Caddy on port 80. Keep
  `127.0.0.1` if cloudflared runs on this server (outside Docker); use the server's
  LAN IP if it runs on another machine.
- `VPN_COUNTRIES`: default VPN exit country; change it later on the admin page.
- `JELLYFIN_BIND_IP`: set to the server's LAN IP for TV/phone apps
  (`http://LAN-IP:8096`). It defaults to `127.0.0.1`, so Jellyfin starts restricted
  to the server itself. Do not forward it from your router.
- `PUID` / `PGID`: the user/group owning your media folders. `TZ` defaults to
  Europe/Luxembourg.

### 2. Start everything and create the admin account

```sh
./start.sh
```

The first run asks for your admin login. It tells you how to generate an Argon2
password hash with Authelia; paste the digest. It creates private, ignored config
files and secrets and never overwrites an existing account. Then it builds and
starts every service. Run it again any time; it is safe to repeat.
Run it before the first `docker compose up`: otherwise Docker creates
`provision/vpn` and `provision/portal` as root and the portal cannot save settings
(setup then stops before asking anything and prints the `sudo chown` command that fixes it). Set `PUID`/`PGID` in
`.env` to your own user (`id -u`, `id -g`).

It also connects the apps to each other (`scripts/connect_apps.py`, safe to repeat):
Sonarr/Radarr get their media folders and qBittorrent, Prowlarr feeds both and
searches through the VPN, Bazarr is linked to both, qBittorrent saves to
`/data/torrents` over the VPN only, and Jellyfin gets its Movies and Shows
libraries once it is connected on the admin page. The admin apps use your one
login (Authelia); they have no second password.

qBittorrent starts too, but stays offline until you connect Surfshark: the VPN's
firewall blocks all its traffic. Compose prints a warning that the optional
dependency gluetun is not healthy until then; that is expected.

### 3. Point your Cloudflare Tunnel at Caddy

In Cloudflare Zero Trust → Networks → Tunnels → your tunnel → Public Hostnames,
add your three hostnames (`MEDIA_DOMAIN`, `JELLYFIN_DOMAIN`, `REQUESTS_DOMAIN`),
each with service **HTTP** and URL `SERVER:80` (the `CADDY_BIND_IP` address, e.g.
`localhost:80`). Cloudflare provides HTTPS; Caddy routes each name. Note that Cloudflare's terms restrict streaming lots of video
through its network; Jellyfin on your LAN (`JELLYFIN_BIND_IP`) avoids that.

Visit `https://MEDIA_DOMAIN`, sign in, and tick **Remember me**. The browser session
lasts up to 30 days, with persisted Redis storage. Without Remember Me, sessions
expire after 12 hours or 1 hour inactivity. Clearing cookies, logging out, or
revoking sessions requires a new login.

**Admin page (`https://MEDIA_DOMAIN/admin`, admins only):** buttons to every app,
which apps are up or down, the VPN state and public IP, and these settings:

- **Surfshark VPN:** the username and password from Surfshark → VPN → Manual setup
  → **Credentials** (not your account email). The VPN connects within a minute and
  qBittorrent goes online. Changes apply to the running VPN directly.
- **VPN country:** e.g. `Germany`; applied live and kept across restarts.
- **Jellyfin connection:** see step 4.

### 4. Connect Jellyfin, then add sources

1. Open `https://JELLYFIN_DOMAIN`, follow the setup wizard and create your Jellyfin
   user (no online account needed). Skip adding libraries.
2. In Jellyfin → Dashboard → API Keys, create a key. Enter it with your Jellyfin
   username on the admin page under **Jellyfin connection**, then run `./start.sh`
   again: it adds the Movies and Shows libraries.
3. In Prowlarr (`MEDIA_DOMAIN/prowlarr`), add the indexers you are allowed to use.
   Prowlarr copies them into Sonarr and Radarr.
4. In Jellyseerr (`https://REQUESTS_DOMAIN`), sign in with Jellyfin (URL
   `http://jellyfin:8096`) and add Radarr and Sonarr: host `radarr` / `sonarr`, port
   `7878` / `8989`, URL base `/radarr` / `/sonarr`, and the API keys `./start.sh`
   printed. Choose a quality profile and the folders `/data/media/movies` and
   `/data/media/tv`.

Then request a movie or series in Jellyseerr: Radarr/Sonarr search your indexers,
qBittorrent downloads it through the VPN to `/data/torrents`, and it is moved (as
a hardlink) to `/data/media`, where Jellyfin shows it within a few minutes.

- **Bazarr:** create profiles for full English/German subtitles (plus Japanese if
  wanted), assign them to movies/series, and configure your subtitle providers.
  Jellyfin must rescan to detect new tracks.

### 5. Add strict acquisition profiles

Run on the server (API keys are entered at a hidden prompt):

```sh
python3 scripts/language_profiles.py sonarr
python3 scripts/language_profiles.py radarr
```

Creates `Media audio · German`, `Media audio · English`, and
`Media audio · Japanese` profiles using each app's current language IDs and
custom-format schema. Profiles require the matching audio score and allow HD
720p/1080p WEB/Bluray; there is **no English fallback** in the German profile.
Select a profile per movie/series or in the request app. It leaves existing
profiles, assignments and files alone and does not trigger searches/downloads.
Reruns preserve existing profiles. Upgrades are disabled to avoid repeated
replacement downloads; enable deliberately if you want that behavior.

Release-language tags can be inaccurate; the portal verifies actual tracks after
import. A Japanese acquisition profile does not guarantee subtitles: configure
Bazarr and check the portal. Sonarr/Radarr generally manage one chosen release per
movie/episode; switching the acquisition profile can replace that file on a later
search. Prefer dual/multi-audio media when you want several audio languages
available immediately without replacing files.

## VPN verification and outages

```sh
bash scripts/vpn-check.sh
```

The public IP inside qBittorrent, and through the indexer proxy, must be a
Surfshark exit, not your home IP.
Gluetun's firewall stays enabled, qBittorrent has no independent network, and it
starts only after Gluetun is healthy. A healthy Compose startup alone is not a
proof against leaks: run this outage test on your actual host before downloads.

1. Pause downloads and run the check above.
2. Run `docker compose exec gluetun ip link set tun0 down` and immediately run
   `docker compose exec -T qbittorrent curl --fail --max-time 3 https://api.ipify.org`.
   It must fail while the tunnel is down or return a Surfshark IP if Gluetun has
   already reconnected; it must never return the home IP.
3. Restore with `docker compose restart gluetun qbittorrent`, then repeat the check.

Jellyfin stays outside the VPN. Surfshark does not provide incoming port forwarding;
peer connectivity/seeding may be limited. The VPN does not give remote access to
your home server. Prowlarr/Jackett searches go through Surfshark only when their
proxy is set as above; when the VPN is down those searches fail instead of leaking.

## Backups, health and updates

```sh
bash scripts/backup.sh
python3 scripts/health.py
```

Backups briefly stop the currently running services for consistent databases and
restart those services afterward. They include `.env`, app configuration,
databases, certificates and credentials, **not media files**. Archives are private
and checksummed; copy them to encrypted storage on another disk. No automatic
retention/deletion is enabled. For restore: stop the stack, verify the `.sha256`
file, extract the archive into a clean checkout, check ownership, then start.
This restores authentication/session state too; revoke sessions if restoring after
a security incident. Test restore on an isolated host.

Run `scripts/health.py` from your own cron/systemd schedule to check missing,
stopped/unhealthy containers and disk space. It logs problems and exits nonzero.
Optional `ALERT_WEBHOOK` must be exported in that scheduler's environment to send
JSON `{ "text": "..." }` alerts; it is not automatically loaded from `.env`.
No scheduled jobs or external notifications are created by installing this repo.

Sonarr, Radarr, qBittorrent, Gluetun, Caddy and Authelia are versioned in Compose.
Other inherited images still follow their release channels; review and pin them
to tested tags/digests before a production rollout. Back up before pulling newer
images. No automatic container updater is enabled. Jellyfin does not run privileged.
Logs rotate at 10 MiB × 3 files per container.

Extra inherited services are opt-in via `docker compose --profile extras up -d`:
Lidarr, Jackett, Ombi, Heimdall, Huntarr, Suggestarr and FlareSolverr. Their ports
are loopback-only and have no default public reverse-proxy routes. Prowlarr and
Jellyseerr are the default source/request apps. Older setup context is archived in
[docs/upstream-setup.md](docs/upstream-setup.md); its port/network assumptions are
historical.

## Validation

```sh
python3 -m pip install PyYAML
python3 -m unittest discover -s tests -v
bash -n scripts/backup.sh scripts/vpn-check.sh
```

CI also runs Docker Compose interpolation checks, Authelia/Caddy native config
validation, and a portal image build using public dummy credentials. Live Jellyfin
playback, client behavior, Surfshark credentials, kill-switch operation, TLS trust
and real release-language detection require a configured deployment and are not
claimed by the unit tests.
