# HTTPS and use away from home

## Recommended first deployment

Run the hub on an always-on computer on the same trusted network as the printers
and media server. Expose **only the hub's HTTP service through HTTPS**. Keep the
application bound to `127.0.0.1`; do not publish its raw port, printer MQTT/camera,
Plex, Seerr or Kindle SSH ports to the internet.

Set your private `config.json`:

```json
"public_url": "https://dashboard.example.net/kindle-dashboard"
```

Replace the example domain with a DNS name you own. Restart the hub after a
configuration change. It checks this hostname for incoming requests and native
client builds pin the chosen hostname, URL prefix and device serial. A different
Wi-Fi does not require rebuilding when the public URL stays the same.

The native client requires an ordinary trusted certificate covering that DNS
name and standard HTTPS port 443. It does not accept a bare IP address or ignore
certificate failures. The Kindle's clock must also be correct.

## Option A: Caddy and a reachable public address

If you have an address reachable from the internet, point your domain's DNS at
that address and configure the router/firewall for the HTTPS proxy. A minimal
Caddyfile is:

```caddyfile
dashboard.example.net {
    reverse_proxy 127.0.0.1:8787
}
```

Caddy manages certificate issuance and renewal when its domain/reachability
requirements are met. Follow its [HTTPS quick start](https://caddyserver.com/docs/quick-starts/https)
and [reverse proxy guide](https://caddyserver.com/docs/quick-starts/reverse-proxy).
Run Caddy and the hub under their normal service managers; opening terminals
manually is useful for testing but does not survive a reboot.

Install Caddy on the same host as Inkboard for the loopback address above to
reach it. Use Caddy's installation and firewall requirements for your platform;
automatic certificate issuance can also require port 80 or an explicitly
configured DNS challenge. The Caddyfile is not a reason to expose port 8787.

Do not strip `/kindle-dashboard` in this example. The hub accepts that prefix
itself. Keep the Authorization header intact and do not cache authenticated
responses at the proxy.

## Option B: an outbound Cloudflare Tunnel

This avoids an incoming router port. You need your own Cloudflare-managed domain
and an installed `cloudflared` connector on the hub computer. Follow the current
[locally managed tunnel guide](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/).
The basic sequence is:

```sh
cloudflared tunnel login
cloudflared tunnel create inkboard
cloudflared tunnel route dns inkboard dashboard.example.net
```

Put the returned UUID and the absolute path of **your own generated credential
file** in your private cloudflared configuration:

```yaml
tunnel: YOUR_TUNNEL_UUID
credentials-file: /YOUR_HOME/.cloudflared/YOUR_TUNNEL_UUID.json
ingress:
  - hostname: dashboard.example.net
    service: http://127.0.0.1:8787
    originRequest:
      httpHostHeader: dashboard.example.net
  - service: http_status:404
```

On Windows use the actual Windows path in that field. The account certificate,
tunnel JSON and config stay outside this repository. Then:

```sh
cloudflared tunnel ingress validate
cloudflared tunnel run inkboard
```

Use Cloudflare's OS-specific service installation instructions after verifying
the foreground connection. See its [configuration reference](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/).
The loopback HTTP hop stays on the same computer; the external client uses HTTPS.

The dashboard's own bearer authentication protects data/actions. Do not add an
interactive browser-login challenge in front of the Kindle API: the native
client cannot complete that challenge. A compatible additional access layer
would need an explicit client integration. Never disable the hub's tokens.

## Verify before building the Kindle package

1. Open `https://YOUR_DOMAIN/kindle-dashboard/` in a browser on another network.
   It should show the sign-in shell without a certificate warning.
2. Sign in with your own reader token. Confirm real source timestamps advance.
3. The same API without a bearer token must reject data access.
4. Connect the Kindle to a hotspot or a different Wi-Fi, open Inkboard and
   inspect Health. A valid refresh establishes delivery; source warnings still
   describe the independent printer/media connections.

Do not put a token in a test URL. Prefer the browser sign-in or a local script
that reads it from the private setup directory into an HTTP header.

For step 3, request
`https://YOUR_DOMAIN/kindle-dashboard/api/v1/modules` in a private browser window
without signing in: it must return HTTP **401**, rather than module data. The
public sign-in shell and `/healthz` are intentionally reachable without a token.
If the shell returns a Host-header error, confirm `public_url`, restart your hub,
and ensure the reverse proxy preserves the configured public hostname.

## Separate collector and hub computers

The portable collector can send snapshots outbound to a remote HTTPS hub:

```sh
inkboard collect --directory PRIVATE_COLLECTOR --hub https://dashboard.example.net/kindle-dashboard
```

Configure the same module IDs on the hub with `"remote": true`. Give the
collector only the matching ingestion credential; do not put reader/controller
tokens in source control. The collector's private configuration holds its local
source credentials. Failed delivery uses bounded backoff and retains old hub
observations with honest freshness.

Provision this explicitly:

1. Run `inkboard init --directory PRIVATE_COLLECTOR` on the source computer.
2. Add its local integration objects and secrets using their guides. On the
   hub, configure matching IDs/types/names with `"remote": true`; the hub does
   not need the source's printer/Plex credentials.
   [`config.remote-hub.json`](../examples/config.remote-hub.json) shows the hub
   shape; keep the hub's generated `secrets.json`, replace its module list and
   public URL deliberately, and restart that owned hub. Do not mark the source
   computer's local integration objects `remote`.
3. On the hub's trusted terminal, run
   `inkboard show-token --directory PRIVATE_HUB --role ingest`. Transfer that
   value privately to the collector's hidden prompt:
   `inkboard set-secret --directory PRIVATE_COLLECTOR --name ingest_token`.
   Type `REPLACE` when asked. Do not change the hub's token or copy its reader
   and controller tokens. The collector's unused local role tokens can remain
   independently generated.
4. Run the collect command above once with `--once`. Check that it succeeds and
   the hub shows a new source timestamp, then use the continuous command and
   the collector startup template. A failed one-shot exits nonzero.

Use the installed `.venv` executables from the quickstart on **both** machines.
Install the Bambu extra on the machine that contacts printers; a telemetry-only
remote hub does not need printer dependencies. Stop the foreground collector
with Ctrl+C before registering its startup service to avoid duplicate polling.

**This release's split collector is telemetry-only.** It cannot grant remote
command capabilities. For working controls, keep the hub's adapter on a machine
that can reach the source directly—for example the recommended local hub, or a
hub joined to an explicitly configured private VPN/subnet route. A remote
snapshot must never make a cloud server claim that it has a command channel.
