"""The web UI: browse readings and manage the calibration.

``oiltank run`` serves it on a thread of its own (:mod:`oiltank.web.server`), and ``oiltank web``
serves it alone, for browsing a data directory without the service. The pages are plain ES
modules under ``static/``, with no build step.
"""
