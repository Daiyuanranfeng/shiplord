"""Render the GitHub Actions release workflow for an application (client-side)."""

from __future__ import annotations

import json
import re

from . import __version__
from .manifest import ManifestError, validate_binary_path, validate_build_command

TOOLING_REPOSITORY = "QueryPlanner/shiplord"
WORKFLOW_PATH = ".github/workflows/shiplord-release.yml"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PLATFORMS = re.compile(r"linux/(amd64|arm64)(,linux/(amd64|arm64))?\Z")
# A native build runs on a hosted runner of the architecture the binary targets.
_RUNNERS = {"linux/amd64": "ubuntu-24.04", "linux/arm64": "ubuntu-24.04-arm"}

# Every action is pinned to a full commit; the comment names the release the commit belongs to.
ACTIONS = {
    "checkout": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
    "python": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
    "qemu": "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130 # v3",
    "buildx": "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f # v3",
    "login": "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9 # v3",
    "metadata": "docker/metadata-action@c299e40c65443455700f0fdfc63efafe5b349051 # v5",
    "build": "docker/build-push-action@ca052bb54ab0790a636c9b5f226502c73d547a25 # v5",
    "tailscale": "tailscale/github-action@99a8333667fff04fdd6cffb46dfd409f8329a7bf # v4.3.0",
    "upload-artifact": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2",
    "download-artifact": "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0",
}


class WorkflowError(ValueError):
    """The workflow cannot be generated for this manifest."""


def validate_platforms(runtime: str, platforms: str) -> None:
    """Reject a platform request the generated workflow cannot satisfy."""
    if not _PLATFORMS.fullmatch(platforms):
        raise WorkflowError("platforms must be linux/amd64, linux/arm64, or both separated by a comma")
    if runtime == "native" and "," in platforms:
        raise WorkflowError("a native workflow builds one binary; choose linux/amd64 or linux/arm64")


def native_build_settings(manifest: dict) -> tuple[str, str]:
    """The validated command and binary path a native workflow needs, or a refusal naming both."""
    github = manifest.get("github") or {}
    command, path = github.get("build_command"), github.get("binary_path")
    if not command or not path:
        raise WorkflowError("a native application needs the command that builds its binary and the path it writes: "
                            "pass --build-command and --binary-path, or add github.build_command and "
                            "github.binary_path to ship.toml")
    try:
        return validate_build_command(command), validate_binary_path(path)
    except ManifestError as error:
        raise WorkflowError(str(error)) from None


def _quality_job() -> str:
    return f"""  quality:
    if: ${{{{ !inputs.check_only }}}}
    permissions:
      contents: read
      id-token: write
    uses: ./.github/workflows/code-quality.yml

"""


def _docker_build_job(platforms: str) -> str:
    qemu = f"      - uses: {ACTIONS['qemu']}\n" if "arm64" in platforms else ""
    return f"""  build:
    needs: quality
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    permissions:
      contents: read
      packages: write
    outputs:
      digest: ${{{{ steps.build-push.outputs.digest }}}}
      image: ${{{{ steps.name.outputs.image }}}}
    steps:
      - uses: {ACTIONS['checkout']}
        with:
          persist-credentials: false
      - name: Image name
        id: name
        run: echo "image=ghcr.io/${{GITHUB_REPOSITORY,,}}" >> "$GITHUB_OUTPUT"
{qemu}      - uses: {ACTIONS['buildx']}
      - uses: {ACTIONS['login']}
        with:
          registry: ghcr.io
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}
      - name: Image metadata
        id: meta
        uses: {ACTIONS['metadata']}
        with:
          images: ${{{{ steps.name.outputs.image }}}}
          tags: type=sha,format=long
      - name: Build and push
        id: build-push
        uses: {ACTIONS['build']}
        with:
          context: .
          push: true
          platforms: {platforms}
          tags: ${{{{ steps.meta.outputs.tags }}}}
          labels: |
            ${{{{ steps.meta.outputs.labels }}}}
            org.opencontainers.image.revision=${{{{ github.sha }}}}
"""


