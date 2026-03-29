from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CLASS_RULES = [
    (re.compile(r"negation", re.I), "NEGATION_MISREAD", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"alias|canonical", re.I), "ALIAS_NOT_CANONICALIZED", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"backlog|system", re.I), "SYSTEM_BACKLOG_LEAK", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"note|pollution|garbage", re.I), "NOTE_PROMOTED_TO_TASK", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"duplicate", re.I), "DUPLICATE_TASK", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"hallucinate|open_activities|active_work_view", re.I), "ACTIVE_LIST_HALLUCINATION", ["app/services/message_handler.py", "app/services/task_manager.py"]),
    (re.compile(r"reset", re.I), "RESET_DID_NOT_CLEAR_STATE", ["app/services/message_handler.py", "app/services/task_manager.py"]),
]


def classify_failure(name: str, message: str) -> tuple[str, list[str]]:
    haystack = f"{name}\n{message}"
    for pattern, label, files in CLASS_RULES:
        if pattern.search(haystack):
            return label, files
    return "UNCLASSIFIED", ["app/services/message_handler.py", "app/services/task_manager.py"]


def parse_junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    failed_tests: list[dict] = []
    summary = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}

    for suite in suites:
        for key in summary:
            summary[key] += int(float(suite.attrib.get(key, 0) or 0))

        for testcase in suite.findall("testcase"):
            failure = testcase.find("failure")
            error = testcase.find("error")
            node = failure or error
            if node is None:
                continue
            message = (node.attrib.get("message") or "").strip()
            text = (node.text or "").strip()
            error_class, suggested_files = classify_failure(testcase.attrib.get("name", ""), f"{message}\n{text}")
            failed_tests.append(
                {
                    "name": testcase.attrib.get("name"),
                    "classname": testcase.attrib.get("classname"),
                    "file": testcase.attrib.get("file"),
                    "time": testcase.attrib.get("time"),
                    "failure_type": node.tag,
                    "message": message[:500],
                    "details": text[:4000],
                    "error_class": error_class,
                    "suggested_files": suggested_files,
                }
            )

    return {"summary": summary, "failed_tests": failed_tests}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junitxml", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stdout", required=False)
    parser.add_argument("--repo", default="")
    args = parser.parse_args()

    junit_path = Path(args.junitxml)
    output_path = Path(args.output)
    if not junit_path.exists():
        print(f"JUnit report not found: {junit_path}", file=sys.stderr)
        return 2

    payload = parse_junit(junit_path)
    payload["repo"] = args.repo
    if args.stdout:
        stdout_path = Path(args.stdout)
        payload["pytest_output_tail"] = stdout_path.read_text(encoding="utf-8", errors="ignore")[-12000:] if stdout_path.exists() else ""
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote failure report to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
