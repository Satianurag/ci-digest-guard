#!/usr/bin/env python3
"""Step 2: run the detection+fix engine against every discovered file.
Computes the fixed content in memory only -- this step never writes to
the real repository. Writing only happens in open_pr.py, and only when
the caller explicitly asked for it (open_pr=true).

Three outcomes per file, not two: CLEAR (nothing to flag), FOUND (a
fixable mutable-tag-into-terraform pattern, with the fix computed), and
UNKNOWN (a real signal that image/tag identity reaches terraform some
way this file can't fully verify -- a differently-named tag/image
variable whose value can't be matched, or a .tfvars/secret-sourced
value). Confirmed necessary by testing against real production repos:
reporting CLEAR for an UNKNOWN case would be a false claim of safety
neither this engine nor anyone reading only the workflow YAML can
actually back up. A file can be both FOUND and UNKNOWN at once (e.g. a
monorepo where one service's pattern is fixable and another isn't)."""
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
    total_unknowns = 0
    for path in files:
        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception as e:
            results.append({"path": path, "status": "SKIPPED",
                             "findings": [], "unknowns": [], "reason": f"could not read: {e}"})
            continue

        try:
            fixed_lines, findings = engine.fix_all(lines)
            unknowns = engine.find_unknowns(fixed_lines)
        except Exception as e:
            results.append({"path": path, "status": "SKIPPED", "findings": [], "unknowns": [],
                             "reason": f"could not analyze/fix ({e.__class__.__name__}: {e})"})
            continue

        if not findings and not unknowns:
            results.append({"path": path, "status": "CLEAR", "findings": [], "unknowns": []})
            continue

        total_issues += len(findings)
        total_unknowns += len(unknowns)
        if findings and unknowns:
            status = "MIXED"
        elif findings:
            status = "FOUND"
        else:
            status = "UNKNOWN"

        results.append({
            "path": path,
            "status": status,
            "findings": [
                {"kind": f["kind"], "scope": f["scope"], "build_job": f["build_job"],
                 "deploy_job": f["deploy_job"], "image_repo": f["image_repo"]}
                for f in findings
            ],
            "unknowns": [
                {"build_job": u["build_job"], "deploy_job": u["deploy_job"],
                 "line": u["line"], "reason": u["reason"]}
                for u in unknowns
            ],
            "fixed_content": "".join(fixed_lines) if findings else None,
        })

    out = {
        "ok": True,
        "demo": demo,
        "repo_path": repo_path,
        "total_files": len(files),
        "total_issues": total_issues,
        "total_unknowns": total_unknowns,
        "results": results,
    }
    out["packed"] = {
        "total_issues": total_issues,
        "total_unknowns": total_unknowns,
        "results": [
            {"path": r["path"], "status": r["status"],
             "findings": r.get("findings", []), "unknowns": r.get("unknowns", []),
             "fixed_content": r.get("fixed_content")}
            for r in results
        ],
        "demo": demo,
        "repo_path": repo_path,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
