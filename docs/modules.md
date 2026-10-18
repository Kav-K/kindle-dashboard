# Add or replace a module

`config.json` contains a `modules` list. Each item has a unique lowercase `id`, a
registered `type`, a friendly `name` and an optional `enabled` boolean. Remove or
disable modules you do not use; their credentials and optional packages are not
required. Complete examples live in [examples/integrations](../examples/integrations).

The built-in factory is `src/inkboard/integrations/__init__.py`. Adapters expose:

```python
class MyAdapter:
    def __init__(self, config, secrets):
        # Validate references here. Do not connect in the constructor.
        self.config = config

    def read(self):
        return {
            "id": self.config["id"],
            "type": self.config["type"],
            "name": self.config["name"],
            "observed_at": "2026-01-01T12:00:00Z",  # actual successful source time
            "available": True,
            "summary": "Readable, sanitized observation",
            "metrics": {},
            "capabilities": [],
            "details": {},
        }

    def close(self):
        pass
```

Use the current UTC time only when the source observation actually succeeds.
An old cached value must keep its older time. Preserve meaningful unavailable
and unknown values; do not turn an unreadable measurement into zero.

Register the type in the factory and config's `MODULE_TYPES`. Keep the import
lazy. Add a module example, its guide, and local provider fixtures. The browser
uses the module envelope; native views need the corresponding typed renderer/API
contract. Do not advertise a capability until both the command path and UI are
implemented and tested.

For actions, add `command(action, parameters, request_id)` and advertise a small
allowlist of capabilities. The hub owns authorization and durable receipts; the
adapter owns provider validation and observed confirmation. Return `confirmed`,
`rejected` or `uncertain`. An API acknowledgement is not proof that a physical
effect occurred. Bind job-specific actions to the currently observed print job.
Use finite deadlines; never resend an ambiguous mutation automatically.

Provider responses can contain tokens, account objects, addresses and private
paths. Select only necessary fields rather than passing raw JSON through. Never
render exception text. Use the bounded HTTP helper, prohibit redirects with
credentials, and verify TLS identity before transmitting device credentials.

Remote snapshot ingestion uses:

```json
{
  "module_id": "my-source",
  "snapshot": {
    "id": "my-source", "type": "http_status", "name": "My source",
    "observed_at": "2026-01-01T12:00:00Z", "available": true,
    "summary": "Sanitized observation", "metrics": {},
    "capabilities": [], "details": {}
  }
}
```

Send this to `/kindle-dashboard/api/v1/ingest` using the ingestion bearer token.
Only configured `remote` module IDs are accepted. Their command capabilities are
always cleared: an incoming telemetry document cannot authorize device control.

Run `inkboard check` without `--connect` during development for offline validation.
Tests must use fake sources and fresh temporary state, not the maintainer's
services or a contributor's actual devices.
