# Security

Keep the hub on loopback behind a trusted HTTPS reverse proxy. Do not forward
printer MQTT/camera, Plex, Seerr, or a Kindle SSH port onto the public internet.
The Kindle needs only your hub's authenticated HTTPS endpoint when away from home.

Reader, controller and ingest roles use separate generated credentials. Store
them in the private setup directory. Use hidden-input provisioning commands and
file references; do not put credentials into command arguments, URLs, issues,
screenshots, commits or environment examples. A paired Kindle USB package
contains a one-time credential handoff: keep it private and delete the temporary
copy once installation is complete. Release source archives are not paired
device packages.

Printer credentials remain on the collector host. Choose either verified
printer TLS certificates or an explicitly verified certificate fingerprint.
Do not disable verification to fix a failed connection. Monitoring support and
control authorization depend on the printer's firmware; see the Bambu guide.

Commands are recorded before dispatch and are never automatically resent after
an ambiguous outcome or restart. Check the actual device before creating a new
request. Restrict control credentials to trusted operators: pause, stop and media
requests have physical or storage effects. AI/phone automation and arbitrary
remote shell execution are not implicit capabilities of this public setup.

Report a vulnerability through GitHub's private security advisory workflow when
available. If it is not available, open an issue asking for a private reporting
channel without including the vulnerability, credentials, or private logs.
