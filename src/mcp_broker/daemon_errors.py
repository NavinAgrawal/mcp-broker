"""Daemon lifecycle exceptions."""


class BrokerDaemonError(Exception):
    """Raised when daemon lifecycle operations fail."""


class BrokerRequestTooLarge(BrokerDaemonError):
    """Raised when a socket request exceeds its configured byte limit."""
