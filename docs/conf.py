"""Sphinx configuration for the TSUMGI documentation (published on GitHub Pages).

Prose is Markdown (MyST). The site is built from docs/ only; pages that link to
files outside docs/ (source, tests) keep those links as plain text on the site.
"""
import os

project = "TSUMGI"
author = "TSUMGI contributors"
copyright = "TSUMGI contributors"
release = os.environ.get("TSUMGI_VERSION", "0.3.0")

extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build", "conf.py"]

myst_heading_anchors = 4
myst_enable_extensions = ["colon_fence"]

html_theme = "furo"
html_title = "TSUMGI"
