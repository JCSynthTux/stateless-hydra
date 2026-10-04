"""``python -m stateless_hydra`` entry point (development convenience).

The production image runs ``uvicorn stateless_hydra.main:app`` directly; this
module exists so the app can also be started via ``python -m stateless_hydra``,
reading host/port/log level from configuration.
"""

from __future__ import annotations

import uvicorn

from .config import load_settings, setup_logging


def main() -> None:
    """Load settings and run the ASGI app with uvicorn."""
    settings = load_settings(None)
    setup_logging(settings)
    uvicorn.run(
        "stateless_hydra.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
