#!/usr/bin/env python3
"""Step 2: run the detection+fix engine against every discovered file.
Computes the fixed content in memory only -- this step never writes to
the real repository. Writing only happens in open_pr.py, and only when
the caller explicitly asked for it (open_pr=true)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402


def main():
    discover_json = json.loads(sys.argv[1])
    files = discover_json.get("files", [])
    demo = discover_json.get("demo", False)
    repo_path = discover_json.get("repo_path")

    results = []
    total_issues = 0
    for path in files:
        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception as e:
            results.append({"path": path, "status": "SKIPPED",
                             "reason": f"could not read: {e}"})
            continue

        try:
            fixed_lines, findings = engine.fix_all(lines)
        except Exception as e:
            results.append({"path": path, "status": "SKIPPED",
                             "reason": f"could not analyze/fix ({e.__class__.__name__}: {e})"})
            continue

        if not findings:
            results.append({"path": path, "status": "CLEAR", "findings": []})
            continue

        total_issues += len(findings)
        results.append({
            "path": path,
            "status": "FOUND",
            "findings": [
                {"kind": f["kind"], "scope": f["scope"], "build_job": f["build_job"],
                 "deploy_job": f["deploy_job"], "image_repo": f["image_repo"]}
                for f in findings
            ],
            "fixed_content": "".join(fixed_lines),
        })

    out = {
        "ok": True,
        "demo": demo,
        "repo_path": repo_path,
        "total_files": len(files),
        "total_issues": total_issues,
        "results": results,
    }
    out["packed"] = {
        "total_issues": total_issues,
        "results": [
            {"path": r["path"], "status": r["status"],
             "findings": r.get("findings", []),
             "fixed_content": r.get("fixed_content")}
            for r in results
        ],
        "demo": demo,
        "repo_path": repo_path,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