def _native_build_job(command: str, path: str, runner: str) -> str:
    quote = json.dumps
    return f"""  build:
    # A native application runs its own checks; this workflow only builds the binary and deploys it.
    if: ${{{{ !inputs.check_only }}}}
    runs-on: {runner}
    timeout-minutes: 30
    permissions:
      contents: read
    steps:
      - uses: {ACTIONS['checkout']}
        with:
          persist-credentials: false
      - name: Build the binary
        env:
          BINARY_PATH: {quote(path)}
        run: |
          set -euo pipefail
          {command}
          test -s "$GITHUB_WORKSPACE/$BINARY_PATH"   # the build must write the binary where ship.toml says
      - name: Upload the binary
        uses: {ACTIONS['upload-artifact']}
        with:
          name: shiplord-binary
          path: {quote(path)}
          if-no-files-found: error
          retention-days: 1
"""


def _docker_deploy_step() -> str:
    return f"""      - name: Deploy
        if: ${{{{ !inputs.check_only }}}}
        env:
          IMAGE: ${{{{ needs.build.outputs.image }}}}
          DIGEST: ${{{{ needs.build.outputs.digest }}}}
          RUN_ID: ${{{{ github.run_id }}}}
          RUN_ATTEMPT: ${{{{ github.run_attempt }}}}
        run: |
          set -euo pipefail
          shiplord deploy --image "${{IMAGE}}@${{DIGEST}}" --commit "$GITHUB_SHA" \\
            --request-id "gh-${{RUN_ID}}-${{RUN_ATTEMPT}}" --require-public --json | tee "$RUNNER_TEMP/shiplord-result.json"
"""


def _native_download_step() -> str:
    return f"""      - name: Download the binary
        if: ${{{{ !inputs.check_only }}}}
        uses: {ACTIONS['download-artifact']}
        with:
          name: shiplord-binary
          path: ${{{{ runner.temp }}}}/shiplord-binary
"""


def _native_deploy_step(path: str) -> str:
    basename = path.rsplit("/", 1)[-1]
    return f"""      - name: Deploy
        if: ${{{{ !inputs.check_only }}}}
        env:
          BINARY: ${{{{ runner.temp }}}}/shiplord-binary/{basename}
          RUN_ID: ${{{{ github.run_id }}}}
          RUN_ATTEMPT: ${{{{ github.run_attempt }}}}
        run: |
          set -euo pipefail
          shiplord deploy --binary "$BINARY" --commit "$GITHUB_SHA" \\
            --request-id "gh-${{RUN_ID}}-${{RUN_ATTEMPT}}" --require-public --json | tee "$RUNNER_TEMP/shiplord-result.json"
"""


