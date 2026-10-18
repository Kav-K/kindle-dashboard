# Plex and optional Seerr

## Plex setup

Run Inkboard on a computer that can reach your Plex Media Server. Use the server's
local address, for example `http://PLEX_IP:32400`, or a correctly configured HTTPS
address. Keep unencrypted HTTP on your trusted host/LAN; use HTTPS or a private
tunnel for network paths outside it. Do not open Plex or Seerr management ports
to the internet just for Inkboard.

Get **your own** Plex authentication token using
[Plex's instructions](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
That page notes that its web-XML method can yield a temporary token; use the
linked developer authentication guidance for a durable integration credential.
Store the value using the quickstart's hidden secret prompt as `plex_token`.
The module configuration contains only `"token_secret": "plex_token"`.

```text
inkboard set-secret --directory PRIVATESETUP --name plex_token
```

Copy [`plex.module.json`](../../examples/integrations/plex.module.json) into
the private `modules` array. Replace `url` with your Plex address. **Remove the
entire `seerr` object** if you do not run Seerr. Plex itself does not require
Seerr, Sonarr, Radarr, Shelfarr, an ebook stack or Docker.

Start Inkboard and confirm that its Plex view lists the right libraries. Start
playback in your ordinary Plex client and check its title, state, progress,
transcoding and bandwidth. Library and Now Playing responses use Plex's JSON
API with `X-Plex-Token` in an HTTP header, never in the request URL. See the
[official Plex API](https://developer.plex.tv/pms/).

### Optional operator actions

Everything is read-only initially. Set `controls_enabled` to `true` to allow a
refresh of a **currently observed** library. The API call being accepted means
the refresh was requested; it does not mean the scan/import has finished.

To stop active streams, also set `allow_terminate_streams` to `true`. Only a
current observed session ID and explicit `confirm: "STOP"` are accepted. A
later Now Playing poll verifies that playback stopped. Leave this disabled if
you only want monitoring.

This adapter does not delete media, clear caches, run shell commands, restart
Plex, administer Docker or change filesystem permissions. Such host-level
features need a separate carefully scoped integration.

## Optional Seerr setup

Inkboard supports a reachable Seerr / compatible Jellyseerr or Overseerr API.
In Seerr open **Settings → General → API Key** as an administrator and save your
own value privately as `seerr_key`. Treat it as a powerful credential; it can
act with administrator permissions. Add the `seerr` object from the example,
replace its URL and keep `api_key_secret` as the secret reference.

```text
inkboard set-secret --directory PRIVATESETUP --name seerr_key
inkboard check --directory PRIVATESETUP
inkboard check --directory PRIVATESETUP --connect
```

Use the private directory created by the quickstart for `PRIVATESETUP`. The
secret commands prompt with hidden input. Offline validation makes no provider
request; `--connect` explicitly checks current read-only observations.

Configure and verify Seerr's connection to Radarr and Sonarr first. Each media
type needs the correct default server, quality profile and root folder, and
automatic search must be enabled if you expect approved requests to download
without further intervention. Inkboard cannot repair a missing indexer,
download client or unplugged SSD merely by submitting a request.
[Seerr service setup](https://docs.seerr.dev/using-seerr/settings/services/).

For new requests, set both the module's `controls_enabled` and Seerr's
`requests_enabled` to `true`. Search is read-only. TV details provide season
numbers: request the selected list, rather than relying on an ambiguous
"whole show" value. An explicit list such as `[1, 3]` requests those seasons.

Optional `destinations` is a mapping of friendly names to **your existing
Seerr/Radarr/Sonarr settings**:

```json
"destinations": {
  "shows-ssd": {
    "rootFolder": "/media/shows",
    "serverId": 0,
    "profileId": 1
  }
}
```

Replace the path and IDs with the values from your actual Seerr services.
These are paths as **Sonarr/Radarr** see them, which may differ from Windows
drive letters. Requests can select only configured names, not arbitrary paths.
Do not use a movie destination for TV or vice versa. If you omit destinations,
Seerr's configured defaults apply. An unplugged drive or an invalid root folder
must be fixed in the media stack before requests will complete.

Seerr itself decides auto-approval from the authenticating account's permissions.
Inkboard neither impersonates another user nor sets `ignoreQuota`. If you want
manual approval from this dashboard, also enable `approvals_enabled`. The action
requires `confirm: "APPROVE"`. See the
[official authentication reference](https://docs.seerr.dev/api/seerr-api/) and
[request API](https://docs.seerr.dev/api/create-new-request/).

### Data and actions

The request list displays the last 30 requests, request approval state, requested
seasons and the media's reported availability. Seerr's media states distinguish
unknown, pending, processing, partially available, available and deleted. These
are **not** download-client progress or ETA; adding acquisition telemetry is a
separate adapter extension. This public package does not claim Sonarr/Radarr/
download-client end-to-end tracking merely from a Seerr status.

| Action | Parameters | Extra enablement |
| --- | --- | --- |
| `refresh_library` | `library_id` currently listed by Plex | `controls_enabled` |
| `terminate_stream` | current `session_id`, `confirm: "STOP"` | Also `allow_terminate_streams` |
| `seerr_search` | `query`, optional `page` | Configured Seerr only |
| `seerr_tv` | positive TMDB `media_id` | Configured Seerr only |
| `seerr_request` | `media_type`: movie/tv, TMDB `media_id`, TV `seasons` list, optional `destination`, boolean `is4k` | Also `requests_enabled` |
| `seerr_approve` | positive `request_id`, `confirm: "APPROVE"` | Also `approvals_enabled` |

Every action requires a unique core `request_id`. Duplicate IDs return their
existing receipt. If a POST times out it may already have created a request:
check Seerr's list before sending another one. `confirmed` for Seerr means its
API recorded the request/approval, not that the title is available in Plex.

The adapter deliberately strips raw `requestedBy` and other user objects because
some Seerr responses can include unrelated Plex tokens. It never stores a raw
provider response in the dashboard snapshot.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Credential rejected | Correct private secret reference/value and sufficient server permission. A Plex password change with device sign-out can revoke old tokens. |
| Redirect error | Configure the final service URL, including any URL base. Tokens are not forwarded to redirects. |
| Plex works, Seerr fails | Verify the optional Seerr URL/key separately. Live Plex streams remain visible. |
| Request approved but nothing downloads | Check Seerr's Radarr/Sonarr service health, default profiles/root folders, search setting, indexers and download client. |
| Wrong SSD | Correct the named destination path/IDs and inspect how Sonarr/Radarr mount that drive. |
| Stopped stream still listed | Refresh Now Playing; accepted control and observed playback state are distinct. |
| Request list shows no title | The API may omit title metadata; TMDB ID and request ID remain available. Search the title through Seerr rather than treating a blank title as a failed request. |
