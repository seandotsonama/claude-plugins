"""Configuration for the document converter engine.

One engine serves both skills in this plugin. The difference is brand scope:

  * branded-doc-converter -- multi-brand. BRAND_FOLDER_ID is empty, so brands are
    discovered as the immediate subfolders of TEMPLATES_FOLDER_ID and chosen with
    --brand.
  * svs-convert -- pinned to SVS. Its SKILL.md always passes --brand SVS and never
    asks. Set BRAND_FOLDER_ID in the environment to hard-pin a deployment instead.

Every value below can be overridden by an environment variable of the same name.
"""

import os

# ---------------------------------------------------------------------------
# Brand scope
# ---------------------------------------------------------------------------

# Empty means multi-brand: brands are discovered at runtime and selected with
# --brand. Setting this to a brand folder ID pins the whole deployment to that
# one brand -- no discovery, no --brand argument. Leave it empty here; the
# svs-convert skill pins itself by always passing --brand SVS.
BRAND_FOLDER_ID = os.environ.get("BRAND_FOLDER_ID", "")

BRAND_NAME = os.environ.get("BRAND_NAME", "")

# The brand library. Its immediate subfolders are the brands, read at runtime so
# a new brand folder appears without editing this plugin. Point this at your own
# folder to use a different library.
TEMPLATES_FOLDER_ID = os.environ.get(
    "TEMPLATES_FOLDER_ID",
    "1qliBYoNaTuBbn70miaPrssnqE4o9Yy_S",
)

# ---------------------------------------------------------------------------
# Output destination (resolved per user at runtime)
# ---------------------------------------------------------------------------

# Default output is the authenticated user's OWN My Drive root plus a folder
# named DEFAULT_OUTPUT_FOLDER_NAME, created if absent. Because each team member
# runs this under their own OAuth token, "root" resolves to their My Drive, so
# everyone gets their own folder. DEFAULT_OUTPUT_FOLDER_ID is an opt-in override
# (empty by default) to pin output to one specific folder ID, e.g. a shared team
# folder.
DEFAULT_OUTPUT_FOLDER_ID = os.environ.get("DEFAULT_OUTPUT_FOLDER_ID", "")

DEFAULT_OUTPUT_FOLDER_NAME = os.environ.get(
    "DEFAULT_OUTPUT_FOLDER_NAME",
    "Converted Docs",
)

# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

# Reuse the same OAuth client the Workspace MCP uses. Most people authenticate
# once with:
#   gcloud auth application-default login \
#     --scopes=https://www.googleapis.com/auth/drive,\
# https://www.googleapis.com/auth/documents,openid
CLIENT_SECRET_PATH = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_SECRET",
    os.path.expanduser("~/.config/doc-converter/client_secret.json"),
)

TOKEN_PATH = os.environ.get(
    "GOOGLE_OAUTH_TOKEN",
    os.path.expanduser("~/.config/doc-converter/token.json"),
)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

# ---------------------------------------------------------------------------
# Engine tuning (leave as-is)
# ---------------------------------------------------------------------------

# Google Docs rejects very large batchUpdate payloads. Keep requests chunked.
MAX_REQUESTS_PER_BATCH = 400
MAX_CHARS_PER_INSERT = 90_000

# Word conversions often prefix headings with a vertical tab (\x0b) that renders
# as a stray blank line. Strip it and use pageBreakBefore where a page break was
# clearly intended.
STRIP_LEADING_VERTICAL_TAB = True

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Ordered-list glyphs. Google Docs offers only fixed presets and there is no API
# request to set glyphFormat, so a source using "(a)" / "(i)" cannot be matched
# exactly; the PARENS preset's "a)" / "i)" is the closest available. Levels are
# 1) then a) then i), which matches the decimal/alpha/roman ladder used by most
# agreements. Swap for NUMBERED_DECIMAL_ALPHA_ROMAN to get 1. a. i. instead.
ORDERED_LIST_PRESET = os.environ.get(
    "ORDERED_LIST_PRESET", "NUMBERED_DECIMAL_ALPHA_ROMAN_PARENS"
)
