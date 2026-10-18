# Bambu P1S printers

## Decide whether you need monitoring or controls

Start with **monitoring** and keep your existing Bambu Studio / Bambu Handy
workflow. The adapter connects directly to your printer on your trusted local
network and subscribes to status. Whether local reports and camera access remain
available in cloud mode depends on the printer model and firmware. If the
printer refuses its local access code, this adapter does not bypass that refusal.

For direct pause, resume, stop, speed and chamber-light controls, this public
release supports the manufacturer's **LAN Only + Developer Mode** route. Enable
that mode yourself on the printer only after reading Bambu's current explanation.
LAN Only mode disconnects that printer from Bambu Cloud, so **Bambu Handy will
not retain its cloud functionality**. Bambu Studio can still send locally to a
reachable printer using its local access code; verify this before relying on the
dashboard. Keep cloud mode if Handy is essential and use this release read-only.
No proprietary authentication keys or signed-client impersonation are included.
[Bambu's integration explanation](https://forum.bambulab.com/t/updates-and-third-party-integration-with-bambu-connect/137408/),
[official Developer Mode guide](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode).

## What you need

- A P1S reachable from the computer running Inkboard. Reserve its IP in your
  router to keep it stable. Do not expose printer ports to the public internet.
- Its exact serial number and **local access code**, shown by the printer's
  network/LAN settings. Use the printer's screen to confirm the IP. Menus vary by
  firmware; the local access code is different from your Bambu account password.
- A verified printer TLS certificate fingerprint or a locally provisioned CA
  file that validates that printer's serial-number certificate.
- Install optional MQTT support into the **same Python environment** as Inkboard:

```powershell
# Windows PowerShell, from the checkout
.\.venv\Scripts\python.exe -m pip install ".[bambu]"
```

```sh
# macOS / Linux, from the checkout
.venv/bin/python -m pip install '.[bambu]'
```

This step is already complete if you used bootstrap's `-Bambu` / `--bambu`
option. Pillow, used to validate camera JPEGs, is a normal dashboard
dependency. No Bambu cloud login is required or collected.

## 1. Discover and verify TLS identity

On a trusted LAN, verify the printer IP on the physical printer and in your
router, then run this credential-free probe from the repository root:

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe .\examples\integrations\inspect_printer_certificate.py PRINTER_IP
```

```sh
# macOS / Linux
.venv/bin/python examples/integrations/inspect_printer_certificate.py PRINTER_IP
```

It performs a TLS handshake and prints a SHA256 fingerprint. It sends **no access
code**, changes no configuration and trusts nothing automatically. First contact
on a compromised network cannot establish a trustworthy identity by itself. If
you already have a trusted certificate, compare against it. Otherwise perform
this initial enrollment only on a trusted network after confirming the physical
printer address. Copy the 64 hexadecimal characters into `tls_fingerprint`.

The default MQTT port is **8883**. The default P1S snapshot port is **6000**.
Some firmware may use different camera protocols; this adapter specifically
implements the P1S authenticated JPEG feed, not RTSP or every Bambu model.
Camera and MQTT certificates must match the configured pin. If they genuinely
use different certificates, use your trusted CA file instead of weakening TLS.

With a trusted CA, omit `tls_fingerprint` and set `tls_ca_file` to its absolute
local path. The adapter checks both the CA chain and exact serial common name.
Do not copy a stranger's certificate pin, CA or access code from an example.

## 2. Configure one or more printers

Copy [`bambu.module.json`](../../examples/integrations/bambu.module.json) into
the `modules` array in your **private** configuration. Replace the hostname,
serial and fingerprint. Save the access code as the private secret named
`printer_one_code` using the quickstart's secret command; the module contains
only `"access_code_secret": "printer_one_code"`.

Use the [executable and private-directory conventions](README.md#edit-the-generated-setup-not-the-repository-examples)
for the commands below. This example's printer ID is `printer-one`; select that
same ID in `kindle.printer_ids` when building the Kindle client. Do not use the
module ID `printers` or the printer serial in that field.

```text
inkboard set-secret --directory PRIVATESETUP --name printer_one_code
inkboard check --directory PRIVATESETUP
inkboard check --directory PRIVATESETUP --connect
```

Replace `PRIVATESETUP` with the private directory from the quickstart. The first
command prompts with hidden input. The second validates offline; `--connect`
explicitly attempts read-only connectivity and allows up to eight seconds for
the TLS/MQTT connection and first report. The check closes its listener when it
finishes. The normal running hub continues reconnecting with backoff; if the
check fails, inspect its network/credential explanation and verify the running
hub receives a real report before enabling controls.

Each printer needs a unique lowercase `id`. Add another object to the `printers`
array for additional printers, each with its own secret reference and TLS pin.
One module supports at most eight printers. Module and printer IDs are stable
keys; rename the display `name` when you only want a different label.

For monitoring, leave:

```json
"controls_enabled": false
```

and each printer's `"mode": "cloud"`. These fields describe the mode you have
actually enabled; setting them does **not** change firmware or the printer.

For controls, first verify LAN Only + Developer Mode on the physical printer,
confirm Bambu Studio can print to it locally, then set the module's
`controls_enabled` to `true` and that printer's `mode` to `"lan_developer"`.
You can also set one printer's `controls_enabled` to `false` independently.

## 3. Verify in order

1. Start or restart your Inkboard process with the private configuration.
2. Verify that the module reports the correct printer, state and temperatures.
3. Expand its camera preview. A missing camera does not invalidate MQTT status;
   enable the printer's local camera/liveview setting if required by firmware.
4. Test the chamber light before testing print controls. Only pause/resume a
   print you are physically prepared to interrupt. Confirm STOP explicitly.
5. If a command is `uncertain`, inspect the printer's actual state before doing
   anything else. An MQTT publish is not a confirmed physical operation.

Snapshots are cached for ten seconds and timestamped. Capture requests have a
total frame deadline, size and decoded-dimension limits. No snapshots are sent
to an AI provider in this public release. Keep the collector on the printer LAN;
the remote Kindle talks only to your HTTPS dashboard, never directly to a printer.

## Reliability and supported controls

The MQTT listener keeps one connection per configured printer and reconnects
with a 2–60 second backoff. Reports are merged as deltas. A full status query is
rate-limited to once every five minutes per process, including reconnects. An
interrupted connection must provide fresh telemetry before controls return;
retained MQTT messages are ignored. Unchanged state fields are valid only within
one authenticated connection and one full-status cycle. Sleep/wake checks use
both a monotonic clock and wall-clock age.

| Action | Required state | Parameters |
| --- | --- | --- |
| Pause | Printing | Current `printer_id` and `job_identity` |
| Resume | Paused | Current `printer_id` and `job_identity` |
| Stop | Printing / paused | Current binding plus `confirm: "STOP"` |
| Speed | Printing | Current binding plus integer `value`: 1 silent, 2 standard, 3 sport, 4 ludicrous |
| Light | Connected, fresh state | `printer_id`, `value`: `"on"` or `"off"` |

Job bindings use a local authenticated-session UUID and observed job counter;
they are not provider job IDs. A reconnect or newly observed job invalidates
old controls. Reprint/start/upload, arbitrary G-code, AMS reconfiguration and
heating/motion passthrough are intentionally not part of this public adapter.
Continue using Bambu Studio to start prints.

The allowlisted protocol structures and speed bounds were checked against the
[official Bambu Studio source](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceManager.cpp)
and its [speed enumeration](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/DeviceCore/DevDefs.h).
The P1S camera framing is independently described in the
[Home Assistant Bambu integration source](https://github.com/greghesp/ha-bambulab/blob/main/custom_components/bambu_lab/pybambu/bambu_client.py).
Protocol source research and synthetic tests do not certify every firmware;
physical printer acceptance remains your setup check.

## Troubleshooting

| Message / symptom | Check |
| --- | --- |
| Install the bambu extra | Activate the same virtual environment and install `.[bambu]`. |
| TLS identity mismatch | Check the physical printer IP and pin; do not disable verification. An unexpected certificate change needs investigation. |
| Local credential rejected | Recheck serial and local access code on the printer; a reset or firmware mode change may rotate it. |
| Status works, controls absent | Inspect `controls_enabled`, actual LAN Developer Mode, fresh reported state and the per-printer explanation. Idle printers cannot pause. |
| Connected, waiting for report | Wait for status bootstrap; reconnects respect the five-minute full-status rate gate. Check local MQTT permission and firewall. |
| Camera unavailable | Check local liveview permission, port 6000 reachability, P1S protocol compatibility and printer authentication. Do not expose the port externally. |
| Command uncertain | Inspect the real screen/telemetry. Do not automatically send it again. |
