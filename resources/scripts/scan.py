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


def _annotate_unknown(u, repo_path, tf_files):
    evidence = engine.confirm_unknown_via_hcl(u, repo_path, tf_files) if tf_files else None
    return {"build_job": u["build_job"], "deploy_job": u["deploy_job"],
            "line": u["line"], "reason": u["reason"],
            "confirmed": evidence is not None, "confirmed_evidence": evidence}


def main():
    discover_json = json.loads(sys.argv[1])
    files = discover_json.get("files", [])
    demo = discover_json.get("demo", False)
    repo_path = discover_json.get("repo_path")

    # Real .tf/HCL files in the repo can resolve an UNKNOWN into a
    # CONFIRMED finding -- e.g. cal-itp/benefits passes a bare
    # TF_VAR_CONTAINER_TAG through the workflow, invisible to this engine
    # from the YAML alone, but that variable is used directly to build a
    # container image reference inside the repo's own Terraform code,
    # which repo_path already gives real access to. Computed once for the
    # whole run, not per-file -- os.walk over a real repo isn't free, and
    # every file's UNKNOWNs cross-reference the same .tf tree regardless
    # of which workflow file they came from. Never attempted in demo mode
    # (fixtures have no real repo_path to search) or without a real,
    # absolute repo_path.
    tf_files = (
        engine.find_hcl_files(repo_path)
        if repo_path and os.path.isabs(repo_path) and not demo
        else []
    )

    results = []
    total_issues = 0
    total_unknowns = 0
    for path in files:
        try:
            # Sniff the ORIGINAL line ending before Python's text-mode
            # universal-newline translation erases it (open()'s default
            # silently turns every \r\n into \n on read). Confirmed a real
            # gap against the play's own stated promise ("nothing else in
            # the file is touched, so the diff a reviewer sees is exactly
            # the change and nothing more"): a CRLF repo file, once run
            # through this engine and written back with Python's default
            # LF-only write, would show as EVERY line changed in the
            # resulting PR diff -- pure line-ending churn burying the one
            # real line that actually changed.
            with open(path, "rb") as f:
                raw = f.read()
            uses_crlf = b"\r\n" in raw
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
            "unknowns": [_annotate_unknown(u, repo_path, tf_files) for u in unknowns],
            "fixed_content": ("".join(fixed_lines).replace("\n", "\r\n") if uses_crlf
                               else "".join(fixed_lines)) if findings else None,
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

    # `packed` crosses into the next step (open_pr.py) through a
    # cross-step @scan{...} argv substitution, which has a hard ~64KB
    # size ceiling that silently truncates JSON mid-string past that
    # point -- confirmed and already fixed this same way in two sibling
    # plays (dupe-sweep, repo-fire-check). Embedding every fixed file's
    # FULL content inline here (a real workflow YAML easily runs several
    # KB, and a monorepo can have many) blows past that ceiling on a
    # perfectly ordinary repo, corrupting open_pr.py's own
    # json.loads(sys.argv[1]) with no clear error. Bulk content goes to
    # an on-disk state file in this step's own workspace directory
    # instead -- the same directory every step in this DAG run shares as
    # its cwd (confirmed by testing) -- and only a small pointer crosses
    # the argv boundary.
    state_path = os.path.join(os.getcwd(), "ci-digest-guard-fixed-content.json")
    fixed_by_path = {r["path"]: r["fixed_content"] for r in results if r.get("fixed_content")}
    with open(state_path, "w") as f:
        json.dump(fixed_by_path, f)

    out["packed"] = {
        "total_issues": total_issues,
        "total_unknowns": total_unknowns,
        "state_file": state_path,
        "results": [
            {"path": r["path"], "status": r["status"],
             "findings": r.get("findings", []), "unknowns": r.get("unknowns", []),
             "has_fix": bool(r.get("fixed_content"))}
            for r in results
        ],
        "demo": demo,
        "repo_path": repo_path,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
