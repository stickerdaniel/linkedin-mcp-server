"""Report uv's OSV advisories without treating vulnerabilities as scanner errors."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCOPES = {
    "runtime": ["--no-dev"],
    "full": [],
}


def _required_string(value: dict[str, Any], key: str) -> None:
    if not isinstance(value.get(key), str) or not value[key]:
        raise TypeError(f"{key} is not a non-empty string")


def _optional_string(value: dict[str, Any], key: str) -> None:
    if key not in value or (value[key] is not None and not isinstance(value[key], str)):
        raise TypeError(f"{key} is not a string or null")


def _string_array(value: dict[str, Any], key: str) -> None:
    items = value.get(key)
    if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
        raise TypeError(f"{key} is not an array of strings")


def _validate_vulnerability(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("vulnerability is not an object")
    dependency = value.get("dependency")
    if not isinstance(dependency, dict):
        raise TypeError("vulnerability dependency is not an object")
    _required_string(dependency, "name")
    _required_string(dependency, "version")
    _required_string(value, "id")
    _required_string(value, "display_id")
    _string_array(value, "aliases")
    _string_array(value, "fix_versions")
    for key in ("summary", "description", "link", "published", "modified"):
        _optional_string(value, key)


def _validate_adverse_status(value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError("adverse status is not an object")
    _required_string(value, "name")
    _required_string(value, "status")
    _optional_string(value, "reason")


def _classify(
    completed: subprocess.CompletedProcess[str],
) -> tuple[str, Any, str | None]:
    try:
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise TypeError("report is not an object")
        schema = payload["schema"]
        summary = payload["summary"]
        vulnerabilities = payload["vulnerabilities"]
        adverse_statuses = payload["adverse_statuses"]
        if not isinstance(schema, dict) or schema.get("version") != "preview":
            raise ValueError("unsupported schema version")
        if not isinstance(summary, dict):
            raise TypeError("summary is not an object")
        if not isinstance(vulnerabilities, list) or not isinstance(
            adverse_statuses, list
        ):
            raise TypeError("result collections are not arrays")
        for vulnerability in vulnerabilities:
            _validate_vulnerability(vulnerability)
        for adverse_status in adverse_statuses:
            _validate_adverse_status(adverse_status)
        counts = {
            name: summary[name]
            for name in ("audited_packages", "vulnerabilities", "adverse_statuses")
        }
        if any(type(value) is not int for value in counts.values()):
            raise TypeError("summary counts are not integers")
        if any(value < 0 for value in counts.values()):
            raise ValueError("summary counts are negative")
        if counts["vulnerabilities"] != len(vulnerabilities):
            raise ValueError("vulnerability count does not match its result array")
        if counts["adverse_statuses"] != len(adverse_statuses):
            raise ValueError("adverse status count does not match its result array")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return "scanner_error", completed.stdout, f"invalid scanner output: {exc}"

    expected_exit = 1 if vulnerabilities else 0
    if completed.returncode != expected_exit:
        return (
            "scanner_error",
            payload,
            f"unexpected scanner exit code {completed.returncode}; expected {expected_exit}",
        )
    if vulnerabilities:
        return "vulnerabilities", payload, None
    return "no_vulnerabilities", payload, None


def _write_summary(scope: str, state: str, report: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path is None:
        return

    scanner = report.get("scanner")
    counts = scanner.get("summary", {}) if isinstance(scanner, dict) else {}
    lines = [
        f"## uv audit: {scope}",
        "",
        f"**State:** `{state}`",
        "",
        f"- Audited packages: {counts.get('audited_packages', 'unavailable')}",
        f"- OSV vulnerability advisories: {counts.get('vulnerabilities', 'unavailable')}",
        "- PEP 792 project statuses: "
        f"{counts.get('adverse_statuses', 'unavailable')} (best effort; completeness is not guaranteed)",
        "- Full uv scanner output: workflow artifact",
    ]
    if report.get("error"):
        lines.extend(["", f"**Scanner error:** {report['error']}"])
    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def run(scope: str, output_dir: Path) -> int:
    command = [
        "uv",
        "--no-cache",
        "--preview-features",
        "audit-command,json-output",
        "audit",
        "--frozen",
        "--output-format",
        "json",
        *_SCOPES[scope],
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False, text=True)
        state, scanner, error = _classify(completed)
        scanner_exit_code: int | None = completed.returncode
        stderr = completed.stderr
    except OSError as exc:
        state = "scanner_error"
        scanner = ""
        error = f"could not start scanner: {exc}"
        scanner_exit_code = None
        stderr = ""

    report = {
        "scope": scope,
        "state": state,
        "command": command,
        "scanner_exit_code": scanner_exit_code,
        "scanner": scanner,
        "stderr": stderr,
        "error": error,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit-result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_summary(scope, state, report)
    print(f"uv audit {scope}: {state}")
    if error:
        print(error, file=sys.stderr)
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
    return 2 if state == "scanner_error" else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=sorted(_SCOPES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    return run(args.scope, args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
