"""stateless-hydra: stateless, Kubernetes-native Newznab/Torznab proxy."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stateless-hydra")
except PackageNotFoundError:  # running from source tree without install metadata
    __version__ = "0.0.0-dev"
