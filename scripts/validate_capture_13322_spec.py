"""Validate the capture specification against its unchanged production baseline.

The acceptance tests use ordinary assertions. This temporary fork-only gate
expects four specific baseline failures; a green gate is not implementation
success. Production changes must follow the hosted specification receipt.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import xml.etree.ElementTree as ET


BASE_COMMIT = "ad9671f3808cd45115687ada6b2bc2bcda17ecb5"
SOURCE_TREE = "d06b4e1b00df725a335311cb90af2794816c5eca"
PUBLIC_FILES = {
    "testing/test_capture.py",
    ".github/capture-13322-spec.json",
    ".github/workflows/capture-13322-spec.yml",
    "scripts/validate_capture_13322_spec.py",
}
REQUIREMENTS = {"R1", "R2", "R3", "R4", "R5", "R7", "R9"}
ACCEPTANCE = {"AE1", "AE2", "AE3", "AE4", "AE5"}
BASELINE_FAILURES = {
    f"testing/test_capture.py::{name}[{method}]": message
    for name, message in (
        (
            "TestCaptureManager::test_suspend_in_preserves_out_err_suspended_state",
            "AssertionError: disabled context restarted suspended capture",
        ),
        (
            "test_live_logging_preserves_suspended_capture",
            "AssertionError: live logging restarted suspended capture",
        ),
    )
    for method in ("sys", "fd")
}
CONTROL_CASES = {
    "AE3": {
        "testing/test_capture.py::TestCaptureManager::"
        f"test_suspend_in_restores_out_err_started_state[{method}]"
        for method in ("sys", "fd")
    },
    "AE4": {
        f"testing/test_capture.py::TestCaptureManager::{name}[{state}-{method}]"
        for name, states in (
            (
                "test_suspend_in_restores_independent_output_states",
                ("out-err", "out-only", "err-only", "neither"),
            ),
            (
                "test_suspend_in_with_missing_streams",
                ("no-stdout", "no-stderr", "no-output", "no-stdin"),
            ),
        )
        for state in states
        for method in ("sys", "fd")
    },
    "AE5": {
        "testing/test_capture.py::TestCaptureManager::"
        f"test_suspend_in_snapshot_consumed_before_ordinary_resume[{method}]"
        for method in ("sys", "fd")
    },
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, encoding="utf-8"
    ).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_contract(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    require(contract["schema_version"] == 1, "unsupported contract schema")
    require(contract["base_commit"] == BASE_COMMIT, "wrong original base commit")
    require(contract["source_tree"] == SOURCE_TREE, "wrong original source tree")
    require(set(contract["requirements"]) == REQUIREMENTS, "requirement map mismatch")
    require(set(contract["acceptance"]) == ACCEPTANCE, "acceptance map mismatch")
    cases: dict[str, dict[str, Any]] = contract["cases"]
    require(isinstance(cases, dict) and bool(cases), "missing expected cases")
    failed = {}
    for nodeid, expected in cases.items():
        require(
            nodeid.startswith("testing/test_capture.py::")
            and expected["outcome"] in {"passed", "failed"},
            f"invalid expected case: {nodeid}",
        )
        if expected["outcome"] == "failed":
            failed[nodeid] = expected["failure_message"]
        else:
            require(set(expected) == {"outcome"}, f"invalid passing case: {nodeid}")
    require(failed == BASELINE_FAILURES, "baseline failure catalog mismatch")
    require(
        cases.keys() == BASELINE_FAILURES.keys() | set().union(*CONTROL_CASES.values()),
        "expected case catalog mismatch",
    )
    covered = set()
    for acceptance, evidence in contract["acceptance"].items():
        nodes = evidence["cases"]
        require(
            isinstance(evidence["statement"], str) and bool(evidence["statement"]),
            f"missing acceptance statement: {acceptance}",
        )
        require(
            isinstance(nodes, list)
            and bool(nodes)
            and len(nodes) == len(set(nodes))
            and set(nodes) <= cases.keys(),
            f"invalid acceptance cases: {acceptance}",
        )
        covered.update(nodes)
    require(covered == cases.keys(), "expected cases lack acceptance ownership")
    for requirement, evidence in contract["requirements"].items():
        require(
            isinstance(evidence["statement"], str) and bool(evidence["statement"]),
            f"missing statement: {requirement}",
        )
        require(
            set(evidence["acceptance"]) <= ACCEPTANCE,
            f"unknown acceptance reference: {requirement}",
        )
        proof = evidence["review_proof"]
        require(
            all(
                isinstance(proof[field], str) and bool(proof[field])
                for field in ("owner", "criteria", "stage")
            ),
            f"missing non-runtime proof ownership: {requirement}",
        )
    require(
        set(contract["requirements"]["R1"]["acceptance"])
        == {"AE1", "AE2", "AE4", "AE5"},
        "R1 acceptance mismatch",
    )
    require(
        set(contract["requirements"]["R2"]["acceptance"]) == {"AE3", "AE4", "AE5"},
        "R2 acceptance mismatch",
    )
    require(
        set(contract["acceptance"]["AE1"]["cases"])
        == {node for node in BASELINE_FAILURES if "TestCaptureManager::" in node},
        "AE1 regression ownership mismatch",
    )
    require(
        set(contract["acceptance"]["AE2"]["cases"])
        == {node for node in BASELINE_FAILURES if "test_live_logging" in node},
        "AE2 regression ownership mismatch",
    )
    for acceptance, controls in CONTROL_CASES.items():
        require(
            set(contract["acceptance"][acceptance]["cases"]) == controls,
            f"passing control ownership mismatch: {acceptance}",
        )
    return cases


def validate_source(root: Path, contract: dict[str, Any]) -> dict[str, str]:
    require(git(root, "rev-parse", f"{BASE_COMMIT}:src") == SOURCE_TREE, "base missing")
    require(
        git(root, "rev-parse", "HEAD:src") == SOURCE_TREE, "committed source changed"
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"],
        cwd=root,
        check=True,
    )
    require(not git(root, "diff", BASE_COMMIT, "--", "src"), "working source changed")
    changed = set(git(root, "diff", "--name-only", BASE_COMMIT).splitlines())
    untracked = set(
        git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    )
    require(
        not (changed | untracked) - PUBLIC_FILES, "unexpected specification-stage files"
    )
    require(
        sha256(root / "testing/test_capture.py") == contract["test_file_sha256"],
        "accepted test content hash changed",
    )
    # Verify the interpreter executes this checkout, including pytester children.
    import _pytest.capture
    import pytest

    for module, relative in (
        (pytest, "src/pytest/__init__.py"),
        (_pytest.capture, "src/_pytest/capture.py"),
    ):
        assert module.__file__ is not None
        require(
            Path(module.__file__).resolve() == (root / relative).resolve(),
            f"interpreter imported a different checkout: {relative}",
        )
    return {path: sha256(root / path) for path in sorted(PUBLIC_FILES)}


def run_pytest(
    root: Path, artifacts: Path, name: str, args: list[str]
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PY_COLORS": "0",
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytester",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--color=no",
        *args,
    ]
    result = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, check=False
    )
    (artifacts / f"{name}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (artifacts / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result


def validate_junit(path: Path, cases: dict[str, dict[str, Any]]) -> dict[str, str]:
    report = ET.parse(path).getroot()
    require(report.tag == "testsuites", "unexpected JUnit root")
    suites = report.findall("testsuite")
    require(len(suites) == 1, "unexpected JUnit suite count")
    suite = suites[0]
    failures = len(BASELINE_FAILURES)
    for key, expected_count in (
        ("tests", len(cases)),
        ("failures", failures),
        ("errors", 0),
        ("skipped", 0),
    ):
        require(int(suite.attrib[key]) == expected_count, f"unexpected JUnit {key}")
    expected_ids = {}
    for nodeid in cases:
        file, *classes, name = nodeid.split("::")
        classname = ".".join([file.removesuffix(".py").replace("/", "."), *classes])
        expected_ids[(classname, name)] = nodeid
    observed = {}
    for case in suite.findall("testcase"):
        identity = (case.attrib["classname"], case.attrib["name"])
        require(identity in expected_ids, f"unexpected JUnit case: {identity}")
        nodeid = expected_ids[identity]
        require(nodeid not in observed, f"duplicate JUnit case: {nodeid}")
        require(not case.findall("error"), f"unexpected error: {nodeid}")
        require(not case.findall("skipped"), f"unexpected skip: {nodeid}")
        failed = case.findall("failure")
        expected = cases[nodeid]
        if expected["outcome"] == "failed":
            require(len(failed) == 1, f"regression unexpectedly passed: {nodeid}")
            message = failed[0].get("message", "").splitlines()
            require(
                bool(message) and message[0] == expected["failure_message"],
                f"wrong failure reason: {nodeid}: {message}",
            )
        else:
            require(not failed, f"passing control regressed: {nodeid}")
        observed[nodeid] = expected["outcome"]
    require(observed.keys() == cases.keys(), "missing JUnit cases")
    return observed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = args.artifacts.resolve()
    require(not artifacts.is_relative_to(root), "keep evidence outside the checkout")
    artifacts.mkdir(parents=True, exist_ok=True)
    require(not any(artifacts.iterdir()), "artifact directory must be empty")
    contract_path = root / ".github/capture-13322-spec.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    cases = validate_contract(contract)
    hashes = validate_source(root, contract)
    selectors = sorted({nodeid.split("[", 1)[0] for nodeid in cases})
    collection = run_pytest(
        root, artifacts, "collection", ["--collect-only", "-q", *selectors]
    )
    require(collection.returncode == 0, "collection failed")
    collected = [
        line.strip()
        for line in collection.stdout.splitlines()
        if line.startswith("testing/test_capture.py::")
    ]
    require(
        Counter(collected) == Counter(cases.keys()), "collection differs from contract"
    )
    junit = artifacts / "acceptance.xml"
    execution = run_pytest(
        root,
        artifacts,
        "acceptance",
        [
            "-q",
            "--tb=short",
            "-o",
            "junit_family=xunit2",
            f"--junitxml={junit}",
            *selectors,
        ],
    )
    require(
        execution.returncode == 1, "expected exactly the baseline test-failure exit"
    )
    outcomes = validate_junit(junit, cases)
    require(
        validate_source(root, contract) == hashes, "files changed during validation"
    )
    head = git(root, "rev-parse", "HEAD")
    if os.environ.get("GITHUB_SHA"):
        require(head == os.environ["GITHUB_SHA"], "hosted commit identity mismatch")
    receipt = {
        "result": "specification-valid; intentional baseline failures, not implementation success",
        "commit": head,
        "base_commit": BASE_COMMIT,
        "source_tree": SOURCE_TREE,
        "file_sha256": hashes,
        "outcomes": outcomes,
    }
    if os.environ.get("GITHUB_RUN_ID"):
        receipt["run_url"] = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    (artifacts / "receipt.json").write_text(rendered, encoding="utf-8")
    print(rendered)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open(
            "a", encoding="utf-8"
        ) as summary:
            summary.write(
                "Capture specification gate: intentional baseline failures verified.\n\n"
            )
            summary.write(
                "This does not certify an implementation.\n\n```json\n"
                + rendered
                + "```\n"
            )


if __name__ == "__main__":
    main()
