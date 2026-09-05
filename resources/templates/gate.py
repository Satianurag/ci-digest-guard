#!/usr/bin/env python3
"""Standalone PR gate: fails a pull request that introduces the
mutable-tag-into-terraform pattern into any GitHub Actions workflow file.

This is the SAME detection engine ci-digest-guard runs as a Rote Play --
copy engine.py into this same directory and this script needs nothing
else. No Rote required at CI time: this is plain python3 stdlib, which
is the whole reason the engine has zero third-party dependencies.

Usage in a workflow (see pr-gate.yml in this same directory):
    python3 .github/scripts/gate.py $(git diff --name-only \
        origin/${{ github.base_ref }}... -- '.github/workflows/*.yml' '.github/workflows/*.yaml')

Exit code 1 (fails the check) on a FOUND (fixable, confirmed mutable-tag)
result, and also on an UNKNOWN that gets [CONFIRMED] against this repo's
own real .tf files (see engine.py's confirm_unknown_via_hcl) -- that one
is no longer a maybe, it's a proven mutable-tag deploy, just one whose
fix belongs in Terraform rather than this YAML file. A plain,
unconfirmed UNKNOWN prints a warning annotation but does not fail the
build -- it means a real signal exists that this script cannot fully
verify (see engine.py's find_unknowns docstring), which is a prompt for
human review, not a proven problem.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402


def main():
    paths = [p for p in sys.argv[1:] if p.endswith((".yml", ".yaml"))]
    if not paths:
        print("ci-digest-guard gate: no changed workflow files to check")
        return 0

    # This script runs inside the target repo's own checkout at CI time
    # (see the usage example above) -- os.getcwd() IS the repo root, the
    # same real filesystem access engine.py's HCL cross-reference needs.
    # Computed once for the whole run, not per-file.
    repo_path = os.getcwd()
    tf_files = engine.find_hcl_files(repo_path)

    had_found = False
    had_unknown = False
    for path in paths:
        if not os.path.isfile(path):
            continue  # deleted in this PR
        try:
            with open(path) as f:
                lines = f.readlines()
        except Exception as e:
            print(f"::warning file={path}::ci-digest-guard could not read this file ({e})")
            continue

        try:
            fixed_lines, findings = engine.fix_all(lines)
            unknowns = engine.find_unknowns(fixed_lines)
        except Exception as e:
            print(f"::warning file={path}::ci-digest-guard could not analyze this file "
                  f"({e.__class__.__name__}: {e})")
            continue

        if findings:
            had_found = True
            for f in findings:
                where = "the same job" if f["scope"] == "same-job" else f"job '{f['deploy_job']}'"
                print(f"::error file={path}::ci-digest-guard: {f['image_repo']} is deployed via a "
                      f"mutable tag (not the digest the build step produced) in {where}. "
                      f"Run the ci-digest-guard Play with open_pr=true to generate the fix, "
                      f"or apply it by hand: pin to @<digest> instead of :<tag>.")
        if unknowns:
            for u in unknowns:
                evidence = engine.confirm_unknown_via_hcl(u, repo_path, tf_files)
                if evidence:
                    had_found = True
                    print(f"::error file={path}::ci-digest-guard: {u['reason']} -- CONFIRMED against "
                          f"this repo's own Terraform code ({evidence}). Not auto-fixable here: the "
                          f"fix belongs in Terraform (accept a full image@digest reference).")
                else:
                    had_unknown = True
                    print(f"::warning file={path}::ci-digest-guard: {u['reason']} "
                          f"(job '{u['deploy_job']}', line {u['line']}) -- worth a human look, "
                          f"not auto-fixable.")
        if not findings and not unknowns:
            print(f"ci-digest-guard gate: {path} CLEAR")

    if had_found:
        return 1
    if had_unknown:
        print("ci-digest-guard gate: no confirmed issues, but see the warnings above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
