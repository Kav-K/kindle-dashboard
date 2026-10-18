# Contributing

Start with the local demo and run the test suite before connecting hardware.
Keep modules independent: an unavailable printer must not break Plex or a service
status page. New adapters should use the documented envelope and capability
contract, finite I/O deadlines, honest freshness and sanitized error messages.

Add tests with local fake HTTP/MQTT endpoints. Automated tests must never need a
real account, printer, media library, paid provider or running personal service.
Tests for writes should prove authorization, exact targeting, idempotency and
uncertain-outcome behavior. Never retry a physical action merely because its
acknowledgement was lost.

After the [quickstart bootstrap](README.md#1-start-with-the-local-demo), run
the block for your platform from the checkout. No shell activation is needed:

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m pip install -e '.[bambu,test]'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/public_audit.py
```

```sh
# macOS/Linux
.venv/bin/python -m pip install -e '.[bambu,test]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tools/public_audit.py
```

Do not commit generated configuration, credentials, state, paired Kindle builds,
or personal snapshots. This project is licensed GPL-3.0-or-later; retain notices
for any third-party source you contribute. A pull request should describe which
fixture checks passed and which hardware behavior remains untested.

Before publishing, stage the exact reviewed files and run
the same environment's `python tools/public_audit.py --staged`. That gate also requires the inspected
tracked worktree to match the Git index, so a different staged blob cannot be
hidden by subsequently cleaning its worktree copy.
