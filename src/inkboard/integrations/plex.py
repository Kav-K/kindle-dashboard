"""Plex and optional Seerr using the owner's own API credentials.

No filesystem deletion, shell execution, container administration or automatic
request approval is included. Seerr retains its own approval policy.
"""
from __future__ import annotations

import re
from urllib.parse import quote, urlencode
from .common import Adapter, HTTPClient, IntegrationError, integer, number, secret, text, utc_now


def container(document):
    if not isinstance(document, dict) or not isinstance(document.get("MediaContainer"), dict):
        raise IntegrationError("Plex returned an unexpected response shape.")
    return document["MediaContainer"]


def rows(value, limit):
    if not isinstance(value, list):
        return []
    return [row for row in value[:limit] if isinstance(row, dict)]


def plain_int(value):
    if type(value) is int and 0 <= value <= 2_147_483_647:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        result = int(value)
        return result if result <= 2_147_483_647 else None
    return None


def media_result(row):
    media_type = row.get("mediaType")
    if media_type not in ("movie", "tv"):
        return None
    identifier = plain_int(row.get("id"))
    if identifier is None:
        return None
    return {"id": identifier, "media_type": media_type,
            "title": text(row.get("title") or row.get("name"), 160),
            "year": text(row.get("releaseDate") or row.get("firstAirDate"), 10),
            "overview": text(row.get("overview"), 500)}


