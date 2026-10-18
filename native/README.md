# Native client source

The generated plugin is built from `main.lua.in`, `_meta.lua` and a generated
`VERSION`. The template derives from the original dashboard client 0.5.47, with
owner identities removed and public image hit regions adjusted to this hub.
`tools/build_kindle.py` makes a device-bound public 0.1.x build. Do not copy an
unexpanded `.in` file to a Kindle.

The public client uses verified HTTPS to its configured DNS origin. It retains
the original parsers, action receipts, safe timeout behavior, everyday settings,
color UI, alert UI and device-health behavior. Advanced integrations are disabled
unless a separately compatible backend is explicitly configured. The public hub
does not acquire the private deployment's credentials, cloud account, automation
workers or system administration access.

`ota-file-recovery.lua.in` is the root-owned API2 updater. The builder binds it to
the device serial and embeds its exact SHA-256 in both the plugin and
`startup-recovery.lua.in`. The startup patch executes before plugin discovery
only when a private update journal exists. It preserves unknown newer files and
does not execute an unverified helper. This fixed helper is not updated over Wi-Fi.

Native tests contain independent inert KOReader objects and retained in-memory
update failure matrices. They import no private commissioning code, operational
receipts, hosts or credentials. The first-install shell fixture substitutes only
absolute device paths, root identity and sync; its file/hash/copy behavior runs
against a temporary tree. Physical Kindle acceptance remains separate.

See [the Kindle setup guide](../docs/kindle.md) for installation, update limits,
security, current feature coverage and troubleshooting.
