from src.vfs_bot.vfs_bot import VfsBot


class UnsupportedCountryError(Exception):
    """Raised when a route has no configured URL."""


class _SchemaVfsBot(VfsBot):
    """
    Concrete VfsBot driven entirely by the route's JSON schema
    (config/routes/<SOURCE>-<DEST>.json, falling back to _default.json).

    There is no longer a Python subclass per destination country — the per-portal
    differences (which Step-2 fields to fill, whether OTP runs, whether a travel-
    insurance step exists) all come from the schema. Add a new portal by dropping
    in a JSON file and a URL in config/vfs_urls.ini; no code change required.
    """

    def __init__(self, source_country_code: str, destination_country_code: str):
        super().__init__()
        self.source_country_code = source_country_code
        self.destination_country_code = destination_country_code


def get_vfs_bot(source_country_code: str, destination_country_code: str) -> VfsBot:
    """Returns a schema-driven VfsBot for the given source/destination route.

    Args:
        source_country_code (str): ISO 3166-1 alpha-2 code you're applying from.
        destination_country_code (str): ISO 3166-1 alpha-2 code of the destination.

    Returns:
        VfsBot: A bot whose flow is driven by the route's JSON schema.
    """
    return _SchemaVfsBot(source_country_code, destination_country_code)
