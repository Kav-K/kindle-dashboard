# Updates, backups and recovery

## Hub updates

Keep your private setup directory separate from the checkout. Before updating,
stop your own hub service and copy that private directory to an equally private
backup location. It includes powerful credentials and state; do not upload it
as an issue attachment. Do not copy an old SQLite database over a running hub.

Review the release notes, then update the checkout and its environment:

```sh
git pull --ff-only
.venv/bin/python -m pip install '.[bambu]'
.venv/bin/inkboard check --directory ~/.config/inkboard/home
```

```powershell
# Windows PowerShell, from the checkout
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install ".[bambu]"
.\.venv\Scripts\inkboard.exe check --directory "$env:LOCALAPPDATA\Inkboard\home"
```

Run only the block for your platform. Keep the
extras you actually use. Restart the hub with the same directory, verify Health
and source timestamps, and confirm reader/controller roles still behave properly.
Bootstrap is a first-run command; it intentionally refuses to replace `.venv`.

## Kindle Wi-Fi updates

First install the paired package and root-owned recovery helper as described in
[Kindle setup](kindle.md). Later client updates need no USB:

1. Build a new client with the same public URL, device serial, selected printer
   IDs and helper identity. Use a version higher than the currently installed and
   published version, for example `0.1.2` after `0.1.1`.
2. Retain your current `client_updates` as a private rollback reference. Publish
   the new generated assets as the guide describes, preserving complete files
   and a manifest that refers only to verified assets.
3. On the Kindle choose **Check for Wi-Fi update**, install, then exit/reopen
   KOReader. Health distinguishes the loaded code from new files pending reopen.

The hub serves only three allowlisted Lua/version files, authenticated to the
reader role, with size and SHA-256 verification. The device additionally checks
its serial, package names, versions, TLS identity and native transaction. Hashes
detect corruption; your hub is the trusted update authority. Protect its code,
credentials and update directory accordingly. A compromised trusted hub can
supply new client code.

The USB zip and generated `usb/` tree can contain the one-time credential
handoff. **Never publish either** to GitHub Releases or a public web directory;
delete both after successful first pairing. Only the generated token-free update
assets belong on the authenticated hub. This release supports one paired native
client per hub update endpoint. Use separate hub instances for different native
serials/profiles.

Normal image/telemetry changes arrive from the hub without a client update. Use
the native updater only when its Lua UI, behavior or device configuration changes.

## Recovery

- A stale source keeps its timestamp and disables capabilities; repair its
  connectivity/credentials rather than resetting the whole dashboard.
- A command marked uncertain needs inspection of the actual printer/media state.
  Do not resend it automatically or delete receipts to make it look successful.
- If a Wi-Fi client update is interrupted, retain the transaction and backups.
  Follow the native helper's recovery path; do not repeatedly run a first-install
  script over an existing plugin.
- If the UI is broken, exit KOReader before USB access and preserve existing
  settings/credentials. Restore only a reviewed plugin backup after inspecting
  the current install. Do not install Amazon firmware as a repair step.
- The initial installer refuses to overwrite a different existing dashboard.
  Migrating a prior personal installation is a separate reviewed operation.
