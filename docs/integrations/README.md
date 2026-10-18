# Choose your integrations

Start with the main [quickstart](../../README.md), which creates a private
configuration and secret store. Each object in `config.json`'s `modules` array
selects one integration. Add only what you use; a status-only dashboard does not
need Plex, a printer account, MQTT, Docker or AI credentials.

| Integration | What is included | Credentials | Guide |
| --- | --- | --- | --- |
| Bambu P1S | Local status, progress, temperature, layers, snapshots; optional authorized LAN controls | Your printer's local access code and a verified TLS certificate pin or CA | [Printers](bambu.md) |
| Plex | Now Playing, playback progress, transcoding, bandwidth and libraries; optional library refresh / stopping a stream | Your Plex token | [Media](plex-seerr.md) |
| Seerr (optional within Plex) | Requests, media availability, title search, selected-season requests, named destinations and approval | Your Seerr API key | [Media](plex-seerr.md) |
| HTTP status | Any configured HTTP service's availability and response latency | None | [Services](http-status.md) |

Copy the relevant object from [`examples/integrations`](../../examples/integrations)
into the private `modules` array. Replace every hostname and placeholder. Examples
are intentionally non-working until configured. Actual secret **values** belong
only in the private secrets file using the quickstart's hidden prompt, never in
module JSON or a Git commit. Keys ending `_secret` are references to entries in
that file.

## Edit the generated setup, not the repository examples

The non-demo `init` command starts with a single `alerts` module and no device
connections. Keep that module and add your chosen integration objects alongside
it, separated by commas. For example, this is a complete status-only setup:

```json
{
  "schema_version": 1,
  "name": "My dashboard",
  "listen": {"host": "127.0.0.1", "port": 8787},
  "public_url": "",
  "refresh_seconds": 15,
  "stale_after_seconds": 60,
  "modules": [
    {"id": "health", "type": "alerts", "name": "Dashboard health", "enabled": true},
    {
      "id": "websites", "type": "http_status", "name": "My website", "enabled": true,
      "timeout_seconds": 3,
      "services": [{"id": "website", "name": "Website", "url": "https://example.com", "path": "/", "expected_status": [200]}]
    }
  ]
}
```

Replace `https://example.com` before the connection check. Keep the generated
`secrets.json`; replacing it with a module example would discard your role tokens.
An empty `public_url` works for the local browser test. Set your real HTTPS URL
before remote access or a Kindle build. Save JSON as UTF-8 and do not add comments
or trailing commas. Restart your owned hub after config or secret changes.

Use the executables installed by the quickstart; shell activation is optional:

| Platform | Executable, from the checkout | Private directory |
| --- | --- | --- |
| Windows PowerShell | `.\.venv\Scripts\inkboard.exe` | `"$env:LOCALAPPDATA\Inkboard\home"` |
| macOS / Linux | `.venv/bin/inkboard` | `~/.config/inkboard/home` |

In the integration guides, `inkboard` means that executable and `PRIVATESETUP`
means your private directory. Run `check` first, then `check --connect`, then
`serve`. The first check validates without contacting providers; the second is
an explicit read-only network test. No check starts prints or submits media.

## Reader and operator sign-in

`init` generates three distinct role tokens. Keep them separate:

| Role | Use |
| --- | --- |
| `reader` | Browser/Kindle observations, camera snapshots and client downloads |
| `controller` | Enabled device actions and alert acknowledgement/quiet-hour changes; can also read |
| `ingest` | A separately configured outbound collector only; cannot read or operate devices |

Use `show-token --directory PRIVATESETUP --role reader` on your trusted terminal
to sign in to the browser. To operate a configured source, display the separate
`controller` token, expand **Operator controls**, paste it there and select
**Enable controls**. Supplying a controller token does not enable actions left
disabled in `config.json`. Disconnect clears tokens from page memory; reloading
or closing the page requires signing in again. Keep terminal output private.

The browser/API supports the integration actions listed in the individual
guides. The Kindle uses dedicated native contracts: printer controls and read-only
Plex observations are available, while the public client does not yet bridge
every browser media action. See [Kindle feature boundaries](../kindle.md#optional-advanced-apis)
before enabling an advanced native menu.

The dashboard stores selected fields rather than whole provider responses. A
failure in one module leaves the other modules running and retains the previous
timestamped view. Availability does not imply that control authorization works.
Provider actions never retry automatically after an uncertain outcome.

## Add a new provider

Implement a class with the small contract below, then register its selected type
in `inkboard.integrations.create_adapter` and the core configuration allowlist.
Use injected clients in tests; no test should require a real user's account.

```python
class MyAdapter:
    def read(self):
        return {
            "id": "example", "type": "custom", "name": "My service",
            "observed_at": "2026-01-01T00:00:00Z",
            "available": True, "summary": "Healthy",
            "metrics": {"requests": 12},
            "capabilities": [], "details": {}
        }

    def command(self, action, parameters, request_id):
        return {"status": "rejected", "message": "Read-only integration"}

    def close(self):
        pass
```

Keep observations bounded and typed; use the source's actual observation time.
Never include tokens, private URLs, raw exception text or raw API user objects.
Use a finite network timeout, validate TLS identity, refuse credential-bearing
redirects, and allowlist every control action and parameter. Report `confirmed`
only after the provider response or actual telemetry proves the stated result;
`sent` means accepted without final state evidence and `uncertain` means the
operator must inspect the state. A received API request is not proof that a
download finished or a printer moved. The core maintains durable request IDs;
adapter caches are only secondary deduplication.

See [`tests/test_integrations_*.py`](../../tests) for synthetic MQTT, camera,
certificate, HTTP, Seerr, timeout, idempotency and privacy examples.
