# Any HTTP service

Copy [`http-status.module.json`](../../examples/integrations/http-status.module.json)
into the private configuration's `modules` array. Replace each URL and choose a
stable path. This is a lightweight availability check for sites, local services
and a self-hosted status API; it does not need printer or media dependencies.

Keep the generated top-level fields and health module when adding this object;
see the [complete configuration example](README.md#edit-the-generated-setup-not-the-repository-examples).
Run your installed `inkboard check --directory PRIVATESETUP` first, then add
`--connect` for the explicit read-only network test. Start or restart your owned
hub after saving the configuration.

```json
{
  "id": "services",
  "type": "http_status",
  "name": "My services",
  "timeout_seconds": 3,
  "services": [
    {"id": "website", "name": "Website", "url": "https://example.com", "path": "/", "expected_status": [200]}
  ]
}
```

The URL is the scheme, host, optional port and optional fixed base path. The
`path` starts with `/` and is appended to it. Do not put a token, username,
password, query string or fragment into a base URL. This integration is for
unauthenticated endpoints; authenticated provider APIs need a purpose-built
adapter that keeps header secrets private.

Each module supports up to sixteen services. Set a sensible refresh interval in
the top-level configuration, usually 15–60 seconds. Each request has a finite
0.25–10 second network timeout. Multiple slow services add to a poll cycle's
duration; split unrelated services into modules when needed. System DNS lookup
behavior is also determined by your operating system and resolver.

Inkboard makes a GET request but reads no response body. It records only status,
response-header latency, a timestamp and a safe availability explanation. It
follows **no redirects**; configure a service's final canonical URL. The default
healthy status is 200, but you can explicitly include other codes, including an
intentionally expected 401/403 for a protected login page. That proves the HTTP
endpoint is responding, not that its authenticated application works.

This module cannot calculate CPU usage, memory, request rates, p95 latency or
disk space from a simple HTTP status. For those metrics, add a typed adapter to
your existing telemetry endpoint; follow the [adapter contract](README.md).
Never scrape private dashboards and expose their entire response as a module.
