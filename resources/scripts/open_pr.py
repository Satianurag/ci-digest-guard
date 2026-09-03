#!/usr/bin/env python3
"""Step 3 (gated behind open_pr=true, and only runs at all when the DAG
condition lets it): write every fix to disk, commit them on one new
branch, and open a single PR via `gh` -- the GitHub CLI already
authenticated on this machine, not a new credential.

This process's own working directory is a rote-managed workspace, not
the target repository -- confirmed by testing, not assumed. Every git
and gh command below must run against repo_path (chdir'd into first),
or "git commit"/"gh pr create" would silently operate on the wrong
directory (most likely: not a git repo at all).

Never touches anything in demo mode: fixtures are for looking at the
mechanism, not for opening a real PR from bundled sample files."""
import json
import os
import subprocess
import sys


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def main():
    scan_json = json.loads(sys.argv[1])
    demo = scan_json.get("demo", False)
    repo_path = scan_json.get("repo_path")
    results = scan_json.get("results", [])
    found = [r for r in results if r.get("status") == "FOUND"]

    if demo:
        print(json.dumps({"ok": True, "skipped": True,
                           "reason": "demo mode never opens a real PR; re-run with demo=false against a real repo"}))
        return

    if not found:
        print(json.dumps({"ok": True, "skipped": True, "reason": "nothing to fix"}))
        return

    if not repo_path or not os.path.isabs(repo_path):
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"repo_path missing or not absolute ({repo_path!r}); "
                                     "cannot know where to run git/gh"}))
        return

    try:
        os.chdir(repo_path)
    except Exception as e:
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"could not chdir to repo_path {repo_path!r}: {e}"}))
        return

    gh_check = run(["gh", "auth", "status"])
    if gh_check.returncode != 0:
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": "gh is not authenticated on this machine; run `gh auth login` first",
                           "detail": gh_check.stderr.strip()}))
        return

    git_check = run(["git", "rev-parse", "--is-inside-work-tree"])
    if git_check.returncode != 0:
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": "not inside a git repository"}))
        return

    branch = "ci-digest-guard/pin-image-digest"
    base_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    create_branch = run(["git", "checkout", "-b", branch])
    if create_branch.returncode != 0:
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"could not create branch {branch}",
                           "detail": create_branch.stderr.strip()}))
        return

    # From here on, ANY failure -- a git command failing, or an unexpected
    # exception in our own code -- must leave the repo exactly as it was
    # found: back on base_branch, branch deleted, working tree restored.
    # (Confirmed by testing: deleting a branch alone does NOT discard the
    # working-tree edits made while it was checked out -- they silently
    # carry over to whatever branch you land on next.)
    def rollback():
        run(["git", "checkout", "--", "."])
        run(["git", "checkout", base_branch])
        run(["git", "branch", "-D", branch])

    try:
        written = []
        for r in found:
            with open(r["path"], "w") as f:
                f.write(r["fixed_content"])
            written.append(r["path"])

        run(["git", "add"] + written)
        summary_lines = []
        for r in found:
            rel_path = os.path.relpath(r["path"], repo_path)
            for f in r["findings"]:
                where = "the same job" if f["scope"] == "same-job" else "job '" + f["deploy_job"] + "'"
                summary_lines.append(
                    f"- {rel_path}: {f['image_repo']} deploy in {where} "
                    f"now pinned to the digest {f['build_job']} produced, instead of a mutable tag"
                )
        commit_msg = "ci: pin deployed image to build digest, not a mutable tag\n\n" + "\n".join(summary_lines)
        commit = run(["git", "commit", "-m", commit_msg])
        if commit.returncode != 0:
            rollback()
            print(json.dumps({"ok": False, "skipped": True,
                               "reason": "commit failed", "detail": commit.stderr.strip()}))
            return

        push = run(["git", "push", "-u", "origin", branch])
        if push.returncode != 0:
            # The commit is real and safe on the local branch; only the
            # push failed, so do NOT roll back -- that would discard a
            # good commit. Leave the branch as-is and report it plainly.
            print(json.dumps({"ok": False, "skipped": True,
                               "reason": "push failed (no push access to a remote?)",
                               "detail": push.stderr.strip(),
                               "branch_kept_locally": branch}))
            return

        pr_body = (
            "Automated by ci-digest-guard (a Rote Play, deterministic, no LLM in the path).\n\n"
            "Each fix below: the deploy step was applying `terraform apply`/`plan` against a "
            "mutable image tag (`:latest`, a branch name, or none) instead of the immutable "
            "digest the same pipeline's build step already produced. A push landing between "
            "build and deploy -- or a scheduled re-run -- can silently deploy a different "
            "image than the one that was actually built and tested.\n\n"
            + "\n".join(summary_lines)
        )
        pr = run(["gh", "pr", "create", "--title", "ci: pin deployed image to build digest, not a mutable tag",
                  "--body", pr_body, "--base", base_branch, "--head", branch])
        if pr.returncode != 0:
            # Already pushed -- rolling back would orphan a remote branch
            # without explanation. Leave it pushed and report plainly;
            # the PR can be opened by hand from the pushed branch.
            print(json.dumps({"ok": False, "skipped": True,
                               "reason": "gh pr create failed", "detail": pr.stderr.strip(),
                               "branch_pushed": branch}))
            return

        print(json.dumps({"ok": True, "skipped": False, "pr_url": pr.stdout.strip(),
                           "branch": branch, "files_fixed": written}))

    except Exception as e:
        rollback()
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"unexpected error, rolled back to {base_branch}: "
                                     f"{e.__class__.__name__}: {e}"}))


if __name__ == "__main__":
    main()
