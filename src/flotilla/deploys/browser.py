"""Shared browser: headful Chrome on a Selkies web desktop, plus a CDP proxy.

Runs ``lscr.io/linuxserver/chromium`` (a LinuxServer.io image: Chromium, a labwc
Wayland desktop, the Selkies web viewer and a watchdog) with docker compose in
``~/shared-chrome``, on hosts whose settings have ``browser.enabled: true``.

- Live view: https on <NetBird IP>:3001 (self-signed; prod's Caddy serves
  <name>.chrome.thevm.space in front of it). No login.
- CDP: http on <NetBird IP>:9223 through a Caddy sidecar. No token.
  NetBird is the only access control for both.
- The profile (cookies, logins) is in ``~/shared-chrome/config`` and is never
  touched here: each host's browser keeps its own logins and its own IP.

``docker compose up -d`` only runs when the rendered files changed or the
browser is not running. A change recreates the container, which restarts
Chrome: open tabs and parked login checkpoints are lost.
"""

import hashlib
from io import StringIO

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, fail, sh

b = bundle()
d = b["data"]
br = d.get("browser") or {}

COMPOSE = """\
# Managed by flotilla (browser component). Edits here are overwritten.
# Chrome + Selkies desktop (linuxserver/chromium) and a CDP proxy, on this host's
# NetBird address only, with no login or token. Profile: ./config/shared-profile.
services:
  chrome:
    image: {image}
    container_name: shared-chrome
    # Chromium's profile lock records hostname-pid; a fixed hostname keeps a stale
    # lock recognisable after the container is recreated.
    hostname: shared-chrome
    restart: unless-stopped
    security_opt:
      - seccomp:unconfined
    environment:
      PUID: "{uid}"
      PGID: "{gid}"
      TZ: {tz}
      # The image's watchdog relaunches Chromium whenever it exits.
      RESTART_APP: "true"
      CHROME_CLI: >-
        --user-data-dir=/config/shared-profile
        --remote-debugging-port=9222
        --remote-allow-origins={allow_origin}
        --lang=en-US
        --no-first-run
        --hide-crash-restore-bubble
        --disable-background-timer-throttling
        --disable-renderer-backgrounding
        --disable-backgrounding-occluded-windows
      SSL_CERT_FILE: /etc/ssl/certs/ca-certificates.crt
    volumes:
      - ./config:/config
      - /etc/ssl/certs:/etc/ssl/certs:ro
    ports:
      - "{address}:3001:3001"   # Selkies live view (https, self-signed)
      - "{address}:9223:9223"   # CDP (served by cdp-proxy)
    shm_size: "1gb"
    mem_limit: {mem_limit}

  cdp-proxy:
    image: {caddy_image}
    container_name: shared-chrome-cdp-proxy
    restart: unless-stopped
    network_mode: "service:chrome"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
    depends_on:
      chrome:
        condition: service_started
        restart: true
"""

CADDYFILE = """\
# Managed by flotilla (browser component). Plain CDP proxy on :9223, no token.
# Chrome refuses CDP WebSockets carrying a browser Origin except the ones allowed
# by --remote-allow-origins, so arbitrary web pages cannot take this browser over.
{
\tadmin off
\tauto_https off
}

:9223 {
\treverse_proxy 127.0.0.1:9222
}
"""

if not br.get("enabled"):
    pass
elif b["os"] == "darwin":
    fail("browser on macOS", "the browser component is Linux only")
elif not fact("docker compose version"):
    fail("browser needs docker", "apply the docker component to this host first")
else:
    home = b["home"]
    root = f"{home}/shared-chrome"
    compose = COMPOSE.format(
        image=br["image"], caddy_image=br["caddy_image"], tz=br["timezone"],
        mem_limit=br["mem_limit"], allow_origin=br["allow_origin"],
        address=d["address"], uid=fact("id -u"), gid=fact("id -g"))
    want = {"docker-compose.yml": compose, "Caddyfile": CADDYFILE}

    changed = False
    files.directory(name="dir ~/shared-chrome", path=root)
    files.directory(name="dir ~/shared-chrome/config", path=f"{root}/config")
    for name, content in want.items():
        digest = hashlib.sha256(content.encode()).hexdigest()
        if fact(f"sha256sum {root}/{name} | cut -d' ' -f1") != digest:
            changed = True
            files.put(name=f"~/shared-chrome/{name}", src=StringIO(content),
                      dest=f"{root}/{name}")

    running = fact("docker inspect -f '{{.State.Running}}' shared-chrome") == "true"
    if changed or not running:
        sh("docker compose up -d (restarts Chrome if it was running)",
           f"cd {root} && docker compose up -d --remove-orphans")