def _deploy_job(branch_ref: str, tooling_repository: str, tooling_ref: str, download: str, deploy_step: str) -> str:
    quote = json.dumps
    return f"""  deploy:
    needs: build
    # Runs after a successful build, or alone for a check-only dispatch (which skips the build).
    if: ${{{{ !cancelled() && github.ref == '{branch_ref}' && (inputs.check_only || needs.build.result == 'success') }}}}
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: {ACTIONS['checkout']}
        with:
          ref: ${{{{ github.sha }}}}
          persist-credentials: false
      - name: Verify the checked-out commit
        run: test "$(git rev-parse HEAD)" = "$GITHUB_SHA"
{download}      - uses: {ACTIONS['python']}
        with:
          python-version: "3.12"
      - name: Install the pinned Shiplord
        run: python -m pip install --disable-pip-version-check {quote(f"git+https://github.com/{tooling_repository}.git@{tooling_ref}")}
      - name: Join the tailnet
        uses: {ACTIONS['tailscale']}
        with:
          oauth-client-id: ${{{{ vars.SHIPLORD_TS_CLIENT_ID }}}}
          audience: ${{{{ vars.SHIPLORD_TS_AUDIENCE }}}}
          tags: ${{{{ vars.SHIPLORD_TS_TAGS || 'tag:ci' }}}}
          # A new node is not accepted by its peers immediately; wait until the server answers.
          ping: ${{{{ vars.SHIPLORD_SERVER_HOST }}}}
      - name: Prepare the SSH identity and pinned host keys
        env:
          SSH_PRIVATE_KEY: ${{{{ secrets.SHIPLORD_SSH_PRIVATE_KEY }}}}
          KNOWN_HOSTS: ${{{{ vars.SHIPLORD_SSH_KNOWN_HOSTS }}}}
          DEPLOY_USER: ${{{{ vars.SHIPLORD_DEPLOY_USER }}}}
          SERVER_HOST: ${{{{ vars.SHIPLORD_SERVER_HOST }}}}
          SSH_PORT: ${{{{ vars.SHIPLORD_SSH_PORT }}}}
        run: |
          set -euo pipefail
          umask 077
          printf '%s\\n' "$SSH_PRIVATE_KEY" > "$RUNNER_TEMP/shiplord_key"
          printf '%s\\n' "$KNOWN_HOSTS" > "$RUNNER_TEMP/shiplord_known_hosts"
          {{
            echo "SHIPLORD_SSH_IDENTITY=$RUNNER_TEMP/shiplord_key"
            echo "SHIPLORD_SSH_KNOWN_HOSTS_FILE=$RUNNER_TEMP/shiplord_known_hosts"
            echo "SHIPLORD_SSH_TARGET=${{DEPLOY_USER}}@${{SERVER_HOST}}"
            if [ -n "$SSH_PORT" ]; then echo "SHIPLORD_SSH_PORT=$SSH_PORT"; fi
          }} >> "$GITHUB_ENV"
      - name: Check access only
        if: ${{{{ inputs.check_only }}}}
        run: shiplord status --json
{deploy_step}      - name: Summary
        if: ${{{{ always() }}}}
        run: |
          python - <<'PY'
          import json, os
          path = os.path.join(os.environ["RUNNER_TEMP"], "shiplord-result.json")
          if os.path.exists(path):
              result = json.load(open(path))
              release = result.get("release") or {{}}
              lines = [f"- status: {{result.get('status')}}", f"- commit: {{release.get('commit')}}",
                       f"- public check: {{(result.get('public') or {{}}).get('ok')}}"]
              open(os.environ["GITHUB_STEP_SUMMARY"], "a").write("### Shiplord deployment\\n" + "\\n".join(lines) + "\\n")
          PY
      - name: Remove the SSH identity
        if: ${{{{ always() }}}}
        run: rm -f "$RUNNER_TEMP/shiplord_key" "$RUNNER_TEMP/shiplord_known_hosts" "$RUNNER_TEMP/shiplord-result.json"
"""


def render_workflow(manifest: dict, *, tooling_ref: str, tooling_repository: str = TOOLING_REPOSITORY,
                    platforms: str = "linux/amd64") -> str:
    """Return the workflow text. Deterministic: the same inputs always produce the same bytes."""
    if not _COMMIT.fullmatch(tooling_ref):
        raise WorkflowError("the tooling reference must be a full 40-character commit SHA")
    runtime = manifest["runtime"]
    if runtime not in ("docker", "native"):
        raise WorkflowError("runtime must be docker or native")
    validate_platforms(runtime, platforms)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", tooling_repository):
        raise WorkflowError("invalid tooling repository")
    if runtime == "native":
        command, path = native_build_settings(manifest)
        quality = ""
        build = _native_build_job(command, path, _RUNNERS[platforms])
        download = _native_download_step()
        deploy_step = _native_deploy_step(path)
    else:
        quality = _quality_job()
        build = _docker_build_job(platforms)
        download = ""
        deploy_step = _docker_deploy_step()
    branch = manifest.get("github", {}).get("deployment_branch", "main")
    app = manifest["name"]
    branch_ref = ("refs/heads/" + branch).replace("'", "''")
    quote = json.dumps
    return f"""# Generated by Shiplord {__version__}. `shiplord github setup` regenerates this file; review changes in a pull request.
name: Release

on:
  push:
    branches: [{quote(branch)}]
  workflow_dispatch:
    inputs:
      check_only:
        description: Check CI access only (Tailscale, SSH, helper); build and deploy nothing
        type: boolean
        default: false

permissions:
  contents: read

# Production releases run one at a time and are never cancelled half way.
concurrency:
  group: {quote("shiplord-release-" + app)}
  cancel-in-progress: false

jobs:
{quality}{build}
{_deploy_job(branch_ref, tooling_repository, tooling_ref, download, deploy_step)}"""
