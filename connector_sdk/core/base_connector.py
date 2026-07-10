from abc import ABC, abstractmethod


class BaseConnector(ABC):
    """
    The contract every connector (API or scraper) must follow, so the future
    dispatcher can run any of them the same way: create it, then call
    discover() -> fetch() -> normalize() -> validate() in that order.
    """

    @abstractmethod
    def discover(self, city):
        """Return a list of raw events for the given city."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, raw_event):
        """Return full detail for one raw event.
        If discover() already returns full detail, just return raw_event unchanged."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_event):
        """Reshape one raw event into the platform's common event format."""
        raise NotImplementedError

    @abstractmethod
    def validate(self, normalized_event):
        """Return (is_valid, missing_fields) for one normalized event."""
        raise NotImplementedError
