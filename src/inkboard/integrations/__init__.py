"""Built-in integrations. Unselected providers are never imported or started."""
from .common import IntegrationError


def create_adapter(config, secrets):
    kind = config.get("type")
    if kind == "bambu":
        from .bambu import BambuAdapter
        return BambuAdapter(config, secrets)
    if kind == "plex":
        from .plex import PlexAdapter
        return PlexAdapter(config, secrets)
    if kind == "http_status":
        from .http_status import HTTPStatusAdapter
        return HTTPStatusAdapter(config, secrets)
    raise IntegrationError("Unknown integration type. Select bambu, plex or http_status.")
