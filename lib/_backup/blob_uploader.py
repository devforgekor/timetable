#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Azure Blob uploader — single shared utility for all pipeline outputs.

Generates a self-contained review-bundle.md from any pipeline result
and uploads to Azure Blob with a 7-day SAS URL.

Usage:
  from lib.blob_uploader import upload_review_bundle

  url = upload_review_bundle(
      content=final_report_markdown,
      pipeline="debate",       # debate | code_mod | extract
      session_id="20260525T...",
  )
  print(f"Review URL: {url}")
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions

ACCOUNT_NAME = "stshareddevforgeprodkrc"
CONTAINER = "devforge"

_account_key: Optional[str] = None


def _get_account_key() -> str:
    global _account_key
    if _account_key:
        return _account_key
    # Read from secrets file directly (avoids bash semicolon issues)
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    with open(secrets_path) as f:
        for line in f:
            if line.startswith("AZURE_STORAGE_ACCOUNT_KEY="):
                _account_key = line.strip().split("=", 1)[1].strip("'\"")
                break
    if not _account_key:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_KEY not found in secrets.env")
    return _account_key


def _upload_blob(blob_name: str, content: str) -> str:
    """Upload content to Azure Blob, return SAS URL with 7-day read permission."""
    account_key = _get_account_key()
    account_url = f"https://{ACCOUNT_NAME}.blob.core.windows.net"

    service = BlobServiceClient(account_url=account_url, credential=account_key)
    blob_client = service.get_blob_client(container=CONTAINER, blob=blob_name)
    blob_client.upload_blob(content.encode("utf-8"), overwrite=True)

    sas_token = generate_blob_sas(
        account_name=ACCOUNT_NAME,
        container_name=CONTAINER,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=7),
    )
    return f"https://{ACCOUNT_NAME}.blob.core.windows.net/{CONTAINER}/{blob_name}?{sas_token}"


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def upload_review_bundle(
    content: str,
    pipeline: str,
    session_id: str,
    metadata: Optional[dict] = None,
) -> str:
    """Wrap pipeline output as a review-ready markdown bundle and upload.

    Args:
        content: The final report / output in markdown format
        pipeline: "debate", "code_mod", or "extract"
        session_id: Unique session identifier
        metadata: Optional dict with keys like question, model, confidence, etc.

    Returns:
        SAS URL string (7-day expiry) for downloading the review bundle.
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    blob_name = f"{pipeline}/{date_str}/{session_id}/review-bundle.md"

    # Build self-contained review bundle
    bundle_lines = [
        f"# DevForge {pipeline.upper()} — Review Bundle",
        "",
        f"**Session:** `{session_id}`",
        f"**Generated:** {now.isoformat()}",
        f"**Pipeline:** {pipeline}",
    ]

    if metadata:
        for key, val in metadata.items():
            bundle_lines.append(f"**{key}:** {val}")

    bundle_lines.extend([
        "",
        "---",
        "",
        content,
        "",
        "---",
        "",
        "*End of review bundle. Submit this entire document to any AI for review.*",
    ])

    bundle = "\n".join(bundle_lines)
    return _upload_blob(blob_name, bundle)


def upload_raw(content: str, pipeline: str, session_id: str, filename: str) -> str:
    """Upload raw file (JSON, YAML, etc.) to the session directory.

    Returns:
        SAS URL string.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob_name = f"{pipeline}/{date_str}/{session_id}/{filename}"
    return _upload_blob(blob_name, content)

