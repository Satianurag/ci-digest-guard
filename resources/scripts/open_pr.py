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
    # MIXED files (some fixable, some UNKNOWN) still carry a real fix --
    # only true UNKNOWN-only files have nothing to write.
    found = [r for r in results if r.get("status") in ("FOUND", "MIXED") and r.get("has_fix")]

    if demo:
        print(json.dumps({"ok": True, "skipped": True,
                           "reason": "demo mode never opens a real PR; re-run with demo=false against a real repo"}))
        return

    if not found:
        print(json.dumps({"ok": True, "skipped": True, "reason": "nothing to fix"}))
        return

    # scan.py writes the actual fixed file content to an on-disk state
    # file in this DAG run's shared workspace, rather than embedding it
    # in `packed` -- that JSON crosses into this step through a
    # cross-step @scan{...} argv substitution with a hard ~64KB size
    # ceiling that silently truncates mid-string past that point.
    # Embedding a real workflow YAML's full fixed content inline (and a
    # monorepo can have several) blew past it on a perfectly ordinary
    # repo. Confirmed and already fixed this same way in two sibling
    # plays (dupe-sweep, repo-fire-check).
    state_path = scan_json.get("state_file")
    if not state_path or not os.path.isabs(state_path):
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"scan step did not provide a valid state_file pointer ({state_path!r})"}))
        return
    try:
        with open(state_path) as f:
            fixed_by_path = json.load(f)
    except Exception as e:
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"could not read fixed content from {state_path!r}: {e}"}))
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

    # Refuse a dirty tree outright rather than trying to work around it.
    # Confirmed necessary by testing: rollback() previously ran
    # `git checkout -- .`, which restores EVERY tracked file from the
    # index -- including a file the caller had uncommitted WIP in in that
    # had nothing to do with this play. A failing commit (a pre-commit
    # hook, commit.gpgsign with no key, either is common) then triggered
    # that rollback and silently discarded the user's own unrelated,
    # unrecoverable edits. The only safe precondition is starting clean.
    dirty_check = run(["git", "status", "--porcelain"])
    if dirty_check.stdout.strip():
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": "repo has uncommitted changes -- refusing to start. "
                                     "Commit or stash your own work first, then re-run with open_pr=true.",
                           "dirty_files": dirty_check.stdout.strip().splitlines()}))
        return

    branch = "ci-digest-guard/pin-image-digest"
    base_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if base_branch == "HEAD":
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": "repo is in a detached HEAD state -- checked out on no branch, "
                                     "so there is no base branch to open a PR against or return to. "
                                     "Check out a real branch first."}))
        return

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
    #
    # rollback() only ever touches `written` -- the exact files THIS run
    # wrote -- never `git checkout -- .`. Confirmed by testing that the
    # blanket form is actively dangerous even with the dirty-tree preflight
    # above as a guard: `git checkout -- .` restores every tracked file
    # from the INDEX, and since `git add` (below) stages the fix before
    # the commit is attempted, a failed commit left the fix sitting
    # staged-and-uncommitted on base_branch after "rollback" -- the
    # opposite of rolling back. `git reset` un-stages first, then only the
    # specific fix files are checked out back to their pre-fix content.
    def rollback(written_files):
        run(["git", "reset"])
        if written_files:
            run(["git", "checkout", "--", *written_files])
        run(["git", "checkout", base_branch])
        run(["git", "branch", "-D", branch])

    written: list = []
    try:
        for r in found:
            with open(r["path"], "w") as f:
                f.write(fixed_by_path[r["path"]])
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
            rollback(written)
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
        rollback(written)
        print(json.dumps({"ok": False, "skipped": True,
                           "reason": f"unexpected error, rolled back to {base_branch}: "
                                     f"{e.__class__.__name__}: {e}"}))


if __name__ == "__main__":
    main()
