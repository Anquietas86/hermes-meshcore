class Platform(str):
    """Stand-in: Hermes' Platform enum accepts registered plugin names."""

    def __new__(cls, value):
        return str.__new__(cls, value)

    @property
    def value(self):
        return str(self)


class PlatformConfig:
    def __init__(self, extra=None, enabled=True):
        self.extra = dict(extra or {})
        self.enabled = enabled
        self.token = None
        self.api_key = None
