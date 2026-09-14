#!/usr/bin/env python3
"""Publish recipients.json to the RECIPIENTS_JSON Secret Manager secret.

Day-to-day workflow for adding/removing/pausing a club member: edit
recipients.json, then run this script to push the new version to Secret
Manager. The Cloud Function picks it up on its next cold start - no
redeploy needed (see DEPLOYMENT.md step 1 / CLAUDE.md "Sharing sessions
with a training club").

One-time GCP setup - granting the Cloud Function's service account access
to read the secret, and including it in --set-secrets on first deploy - is
still a manual step (DEPLOYMENT.md steps 3-4). This script only handles the
recurring "publish a new version" operation, and will tell you if it just
created the secret for the first time so you don't forget that setup.

Shells out to the gcloud CLI rather than adding the Secret Manager Python
client as a dependency for one script. On Windows, targets gcloud.cmd
explicitly (see _gcloud_executable) to sidestep a real issue hit locally:
typing a bare "gcloud" in PowerShell can resolve to gcloud.ps1, which then
fails if the shell's script-execution policy blocks it. subprocess.run here
never goes through PowerShell at all, so this bypasses that entirely.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from planiteam_to_coros import parse_recipients

SECRET_NAME = "RECIPIENTS_JSON"


def _gcloud_executable() -> str:
    if platform.system() == "Windows":
        exe = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    else:
        exe = shutil.which("gcloud")
    if not exe:
        raise FileNotFoundError("gcloud CLI not found on PATH - install the Google Cloud SDK first.")
    return exe


def _secret_exists(gcloud: str, secret_name: str, project: Optional[str]) -> bool:
    cmd = [gcloud, "secrets", "describe", secret_name, "--format=value(name)"]
    if project:
        cmd.append(f"--project={project}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def publish_recipients(path: Path, *, project: Optional[str] = None, secret_name: str = SECRET_NAME) -> int:
    raw = path.read_text(encoding="utf-8")
    # Fail loudly on malformed JSON/a missing field *before* touching GCP -
    # same validation main.py/push_to_recipients would hit at request time,
    # just surfaced here instead of on the next real session push.
    recipients = parse_recipients(raw)
    enabled = [r.name for r in recipients if r.enabled]
    paused = [r.name for r in recipients if not r.enabled]

    print(f"{path}: {len(recipients)} recipient(s) - {len(enabled)} enabled, {len(paused)} paused.")
    for name in enabled:
        print(f"  enabled: {name}")
    for name in paused:
        print(f"  paused:  {name}")

    gcloud = _gcloud_executable()
    first_time = not _secret_exists(gcloud, secret_name, project)

    cmd: List[str] = [gcloud, "secrets"]
    cmd += ["create", secret_name, "--data-file=-"] if first_time else ["versions", "add", secret_name, "--data-file=-"]
    if project:
        cmd.append(f"--project={project}")

    print(f"\nRunning: {' '.join(cmd)}")
    result = subprocess.run(cmd, input=raw, text=True)
    if result.returncode != 0:
        return result.returncode

    if first_time:
        print(
            f"\nCreated {secret_name} for the first time. Before it's usable, grant the Cloud "
            "Function's service account access to it (DEPLOYMENT.md step 3) and include it in "
            "--set-secrets on the next deploy (step 4)."
        )
    else:
        print(
            f"\nPublished a new version of {secret_name}. Takes effect on the Cloud Function's "
            "next cold start - no redeploy needed (see DEPLOYMENT.md step 1)."
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Publish recipients.json to the RECIPIENTS_JSON Secret Manager secret.")
    parser.add_argument("--file", type=Path, default=Path("recipients.json"), help="Path to the local recipients JSON file (default: %(default)s).")
    parser.add_argument("--project", type=str, default=None, help="GCP project id (default: gcloud's currently configured project).")
    args = parser.parse_args(argv)

    if not args.file.exists():
        parser.error(f"{args.file} not found - see DEPLOYMENT.md step 1 for the expected JSON shape.")

    try:
        return publish_recipients(args.file, project=args.project)
    except ValueError as exc:  # malformed recipients.json
        parser.error(str(exc))
    except FileNotFoundError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
