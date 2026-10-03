# DEPLOYMENT.md — Deployment Notes

## Container Restart Policy and Panic

The Docker image starts `server.py` directly. Set
`OUROBOROS_LAUNCH_INTENT=automatic` and use `restart: on-failure` so an
automatic restart preserves Panic. The server uses the launcher's existing
Panic check before runtime startup: it leaves the stop marker intact and
exits 0. Panic itself still exits 99; Docker may restart it once, then the
automatic entry exits cleanly and `on-failure` leaves it stopped.

For an image built as `ouroboros-web`, a Compose service can use:

```yaml
services:
  ouroboros:
    image: ouroboros-web
    ports:
      - "127.0.0.1:8765:8765"
    environment:
      OUROBOROS_DATA_DIR: /data
      OUROBOROS_LAUNCH_INTENT: automatic
    volumes:
      - ouroboros-data:/data
    restart: on-failure
volumes:
  ouroboros-data:
```

An absent intent or `OUROBOROS_LAUNCH_INTENT=owner` keeps explicit owner startup
available. To resume the stopped example with the same data volume:

```bash
docker compose run --rm --service-ports -e OUROBOROS_LAUNCH_INTENT=owner ouroboros
```

This runs a foreground owner session; `--rm` disables its restart policy and
removes that container on exit, while the named data volume survives. An
ordinary `docker start` retains the configured automatic intent and therefore
does not resume Panic. Do not delete the stop marker to resume.

`always` and `unless-stopped` can keep restarting the process after its clean
exit; the marker stays intact, but these policies do not provide the supported
stopped-container behavior. Without automatic intent, restart policies can
still consume the marker and resume the server. See Docker's
[restart policy documentation](https://docs.docker.com/engine/containers/start-containers-automatically/).

## Trusted Docker / Kubernetes Non-Local Binds

By default, saving `OUROBOROS_SERVER_HOST=0.0.0.0` through the Settings UI
requires `OUROBOROS_NETWORK_PASSWORD` in the same save. This keeps desktop and
local-network launches from accidentally exposing the full Ouroboros HTTP and
WebSocket surface without the built-in password gate.

Trusted container deployments may opt out with:

```bash
OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1
```

Use this flag only when access is already restricted by external
infrastructure, for example:

- ingress authentication
- VPN-only routing
- private Kubernetes service/network policy
- an authenticated reverse proxy

With the flag enabled, Ouroboros still warns when saving a non-localhost bind
without `OUROBOROS_NETWORK_PASSWORD`, but the Settings UI no longer blocks
ordinary settings saves such as API-key updates. Do not use this flag on an
open LAN or public port.

## Extra CA Certificates

Ouroboros verifies its own provider calls (OpenRouter, OpenAI-compatible endpoints,
Anthropic, GigaChat, model catalogs and Provider Test) against the `certifi`
bundle, never the operating-system store. A deployment behind a TLS-inspecting
proxy, or one that talks to an endpoint signed by a CA `certifi` lacks (for
example the Russian Trusted Root CA behind GigaChat), sets
`OUROBOROS_EXTRA_CA_BUNDLE` to a PEM file holding the missing CA certificates.
The file is added on top of the defaults, so every other provider keeps working;
a path that cannot be read fails the call loudly instead of silently falling
back to the defaults. In Docker, mount the file and pass the setting:

```bash
docker run --rm -p 8765:8765 \
  -v "$PWD/extra-ca.pem:/certs/extra-ca.pem:ro" \
  -e OUROBOROS_EXTRA_CA_BUNDLE=/certs/extra-ca.pem \
  ouroboros-web
```

On a desktop install the same key lives in Settings → Advanced. It covers the
provider calls: model requests, model catalogs, Provider Test, pricing and
capability probes. Everything else keeps its own trust store: `git`, `uv`,
`pip`, the Claudexor engine and its runtime downloads, the Telegram skill, MCP
servers, the web-search scraper and the browsers.
