# Verification and feature boundaries

## Repeat the clean setup check

From a new checkout with Python 3.11+:

```sh
python -m venv .venv
# Windows: use .venv\Scripts\python.exe instead of .venv/bin/python below.
.venv/bin/python -m pip install '.[bambu,test]' build
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/public_audit.py
.venv/bin/python -m build
.venv/bin/python tools/verify_setup.py --wheel-dir dist
```

The clean verifier uses another fresh virtual environment and installs the
actual wheel. It initializes a temporary private demo through the real CLI,
checks that reinitialization cannot overwrite credentials, loads the browser
shell, tests authenticated data/role separation and a rendered PNG, restarts its
owned loopback server, builds a synthetic device's USB package and retrieves all
three update assets through the authenticated HTTP API. It destroys only its
own temporary directory/processes. Source templates/tools come from the cloned
repository; a wheel alone is not the complete native-device installation kit.

GitHub Actions performs fixture and clean-install checks on Linux, Windows and
macOS, with an additional minimum-Python job. Actions use immutable commit pins
and read-only repository permissions. No custom account credentials are needed.

## What automated fixtures cover

- Private configuration/secrets, role separation, invalid input, bounded
  requests, unsupported features and stale-capability gating.
- Cached observations, partial-provider failures, independent recovery and
  durable command identity/outcome handling.
- Fake Plex/Seerr HTTP responses, request/season/destination validation and
  removal of provider objects that might contain tokens.
- Fake MQTT/camera transports, TLS trust checks, current print-job binding,
  state-confirmed controls and ambiguous outcomes without automatic resend.
- A complete LuaJIT load of the generated native plugin, typed hub responses,
  header/touch behavior, HTTPS/OTA validation and the atomic recovery state machine.
- Synthetic first-install shell scenarios, device identity, hashes, no-overwrite
  rules, interrupted transactions and cleanup. These operate on fixtures, not a
  physical Kindle's root filesystem.
- Publication scans for credential files, private keys, token formats and
  accidental personal paths. Before committing use the additional `--staged`
  audit to require the scanned worktree to match the Git index.

Fixtures never use the maintainer's services, an actual printer, a real media
request, paid AI/phone API, firmware update, or production data/credentials.

## Supported surfaces

| Feature | Browser | Native Kindle in this public release |
| --- | --- | --- |
| Bambu status / camera / opt-in supported LAN controls | Yes | Yes |
| Plex libraries and Now Playing | Yes | Yes, read-only native Plex view |
| Plex library refresh / opt-in stream stop | Yes | Not the private operator bridge |
| Seerr search, seasons, configured destination, request/approval | Yes | Legacy advanced request menus remain disabled |
| HTTP service status | Yes | Service status images |
| Source-health notices | Yes | Alerts inbox |
| Screen brightness, night dimming, offline cache, self-health | Not a browser device control | Yes |
| Origin request analytics / hot-path snapshots | Extension | Requires a compatible analytics backend |
| AI Print Doctor / SMS / phone / automatic pause | Extension | Not provisioned here |
| Acquisition download-client progress/ETA, GPU subtitles, media deletion, OS recovery | Extension | Not provisioned here |

Retaining an advanced native menu implementation does not install its external
backend. The default public profile disables those entry points and explains
what is missing; do not enable a flag just to make an unsupported button visible.

## Physical checks you still perform

Check the current upstream jailbreak compatibility and exact firmware yourself.
After first install, confirm color/taps, brightness levels, Wi-Fi update/reopen,
power behavior, screenshot freshness and operation on a different network.
Printing controls depend on your model/firmware/security settings: start with a
safe test print. Do not stop a valuable print or reprint onto an occupied bed to
test this application. A transport fixture cannot establish physical safety.

External DNS, certificates/tunnels, startup registration and hardware failures
must be validated in your own environment. The guides deliberately separate
these checks from synthetic CI success.
