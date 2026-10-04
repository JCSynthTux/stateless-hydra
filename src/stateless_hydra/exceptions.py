"""Exception hierarchy for stateless-hydra."""


class StatelessHydraError(Exception):
    """Base class for all stateless-hydra errors."""


class ConfigError(StatelessHydraError):
    """Raised when configuration is missing, malformed, or invalid."""
