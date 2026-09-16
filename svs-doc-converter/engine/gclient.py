"""Authentication and Drive helpers."""

import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config


def get_credentials():
    """Resolve credentials, preferring whatever already exists.

    Order:
      1. Application Default Credentials (gcloud auth application-default login)
      2. A cached token from a previous run of this skill
      3. An OAuth client secret, prompting a browser once

    Most people land on (1) and never touch a client secret file.
    """
    # 1. Application default credentials
    try:
        import google.auth

        creds, _ = google.auth.default(scopes=config.SCOPES)
        if creds:
            return creds
    except Exception:
        pass

    # 2. Cached token from a previous run
    creds = None
    if os.path.exists(config.TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(config.TOKEN_PATH, config.SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return creds

    # 3. Interactive OAuth against a desktop client
    if not os.path.exists(config.CLIENT_SECRET_PATH):
        raise SystemExit(
            "No Google credentials found.\n\n"
            "Easiest fix, one command:\n\n"
            "  gcloud auth application-default login \\\n"
            "    --scopes=https://www.googleapis.com/auth/drive,"
            "https://www.googleapis.com/auth/documents,openid\n\n"
            "If gcloud is not installed:  brew install --cask google-cloud-sdk\n\n"
            "Alternatively, set GOOGLE_OAUTH_CLIENT_SECRET to a desktop-type "
            "OAuth client JSON."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        config.CLIENT_SECRET_PATH, config.SCOPES
    )
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(config.TOKEN_PATH), exist_ok=True)
    with open(config.TOKEN_PATH, "w") as fh:
        fh.write(creds.to_json())

    return creds


def get_services():
    creds = get_credentials()
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return docs, drive


# ----------------------------------------------------------------------------
# Drive helpers
# ----------------------------------------------------------------------------

def list_children(drive, folder_id, mime_type=None, page_size=200):
    """Return immediate children of a folder."""
    query = f"'{folder_id}' in parents and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"

    results, token = [], None
    while True:
        resp = (
            drive.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                pageSize=page_size,
                pageToken=token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return results


def discover_brands(drive, templates_folder_id):
    """Each immediate subfolder of the template library is one brand."""
    folders = list_children(drive, templates_folder_id, config.FOLDER_MIME)
    return sorted(
        (f for f in folders if not f["name"].startswith(".")),
        key=lambda f: f["name"].lower(),
    )


def list_brand_templates(drive, brand_folder_id):
    """Every native Google Doc in a brand folder, sorted by name."""
    docs = list_children(drive, brand_folder_id, config.GOOGLE_DOC_MIME)
    return sorted(docs, key=lambda d: d["name"].lower())


def find_brand_template(drive, brand_folder_id, template_ref=None):
    """Return the master template Google Doc inside a brand folder.

    A brand folder may legitimately hold more than one template (SVS carries
    both a letterhead and a logo-report master). Never guess by name pattern or
    recency: the MCP formatter learned the hard way that a document going out
    under the wrong template reaches a counterparty. Resolve deterministically,
    or stop and make the caller choose.

    template_ref, when given, is matched first by exact file ID, then by a
    unique case-insensitive substring of the document name.
    """
    docs = list_brand_templates(drive, brand_folder_id)
    if not docs:
        raise SystemExit(
            "No Google Doc template found in the selected brand folder. "
            "Each brand folder must contain its master template as a native "
            "Google Doc."
        )

    if template_ref:
        exact = [d for d in docs if d["id"] == template_ref]
        if exact:
            return exact[0]
        ref = template_ref.lower()
        subs = [d for d in docs if ref in d["name"].lower()]
        if len(subs) == 1:
            return subs[0]
        listing = "\n".join(
            f"  - {d['name']}  (id {d['id']})" for d in docs
        )
        if len(subs) > 1:
            raise SystemExit(
                f"--template '{template_ref}' matches more than one document. "
                f"Pass a full ID.\nAvailable templates:\n{listing}"
            )
        raise SystemExit(
            f"--template '{template_ref}' matched no document in this brand "
            f"folder.\nAvailable templates:\n{listing}"
        )

    if len(docs) == 1:
        return docs[0]

    listing = "\n".join(
        f"  - {d['name']}  (id {d['id']}, modified {d.get('modifiedTime', '?')})"
        for d in docs
    )
    raise SystemExit(
        f"This brand folder holds {len(docs)} templates. Choose one with "
        f"--template <name-or-id>:\n{listing}"
    )


def ensure_folder(drive, name, parent_id="root"):
    """Find or create a folder by name under a parent, per authenticated user.

    When parent_id is "root" (the default output case), the lookup is scoped to
    the user's own My Drive: it resolves the real root folder ID, matches only
    folders whose parent is that root, and ignores same-named folders that live
    in shared drives or nested elsewhere. The result is that each user who runs
    this gets a "Converted Docs" folder at the top of their own My Drive, never
    someone else's and never a shared-drive folder that happens to share the
    name.
    """
    if parent_id == "root":
        root_meta = (
            drive.files()
            .get(fileId="root", fields="id", supportsAllDrives=True)
            .execute()
        )
        parent_id = root_meta["id"]

    safe = name.replace("'", "\\'")
    query = (
        f"name = '{safe}' and mimeType = '{config.FOLDER_MIME}' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = (
        drive.files()
        .list(
            q=query,
            fields="files(id, name)",
            pageSize=10,
            corpora="user",
            includeItemsFromAllDrives=False,
            supportsAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files", [])
    if files:
        return files[0]["id"]

    created = (
        drive.files()
        .create(
            body={"name": name, "mimeType": config.FOLDER_MIME, "parents": [parent_id]},
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    return created["id"]


def copy_file(drive, file_id, new_name, parent_id, convert_to_doc=False):
    body = {"name": new_name, "parents": [parent_id]}
    if convert_to_doc:
        body["mimeType"] = config.GOOGLE_DOC_MIME
    return (
        drive.files()
        .copy(fileId=file_id, body=body, fields="id, name, mimeType",
              supportsAllDrives=True)
        .execute()
    )


def get_file(drive, file_id):
    return (
        drive.files()
        .get(fileId=file_id, fields="id, name, mimeType, parents",
             supportsAllDrives=True)
        .execute()
    )


def upload_local_file(drive, path, parent_id, convert_to_doc=True):
    """Upload a local file, optionally converting to a native Google Doc."""
    from googleapiclient.http import MediaFileUpload
    import mimetypes

    name = os.path.splitext(os.path.basename(path))[0]
    guessed = mimetypes.guess_type(path)[0] or "application/octet-stream"

    body = {"name": name, "parents": [parent_id]}
    if convert_to_doc:
        body["mimeType"] = config.GOOGLE_DOC_MIME

    media = MediaFileUpload(path, mimetype=guessed, resumable=False)
    return (
        drive.files()
        .create(body=body, media_body=media, fields="id, name, mimeType",
                supportsAllDrives=True)
        .execute()
    )


def folder_path(drive, folder_id):
    """Human-readable path for reporting."""
    if folder_id in ("root", None):
        return "My Drive"
    parts = []
    current = folder_id
    for _ in range(10):
        meta = (
            drive.files()
            .get(fileId=current, fields="id, name, parents", supportsAllDrives=True)
            .execute()
        )
        parts.append(meta["name"])
        parents = meta.get("parents")
        if not parents:
            break
        current = parents[0]
    return "My Drive / " + " / ".join(reversed(parts))
