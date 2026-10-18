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
