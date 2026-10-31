"""The local web interface: a dashboard served by the daemon at /ui/."""

from projectmind.web.api import WebApi, static_file

__all__ = ["WebApi", "static_file"]
