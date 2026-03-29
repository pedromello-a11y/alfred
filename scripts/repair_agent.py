from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import textwrap
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic

ALLOWED_FILES = {
    "app/services/message_handler.py",
    "app/services/task_manager.py",
}
ARTIFACT_NAME = "alfred-eval-report"
DEFAULT_MODEL = os.getenv("REPAIR_MODEL", "claude-3-5-sonnet-latest")


def gh_api(url: str, method: str = "GET", data: dict | None = None, accept: str = "application/vnd.github+json") -> dict | list:
    token = os.environ["GITHUB_TOKEN"]
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "alfred-repair-agent",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        content_type = resp.headers.get("Content-Type", "")
        raw = resp.read()
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        return {"raw": raw}


def download_failure_report(repo: str, run_id: int, out_dir: Path) -> dict:
    artifacts = gh_api(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts")
    items = artifacts.get("artifacts", []) if isinstance(artifacts, dict) else []
    artifact = next((a for a in items if a.get("name") == ARTIFACT_NAME and not a.get("expired")), None)
    if artifact is None:
        raise RuntimeError(f"Artifact '{ARTIFACT_NAME}' not found for run {run_id}")

    archive_resp = gh_api(artifact["archive_download_url"], accept="application/vnd.github+json")
    raw = archive_resp["raw"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(out_dir)
    report_path = out_dir / "failure_report.json"
    if not report_path.exists():
        raise RuntimeError("failure_report.json not found inside artifact")
    return json.loads(report_path.read_text(encoding="utf-8"))


def create_issue(repo: str, title: str, body: str) -> None:
    gh_api(f"https://api.github.com/repos/{repo}/issues", method="POST", data={"title": title, "body": body})


def create_pr(repo: str, branch: str, title: str, body: str, draft: bool) -> None:
    gh_api(
        f"https://api.github.com/repos/{repo}/pulls",
        method="POST",
        data={
            "title": title,
            "head": branch,
            "base": "master",
            "body": body,
            "draft": draft,
        },
    )


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def configure_git(repo: str) -> None:
    git("config", "user.name", "alfred-repair-bot")
    git("config", "user.email", "alfred-repair-bot@users.noreply.github.com")
    token = os.environ["GITHUB_TOKEN"]
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    git("remote", "set-url", "origin", remote_url)


def build_prompt(report: dict, files_payload: dict[str, str]) -> str:
    failures = report.get("failed_tests", [])
    prompt = {
        "task": "Fix failing Alfred regression tests by editing only allowed files.",
        "rules": [
            "Do not change tests.",
            "Do not weaken behavior checks.",
            "Preserve system-backlog separation, note-vs-task separation, and canonical task titles.",
            "Pay special attention to short status updates, negation, aliases, and long context blocks.",
            "Return strict JSON only with keys: commit_message, pr_title, pr_body, files.",
            "files must be an object mapping file path to full replacement file content.",
        ],
        "failed_tests": failures,
        "allowed_files": sorted(files_payload.keys()),
        "current_files": files_payload,
    }
    return json.dumps(prompt, ensure_ascii=False)


def call_model(prompt: str) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=DEFAULT_MODEL,
        max_tokens=12000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    raw = "\n".join(text_parts).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Model did not return JSON: {raw[:500]}")
    return json.loads(raw[start : end + 1])


def write_files(files: dict[str, str]) -> list[str]:
    changed = []
    for path, content in files.items():
        if path not in ALLOWED_FILES:
            continue
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        old = file_path.read_text(encoding="utf-8") if file_path.exists() else None
        if old == content:
            continue
        file_path.write_text(content, encoding="utf-8")
        changed.append(path)
    return changed


def summarize_failures(report: dict) -> str:
    lines = []
    for item in report.get("failed_tests", [])[:10]:
        lines.append(f"- {item.get('name')} [{item.get('error_class')}]")
        if item.get("message"):
            lines.append(f"  - {item['message'][:200]}")
    return "\n".join(lines) or "- No parsed failures"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    args = parser.parse_args()

    workspace = Path(".repair_artifacts") / str(args.run_id)
    report = download_failure_report(args.repo, args.run_id, workspace)
    failures = report.get("failed_tests", [])
    if not failures:
        print("No failed tests found in report; nothing to do.")
        return 0

    suggested = []
    for failure in failures:
        suggested.extend(failure.get("suggested_files", []))
    target_files = sorted(ALLOWED_FILES.intersection(suggested or ALLOWED_FILES))
    files_payload = {path: Path(path).read_text(encoding="utf-8") for path in target_files}

    if not os.getenv("ANTHROPIC_API_KEY"):
        title = f"Auto-repair blocked: configure ANTHROPIC_API_KEY for failed eval run {args.run_id}"
        body = textwrap.dedent(
            f"""
            The Alfred eval workflow failed and the autonomous repair workflow was triggered, but `ANTHROPIC_API_KEY` is not configured in repository secrets.

            Run: `{args.run_id}`

            Failures:
            {summarize_failures(report)}
            """
        ).strip()
        create_issue(args.repo, title, body)
        print("Created issue because ANTHROPIC_API_KEY is missing.")
        return 0

    prompt = build_prompt(report, files_payload)
    proposal = call_model(prompt)
    proposed_files = proposal.get("files", {}) if isinstance(proposal, dict) else {}
    changed = write_files(proposed_files)
    if not changed:
        title = f"Auto-repair produced no patch for failed eval run {args.run_id}"
        body = textwrap.dedent(
            f"""
            The repair agent ran but did not produce a valid patch for allowed files.

            Failures:
            {summarize_failures(report)}
            """
        ).strip()
        create_issue(args.repo, title, body)
        print("Created issue because no valid patch was produced.")
        return 0

    configure_git(args.repo)
    branch = f"auto/repair-{args.run_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    git("checkout", "-b", branch)
    git("add", *changed)
    commit_message = proposal.get("commit_message") or f"fix: auto-repair Alfred eval failures from run {args.run_id}"
    git("commit", "-m", commit_message)
    git("push", "-u", "origin", branch)

    draft = True
    pr_title = proposal.get("pr_title") or f"Auto-repair for Alfred eval run {args.run_id}"
    pr_body = proposal.get("pr_body") or textwrap.dedent(
        f"""
        Automated repair attempt for failed Alfred eval run `{args.run_id}`.

        Failures:
        {summarize_failures(report)}
        """
    ).strip()
    create_pr(args.repo, branch, pr_title, pr_body, draft=draft)
    print(f"Created repair PR from branch {branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
