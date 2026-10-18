# Running unattended

First get the foreground demo, real module observations and HTTPS working. The
templates in [deploy](../deploy) are reviewed examples, not installers that change
your machine automatically. Replace every placeholder with your own absolute
paths and account. Never run the application as root merely to obtain boot startup.

The hub needs a stable network connection, writable private `state/`, correct
clock and an awake computer. It keeps bounded cached observations and persistent
command receipts. Failed source polls do not require restarting the whole hub.
After a crash/restart, an in-flight command becomes uncertain instead of being
sent again. Inspect its actual outcome before a new request.

## Linux: systemd at boot

Use `deploy/inkboard.service.in` with a dedicated unprivileged account. Fill in:

- `__USER__`: the account that owns the private setup directory.
- `__VENV__`: the checkout's virtual environment, with all required optional
  dependencies installed and readable by that account.
- `__CONFIG__`: the initialized private setup directory, containing config,
  secrets, and its writable state subdirectory.

The unit uses restart backoff, no new privileges, a private temporary directory,
read-only system paths, and a narrow writable state path. Check path quoting if
you use spaces. Save the configured file separately as `inkboard.service`, then:

```sh
sudo install -m 644 inkboard.service /etc/systemd/system/inkboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now inkboard.service
systemctl status inkboard.service
journalctl -u inkboard.service --since '10 minutes ago'
```

These commands register your chosen unit; they do not initialize credentials.
Run `inkboard check --directory ...` as the service account first. Keep the code
directory separate from writable state. Configure Caddy/cloudflared startup too.

An optional split telemetry collector has a separate service template. Its
private config and ingestion credential belong to that collector account; see
[remote access](remote-access.md) for the read-only split-collector boundary.

## macOS: launchd

Fill in `deploy/com.example.inkboard.plist.in` with your absolute virtualenv and
setup paths, save it as a local plist and check it with `plutil -lint`. A
per-user LaunchAgent in `~/Library/LaunchAgents` starts after that account logs
in. It does **not** establish pre-login boot availability.

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.inkboard.plist
launchctl print gui/$(id -u)/com.example.inkboard
```

Use an explicit restricted account and a properly installed LaunchDaemon if you
need pre-login operation. That is an OS administrator registration, not something
the application silently grants itself. Avoid putting credentials directly in
plist arguments or environment variables.

## Windows: an unprivileged login task

After checking your private setup, register the supplied current-user task:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\Register-CurrentUserTask.ps1 `
  -InkboardExe (Resolve-Path .\.venv\Scripts\inkboard.exe).Path `
  -ConfigDirectory "$env:LOCALAPPDATA\Inkboard\home"
```

The script refuses an existing task with that name, validates config offline,
uses Limited privileges, prevents overlapping instances and configures restart
attempts. It changes no firewall, SSH or remote-administration settings. Start
the task in Task Scheduler and inspect its result before relying on it.

This task requires the account to be signed in. For operation before sign-in,
an administrator must deliberately configure a suitable noninteractive service
account/task with its own file access and network permissions. The public
installer does not enable automatic desktop login or weaken Windows protections.

## Power and verification

Use AC power for long-running hub/Kindle operation. Configure the host's normal
power settings so it does not sleep while serving the dashboard. A closed laptop
lid, power loss, Wi-Fi loss or unplugged drive can still remove a data source;
freshness and capability gating make that visible.

After registering startup, test your own reboot:

1. Confirm hub and HTTPS proxy started under the intended account.
2. Open the dashboard from another network and inspect Health/source timestamps.
3. Confirm the original setup tokens are still accepted and unauthenticated API
   data is rejected.
4. Check that an unplugged/offline source stays stale without disabling healthy
   peers; reconnect it and verify a newer real observation.
5. Inspect any uncertain command before submitting another action.

The repository's automated tests simulate process restarts. They do not certify
a real macOS/Windows administrator registration, physical reboot, printer pause,
phone call, or every user's external proxy/DNS configuration.
