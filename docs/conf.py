"""Sphinx configuration.  Pages are Markdown, parsed by MyST."""

from agentboss import __version__

project = "agentboss"
author = "Joe Drumgoole"
copyright = "2026, Joe Drumgoole"
release = __version__

extensions = ["myst_parser"]
myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

source_suffix = {".md": "markdown"}
exclude_patterns = ["_build"]

html_theme = "furo"
html_title = "agentboss"
html_static_path = ["_static"]
