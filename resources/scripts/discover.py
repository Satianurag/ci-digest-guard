#!/usr/bin/env python3
"""Step 1: find which GitHub Actions workflow files to scan.

Every process.exec step in this play runs inside a rote-managed workspace
directory, not wherever the caller's terminal happened to be -- confirmed
by testing, not assumed (`rote play run` from inside a real repo still
puts the step's cwd at ~/.rote/workspaces/dag-ci-digest-guard-<hash>).
So `repo_path` must be an absolute path the caller supplies explicitly;
there is no implicit "current directory" a step can fall back on. An
agent invoking this on someone's behalf already knows its own working
directory and should pass it through directly.

Real mode: everything under `repo_path/workflows_subdir` ending in
.yml/.yaml. Demo mode: the three bundled fixtures shipped with this
play, so the whole thing produces real, meaningful output with zero
setup, on a machine with no target repo at all."""
import json
import os
import sys


def main():
    repo_path = sys.argv[1] if len(sys.argv) > 1 else ""
    workflows_subdir = sys.argv[2] if len(sys.argv) > 2 else ".github/workflows"
    demo = sys.argv[3].strip().lower() in ("true", "1", "yes") if len(sys.argv) > 3 else False

    if demo:
        here = os.path.dirname(os.path.abspath(__file__))
        fixtures_dir = os.path.join(os.path.dirname(here), "fixtures")
        files = sorted(
            os.path.join(fixtures_dir, f)
            for f in os.listdir(fixtures_dir)
            if f.endswith((".yml", ".yaml"))
        )
        result = {"ok": True, "demo": True, "repo_path": None, "path": fixtures_dir, "files": files}

    elif not repo_path:
        result = {"ok": True, "demo": False, "repo_path": None, "path": None, "files": [],
                   "warning": "repo_path is required outside demo mode -- pass the absolute path "
                              "to the repository root (e.g. repo_path=$(pwd) if you are standing in it)"}

    elif not os.path.isabs(repo_path):
        result = {"ok": True, "demo": False, "repo_path": repo_path, "path": None, "files": [],
                   "warning": f"repo_path must be absolute, got a relative path: {repo_path!r} -- "
                              "steps run in an isolated rote workspace, not your terminal's directory, "
                              "so a relative path cannot be resolved against anything meaningful"}

    else:
        target = os.path.join(repo_path, workflows_subdir)
        if not os.path.isdir(target):
            result = {"ok": True, "demo": False, "repo_path": repo_path, "path": target, "files": [],
                       "warning": f"directory not found: {target}"}
        else:
            files = sorted(
                os.path.join(target, f)
                for f in os.listdir(target)
                if f.endswith((".yml", ".yaml"))
            )
            result = {"ok": True, "demo": False, "repo_path": repo_path, "path": target, "files": files}

    result["packed"] = {"files": result["files"], "demo": result["demo"], "repo_path": result["repo_path"]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