class PlexAdapter(Adapter):
    def __init__(self, config, secrets):
        super().__init__(config, secrets)
        self.client = HTTPClient(config["url"], {"X-Plex-Token": secret(secrets, config.get("token_secret")),
                                 "X-Plex-Product": "Inkboard", "X-Plex-Client-Identifier": "inkboard-" + self.id},
                                 config.get("timeout_seconds", 4))
        self.controls = config.get("controls_enabled", False) is True
        self._libraries = set()
        self._sessions = set()
        self._last_plex = None
        self._last_seerr = None
        self.seerr = None
        self.seerr_config = config.get("seerr", {})
        if self.seerr_config:
            if not isinstance(self.seerr_config, dict):
                raise IntegrationError("seerr must be a configuration object.")
            self.seerr = HTTPClient(self.seerr_config["url"],
                                    {"X-Api-Key": secret(secrets, self.seerr_config.get("api_key_secret"))},
                                    config.get("timeout_seconds", 4))
        self.destinations = self.seerr_config.get("destinations", {})
        if not isinstance(self.destinations, dict) or len(self.destinations) > 16:
            raise IntegrationError("Seerr destinations must be a mapping with at most 16 entries.")
        for name, destination in self.destinations.items():
            if not isinstance(name, str) or not 1 <= len(name) <= 64 or not isinstance(destination, dict):
                raise IntegrationError("Invalid named Seerr destination.")
            if set(destination) - {"rootFolder", "serverId", "profileId"}:
                raise IntegrationError("Unknown Seerr destination field.")
            if "rootFolder" in destination and (not isinstance(destination["rootFolder"], str)
                    or not 1 <= len(destination["rootFolder"]) <= 512):
                raise IntegrationError("Invalid destination rootFolder.")
            for key in ("serverId", "profileId"):
                if key in destination:
                    integer(destination[key])

    def read(self):
        details = {"streams": [], "libraries": [], "requests": [], "destinations": list(self.destinations)}
        capabilities = []
        try:
            sessions = container(self.client.request("/status/sessions"))
            libraries = container(self.client.request("/library/sections"))
            stream_ids = set()
            transcodes = 0
            bandwidth = 0
            for row in rows(sessions.get("Metadata"), 64):
                session = row.get("Session") if isinstance(row.get("Session"), dict) else {}
                player = row.get("Player") if isinstance(row.get("Player"), dict) else {}
                transcode = row.get("TranscodeSession") if isinstance(row.get("TranscodeSession"), dict) else {}
                identifier = text(session.get("id"), 128)
                if identifier and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier):
                    stream_ids.add(identifier)
                title = text(row.get("title"), 120)
                if row.get("type") == "episode":
                    show = text(row.get("grandparentTitle"), 100)
                    title = (show + " · " + title).strip(" ·")
                duration, offset = number(row.get("duration")), number(row.get("viewOffset"))
                is_transcoding = transcode.get("videoDecision") == "transcode" or transcode.get("audioDecision") == "transcode"
                transcodes += is_transcoding
                stream_bandwidth = number(session.get("bandwidth")) or 0
                bandwidth += stream_bandwidth
                details["streams"].append({"id": identifier, "title": title,
                    "player": text(player.get("title") or player.get("product"), 80),
                    "state": text(player.get("state"), 24), "position_ms": offset,
                    "duration_ms": duration, "progress": round(min(100, 100 * offset / duration), 1)
                    if duration and offset is not None else None, "transcoding": bool(is_transcoding),
                    "transcode_progress": number(transcode.get("progress"), 0, 100),
                    "bandwidth_kbps": stream_bandwidth, "location": text(session.get("location"), 24)})
            library_ids = set()
            for row in rows(libraries.get("Directory"), 64):
                identifier = text(row.get("key"), 12)
                if not re.fullmatch(r"[0-9]{1,8}", identifier):
                    continue
                library_ids.add(identifier)
                details["libraries"].append({"id": identifier, "name": text(row.get("title"), 100),
                    "type": text(row.get("type"), 24), "refreshing": row.get("refreshing") is True})
            self._sessions, self._libraries = stream_ids, library_ids
            details["plex_available"] = True
            details["plex_observed_at"] = utc_now()
            if self.controls:
                capabilities = ["refresh_library"]
                if self.config.get("allow_terminate_streams") is True:
                    capabilities.append("terminate_stream")
            metrics = {"active_streams": len(details["streams"]), "transcodes": transcodes,
                       "bandwidth_kbps": bandwidth, "libraries": len(details["libraries"])}
            self._last_plex = {"streams": details["streams"], "libraries": details["libraries"],
                               "plex_observed_at": details["plex_observed_at"], "metrics": dict(metrics)}
        except IntegrationError as exc:
            self._sessions, self._libraries = set(), set()
            details["plex_available"], details["plex_error"] = False, str(exc)
            metrics = {}
            details["plex_observed_at"] = None
            if self._last_plex:
                details.update({key: self._last_plex[key] for key in ("streams", "libraries", "plex_observed_at")})
                metrics.update(self._last_plex["metrics"])
        if self.seerr:
            try:
                document = self.seerr.request("/api/v1/request?take=30&skip=0&sort=modified")
                if not isinstance(document, dict) or not isinstance(document.get("results"), list):
                    raise IntegrationError("Seerr returned an unexpected request list.")
                for row in rows(document["results"], 30):
                    media = row.get("media") if isinstance(row.get("media"), dict) else {}
                    details["requests"].append({"id": plain_int(row.get("id")),
                        "media_id": plain_int(media.get("tmdbId")), "media_type": text(row.get("type"), 16),
                        "title": text(row.get("title") or media.get("title") or media.get("name"), 160),
                        "request_status": plain_int(row.get("status")), "media_status": plain_int(media.get("status")),
                        "seasons": [plain_int(s.get("seasonNumber")) for s in rows(row.get("seasons"), 100)
                                    if plain_int(s.get("seasonNumber")) is not None],
                        "updated_at": text(row.get("updatedAt"), 40)})
                details["seerr_available"] = True
                details["seerr_observed_at"] = utc_now()
                self._last_seerr = {"requests": details["requests"], "seerr_observed_at": details["seerr_observed_at"]}
                capabilities += ["seerr_search", "seerr_tv"]
                if self.controls and self.seerr_config.get("requests_enabled") is True:
                    capabilities.append("seerr_request")
                if self.controls and self.seerr_config.get("approvals_enabled") is True:
                    capabilities.append("seerr_approve")
            except IntegrationError as exc:
                details["seerr_available"], details["seerr_error"] = False, str(exc)
                details["seerr_observed_at"] = None
                if self._last_seerr:
                    details.update(self._last_seerr)
        available = details["plex_available"] or details.get("seerr_available", False)
        details["degraded"] = not details["plex_available"] or (self.seerr is not None and not details.get("seerr_available"))
        summary = f"{len(details['streams'])} active streams" if details["plex_available"] else "Plex needs attention"
        if details.get("seerr_available") is False:
            summary += " · Seerr unavailable"
        return self.snapshot(available=available, summary=summary, metrics=metrics, details=details, capabilities=capabilities)

    def command(self, action, parameters, request_id):
        if not isinstance(parameters, dict):
            return {"status": "rejected", "message": "Action parameters must be an object."}
        return self._once(action, parameters, request_id, lambda: self._perform(action, parameters))

    def _perform(self, action, parameters):
        allowed = {"refresh_library": {"library_id"}, "terminate_stream": {"session_id", "confirm"},
                   "seerr_search": {"query", "page"}, "seerr_tv": {"media_id"},
                   "seerr_request": {"media_id", "media_type", "seasons", "destination", "is4k"},
                   "seerr_approve": {"request_id", "confirm"}}
        if action not in allowed or set(parameters) - allowed[action]:
            raise IntegrationError("Unknown action or parameter.")
        if action not in ("seerr_search", "seerr_tv") and not self.controls:
            raise IntegrationError("Controls are disabled in this module's configuration.")
        if action == "refresh_library":
            identifier = str(parameters.get("library_id", ""))
            if identifier not in self._libraries:
                raise IntegrationError("Refresh the dashboard and select a currently observed library.")
            self.client.request("/library/sections/" + identifier + "/refresh", json_response=False)
            return {"status": "sent", "message": "Plex accepted the library refresh request; the next poll shows its state."}
        if action == "terminate_stream":
            identifier = parameters.get("session_id")
            if (self.config.get("allow_terminate_streams") is not True or parameters.get("confirm") != "STOP"
                    or identifier not in self._sessions):
                raise IntegrationError("Select a current stream and explicitly confirm STOP.")
            self.client.request("/status/sessions/terminate?" + urlencode({"sessionId": identifier,
                                "reason": "Stopped by server operator using Inkboard"}), json_response=False)
            return {"status": "sent", "message": "The stream stop request was sent. Verify it in Now Playing."}
        if not self.seerr:
            raise IntegrationError("Seerr is not configured.")
        if action == "seerr_search":
            query = parameters.get("query")
            if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
                raise IntegrationError("Search for between 2 and 100 characters.")
            page = integer(parameters.get("page", 1), 1, 100)
            result = self.seerr.request("/api/v1/search?" + urlencode({"query": query.strip(), "page": page}))
            if not isinstance(result, dict):
                raise IntegrationError("Seerr returned an invalid search result.")
            results = [media_result(row) for row in rows(result.get("results"), 20)]
            return {"status": "confirmed", "message": "Search results loaded.",
                    "data": {"results": [row for row in results if row], "page": page}}
        identifier = integer(parameters.get("media_id"), 1) if action in ("seerr_tv", "seerr_request") else None
        if action == "seerr_tv":
            result = self.seerr.request("/api/v1/tv/" + str(identifier))
            if not isinstance(result, dict):
                raise IntegrationError("Seerr returned an invalid show result.")
            return {"status": "confirmed", "message": "Seasons loaded.", "data": {
                "id": identifier, "title": text(result.get("name"), 160),
                "seasons": [{"number": plain_int(row.get("seasonNumber")),
                             "name": text(row.get("name"), 100), "episodes": plain_int(row.get("episodeCount"))}
                            for row in rows(result.get("seasons"), 100) if plain_int(row.get("seasonNumber")) is not None]}}
        if action == "seerr_approve":
            if self.seerr_config.get("approvals_enabled") is not True or parameters.get("confirm") != "APPROVE":
                raise IntegrationError("Approvals require configuration and explicit APPROVE confirmation.")
            request = integer(parameters.get("request_id"), 1)
            # An API response confirms the approval mutation, not downstream download success.
            self.seerr.request(f"/api/v1/request/{request}/approve", method="POST", body={})
            return {"status": "confirmed", "message": "Seerr approved the request. Download/import availability remains separate."}
        if self.seerr_config.get("requests_enabled") is not True:
            raise IntegrationError("New media requests are disabled in this module's configuration.")
        media_type = parameters.get("media_type")
        if media_type not in ("movie", "tv"):
            raise IntegrationError("media_type must be movie or tv.")
        body = {"mediaType": media_type, "mediaId": identifier}
        if media_type == "tv":
            seasons = parameters.get("seasons")
            if (not isinstance(seasons, list) or not 1 <= len(seasons) <= 100
                    or len(set(str(s) for s in seasons)) != len(seasons)):
                raise IntegrationError("Select one or more unique season numbers.")
            body["seasons"] = [integer(s, 0, 1000) for s in seasons]
        elif "seasons" in parameters:
            raise IntegrationError("Season selection applies only to TV requests.")
        if "is4k" in parameters:
            if type(parameters["is4k"]) is not bool:
                raise IntegrationError("is4k must be a boolean.")
            body["is4k"] = parameters["is4k"]
        if "destination" in parameters:
            destination = parameters["destination"]
            if not isinstance(destination, str) or destination not in self.destinations:
                raise IntegrationError("Select a configured destination.")
            body.update(self.destinations[destination])
        try:
            result = self.seerr.request("/api/v1/request", method="POST", body=body)
        except IntegrationError as exc:
            # A network error after POST may have committed remotely. Never call it rejected.
            if "network timeout" in str(exc) or "invalid JSON" in str(exc):
                return {"status": "uncertain", "message": "Seerr may have created the request. Check its request list before submitting again."}
            raise
        if not isinstance(result, dict) or plain_int(result.get("id")) is None:
            return {"status": "uncertain", "message": "Seerr responded without a request ID. Check its request list before submitting again."}
        return {"status": "confirmed", "message": "Seerr recorded the request; its configured approval policy applies.",
                "data": {"request_id": result["id"], "request_status": plain_int(result.get("status"))}}
