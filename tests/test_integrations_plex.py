import json
import unittest
from copy import deepcopy
from inkboard.integrations.common import IntegrationError, TransportError
from inkboard.integrations.plex import PlexAdapter


class API:
    def __init__(self, routes): self.routes, self.calls = routes, []
    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        result = self.routes.get(path, {})
        if isinstance(result, Exception): raise result
        return deepcopy(result)


def adapter(controls=False, seerr=False):
    config = {"id": "media", "type": "plex", "name": "Media", "url": "http://plex.invalid:32400",
              "token_secret": "plex_token", "controls_enabled": controls}
    if seerr:
        config["seerr"] = {"url": "http://seerr.invalid:5055", "api_key_secret": "seerr_key",
             "requests_enabled": True, "approvals_enabled": True,
             "destinations": {"ssd": {"rootFolder": "/movies", "serverId": 1, "profileId": 2}}}
    result = PlexAdapter(config, {"plex_token": "fixture-plex", "seerr_key": "fixture-seerr"})
    result.client = API({"/status/sessions": {"MediaContainer": {"Metadata": [{"title": "Episode",
         "grandparentTitle": "A show", "type": "episode", "duration": 20000, "viewOffset": 5000,
         "User": {"title": "private user", "token": "private-user-token"},
         "Player": {"title": "Living room", "state": "playing"},
         "Session": {"id": "session-1", "bandwidth": 5000},
         "TranscodeSession": {"videoDecision": "transcode", "progress": 20}}]}},
         "/library/sections": {"MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "type": "movie"}]}}})
    if seerr:
        result.seerr = API({"/api/v1/request?take=30&skip=0&sort=modified": {"results": [
            {"id": 3, "status": 2, "type": "tv", "requestedBy": {"plexToken": "private-plex-token"},
             "media": {"tmdbId": 10, "status": 3}, "seasons": [{"seasonNumber": 1}]}]}})
    return result


class PlexTests(unittest.TestCase):
    def test_sessions_and_library_are_normalized_without_tokens_or_raw_user(self):
        result = adapter(seerr=True).read()
        self.assertTrue(result["available"])
        self.assertEqual(result["metrics"]["active_streams"], 1)
        self.assertEqual(result["metrics"]["transcodes"], 1)
        self.assertEqual(result["metrics"]["bandwidth_kbps"], 5000)
        self.assertEqual(result["details"]["streams"][0]["progress"], 25)
        self.assertEqual(result["details"]["requests"][0]["seasons"], [1])
        for forbidden in ("private-", "requestedBy", "User", "fixture-plex", "seerr.invalid"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_bad_provider_shape_is_unavailable(self):
        obj = adapter()
        obj.client.routes["/status/sessions"] = []
        result = obj.read()
        self.assertFalse(result["available"])
        self.assertTrue(result["details"]["degraded"])
        self.assertIn("unexpected", result["details"]["plex_error"])

    def test_seerr_failure_does_not_hide_live_plex_streams(self):
        obj = adapter(seerr=True)
        obj.seerr.routes["/api/v1/request?take=30&skip=0&sort=modified"] = IntegrationError("Unavailable")
        result = obj.read()
        self.assertTrue(result["available"])
        self.assertTrue(result["details"]["degraded"])
        self.assertTrue(result["details"]["plex_available"])
        self.assertEqual(len(result["details"]["streams"]), 1)

    def test_plex_failure_preserves_timestamped_cache_but_not_plex_controls(self):
        obj = adapter(controls=True, seerr=True)
        first = obj.read()
        obj.client.routes["/status/sessions"] = IntegrationError("Unavailable")
        result = obj.read()
        self.assertTrue(result["available"])
        self.assertTrue(result["details"]["degraded"])
        self.assertFalse(result["details"]["plex_available"])
        self.assertEqual(result["details"]["plex_observed_at"], first["details"]["plex_observed_at"])
        self.assertEqual(result["details"]["streams"], first["details"]["streams"])
        self.assertIn("seerr_request", result["capabilities"])
        self.assertNotIn("refresh_library", result["capabilities"])
        self.assertEqual(obj._libraries, set())

    def test_seerr_failure_retains_old_requests_without_granting_request_controls(self):
        obj = adapter(controls=True, seerr=True)
        first = obj.read()
        obj.seerr.routes["/api/v1/request?take=30&skip=0&sort=modified"] = IntegrationError("Unavailable")
        result = obj.read()
        self.assertEqual(result["details"]["requests"], first["details"]["requests"])
        self.assertEqual(result["details"]["seerr_observed_at"], first["details"]["seerr_observed_at"])
        self.assertNotIn("seerr_request", result["capabilities"])
        self.assertIn("refresh_library", result["capabilities"])

    def test_default_cannot_mutate_any_service(self):
        obj = adapter(seerr=True)
        obj.read()
        for index, action in enumerate(("refresh_library", "terminate_stream", "seerr_request", "seerr_approve")):
            result = obj.command(action, {}, f"request-{index:03}")
            self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(obj.client.calls), 2)
        self.assertEqual(len(obj.seerr.calls), 1)

    def test_library_refresh_requires_current_observed_id(self):
        obj = adapter(controls=True)
        self.assertEqual(obj.command("refresh_library", {"library_id": "1"}, "request-001")["status"], "rejected")
        obj.read()
        self.assertEqual(obj.command("refresh_library", {"library_id": "1"}, "request-002")["status"], "sent")
        self.assertEqual(obj.client.calls[-1][0], "/library/sections/1/refresh")
        self.assertEqual(obj.command("refresh_library", {"library_id": "../../bad"}, "request-003")["status"], "rejected")

    def test_failed_poll_clears_old_operator_targets(self):
        obj = adapter(controls=True)
        obj.read()
        obj.client.routes["/status/sessions"] = IntegrationError("offline")
        obj.read()
        self.assertEqual(obj.command("refresh_library", {"library_id": "1"}, "request-001")["status"], "rejected")

    def test_refresh_timeout_is_uncertain_and_never_repeated(self):
        obj = adapter(controls=True)
        obj.read()
        obj.client.routes["/library/sections/1/refresh"] = TransportError("timeout")
        self.assertEqual(obj.command("refresh_library", {"library_id": "1"}, "request-001")["status"], "uncertain")
        self.assertEqual(obj.command("refresh_library", {"library_id": "1"}, "request-001")["status"], "uncertain")
        self.assertEqual(sum(path.endswith("/refresh") for path, _ in obj.client.calls), 1)

    def test_stream_stop_requires_allowlist_current_id_and_confirmation(self):
        obj = adapter(controls=True)
        obj.read()
        parameters = {"session_id": "session-1", "confirm": "STOP"}
        self.assertEqual(obj.command("terminate_stream", parameters, "request-001")["status"], "rejected")
        obj.config["allow_terminate_streams"] = True
        self.assertEqual(obj.command("terminate_stream", {"session_id": "session-1"}, "request-002")["status"], "rejected")
        self.assertEqual(obj.command("terminate_stream", parameters, "request-003")["status"], "sent")
        self.assertIn("sessionId=session-1", obj.client.calls[-1][0])

    def test_search_query_is_encoded_and_results_do_not_copy_raw_fields(self):
        obj = adapter(seerr=True)
        obj.seerr.routes["/api/v1/search?query=A%26B&page=1"] = {"results": [
            {"id": 5, "mediaType": "movie", "title": "A&B", "secret": "do-not-return"}]}
        result = obj.command("seerr_search", {"query": "A&B"}, "request-001")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["data"]["results"][0]["title"], "A&B")
        self.assertNotIn("do-not-return", json.dumps(result))

    def test_tv_season_selection_is_loaded_and_bounded(self):
        obj = adapter(seerr=True)
        obj.seerr.routes["/api/v1/tv/5"] = {"name": "A show", "seasons": [
             {"seasonNumber": 1, "name": "Season one", "episodeCount": 8}]}
        result = obj.command("seerr_tv", {"media_id": 5}, "request-001")
        self.assertEqual(result["data"]["seasons"][0]["number"], 1)

    def test_tv_request_uses_selected_seasons_and_named_destination(self):
        obj = adapter(controls=True, seerr=True)
        obj.seerr.routes["/api/v1/request"] = {"id": 42, "status": 2, "requestedBy": {"plexToken": "do-not-return"}}
        parameters = {"media_id": 5, "media_type": "tv", "seasons": [1, 3], "destination": "ssd"}
        result = obj.command("seerr_request", parameters, "request-001")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(obj.seerr.calls[-1][1]["body"], {"mediaType": "tv", "mediaId": 5,
             "seasons": [1, 3], "rootFolder": "/movies", "serverId": 1, "profileId": 2})
        self.assertNotIn("do-not-return", json.dumps(result))

    def test_tv_request_rejects_whole_show_magic_unknown_roots_bools_and_duplicates(self):
        obj = adapter(controls=True, seerr=True)
        for index, changed in enumerate(({"seasons": "all"}, {"seasons": [True]}, {"seasons": [1, 1]},
                      {"destination": "../../private"}, {"rootFolder": "/etc"}, {"is4k": 1}, {"media_id": True})):
            parameters = {"media_id": 5, "media_type": "tv", "seasons": [1]}
            parameters.update(changed)
            self.assertEqual(obj.command("seerr_request", parameters, f"request-{index:03}")["status"], "rejected")
        self.assertEqual(obj.seerr.calls, [])

    def test_post_timeout_may_have_created_request(self):
        obj = adapter(controls=True, seerr=True)
        obj.seerr.routes["/api/v1/request"] = TransportError("network timeout")
        parameters = {"media_id": 5, "media_type": "movie"}
        self.assertEqual(obj.command("seerr_request", parameters, "request-001")["status"], "uncertain")
        self.assertEqual(obj.command("seerr_request", parameters, "request-001")["status"], "uncertain")
        self.assertEqual(len(obj.seerr.calls), 1)

    def test_seerr_approval_requires_explicit_confirmation(self):
        obj = adapter(controls=True, seerr=True)
        self.assertEqual(obj.command("seerr_approve", {"request_id": 3}, "request-001")["status"], "rejected")
        self.assertEqual(obj.command("seerr_approve", {"request_id": 3, "confirm": "APPROVE"}, "request-002")["status"], "confirmed")
        self.assertEqual(obj.seerr.calls[-1][0], "/api/v1/request/3/approve")


if __name__ == "__main__": unittest.main()
