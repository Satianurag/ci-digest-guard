# ci-digest-guard

**Your Docker+Terraform pipeline probably deploys `:latest`, not the image it just built and tested. This finds it and fixes it — once for what's already there, and permanently for what comes next.**

A push landing between build and deploy — or a scheduled re-run — can silently
deploy a different image than the one that was actually built and tested. A
Docker maintainer has confirmed there are no plans to make `buildx` share
images across GitHub Actions jobs another way, which is why pipelines lean on
a mutable tag in the first place instead of the digest the build step already
produced.

```
ci-digest-guard · 1 file(s) scanned · 1 issue(s)

  FOUND    deploy.yml
           · ghcr.io/acme/api:<mutable tag> deployed via -var in job 'deploy' (needs 'build') — fixed

  fix opened: https://github.com/acme/api/pull/142 (branch ci-digest-guard/pin-image-digest)
```

## Be honest about what this is

This is **not** a daily-habit tool. Run it on a repo whose pipeline is already
correct, and it reports `CLEAR` every single time — there's nothing new to
tell you, because the thing it checks doesn't change between runs on its own.
That's a real difference from something like a git-status dashboard, where the
underlying state moves every day whether you touch it or not.

So there are two honest ways to actually use this, and only one of them
resembles "daily":

1. **Run it once per repo** (or once per pipeline) to fix what's already
   there. `rote play run satianurag/ci-digest-guard repo_path=$(pwd)` — a
   one-time correction, like a linter you run on a legacy codebase before
   turning the linter on for good.
2. **Wire it into your own CI as a gate**, so it runs on *every pull request
   that touches a workflow file* — which for an active repo is far more
   frequent than daily, and it's exactly the moment this class of bug gets
   introduced. See [`resources/templates/`](./resources/templates) for a ready-to-copy GitHub
   Actions workflow that does this. No Rote installation needed at CI time:
   the whole engine is plain `python3` standard library on purpose, so the
   gate is two files copied into `.github/scripts/` and one workflow file.

Tested end to end: a PR that introduces the bug fails the check with a
`::error::` annotation; a PR that fixes it passes; a PR with only an
[UNKNOWN](#unknown-is-not-a-bug) signal warns without blocking; a PR that
doesn't touch workflow files is skipped entirely. All four verified with real
`git diff` against a real two-branch repo, not asserted.

## What it actually does

Covers two build shapes:

- **`docker/build-push-action`** (any version tag or commit-SHA pin)
- **A raw `docker build`/`docker buildx build` shell command** — a plain build
  prints several different `sha256` digests (manifest, config, attestation,
  manifest list), and grabbing the first one seen would silently pick the
  wrong one. The fix adds `--metadata-file` and reads its
  `containerimage.digest` field instead — the same field name
  `docker/actions-toolkit` uses internally, cross-checked against
  `docker inspect --format '{{.RepoDigests}}'` on a real build.

Detects the mutable-tag pattern whether it's a CLI `-var` (any quoting, any
variable name — not just literally `image`) or a `TF_VAR_*` env var, whether
build and deploy share one job or are linked by `needs:`, whether the build
step has one tag or several, whether the terraform command is one line or
continues across backslash line-continuations, and whether multiple
independent build+deploy pairs exist in one file (a monorepo with several
services — every pair gets fixed, not just the first).

Every fix is minimal: it adds an `id:` to the build step, an output exposing
the digest, and rewires only the one line that named the mutable tag —
nothing else in the file is touched, so the diff a reviewer sees is exactly
the change and nothing more.

## UNKNOWN is not a bug

Sometimes a build+deploy pair exists but no variable's value can be matched to
the built image — a differently-named tag/image/version/sha/digest/ref
variable whose value doesn't line up, or a `.tfvars`/`-var-file`/secret-backed
value. Reporting `CLEAR` in that case would be a false claim of safety nobody
reading only the workflow YAML can actually back up, so it reports `UNKNOWN`
instead, with the specific line and reason.

This fires only on a real, visible signal — never merely because Docker and
Terraform both appear somewhere in the same file. Confirmed against two real
production repos: one assembles the final image reference inside Terraform
HCL from a bare `${{ github.sha }}` this play correctly can't see the other
half of; the other writes a full `terraform.tfvars` from a GitHub Secret,
which is — correctly — opaque.

### UNKNOWN, then CONFIRMED: reading your actual Terraform

For the most common kind of UNKNOWN — a tag-ish variable whose value can't be
matched to the built image from the workflow file alone — the play now goes and
reads your repo's own `.tf` files, using the `repo_path` it already has. If that
exact variable turns up inside a container/deploy resource, interpolated into an
image reference with no digest, the finding is upgraded to **`[CONFIRMED]`** and
carries the real HCL line as evidence:

```
UNKNOWN  deploy.yml
   · [CONFIRMED] job 'deploy' (build 'build'), line 14: env var TF_VAR_CONTAINER_TAG …
     -> infra/main.tf: image = "ghcr.io/acme/api:${var.container_tag}"
```

Verified live against the real `cal-itp/benefits` repo — the same repo that
justified having an UNKNOWN category in the first place. It resolves
`TF_VAR_CONTAINER_TAG` → `var.container_tag` → `azurerm_container_app.web`'s
image, in `terraform/modules/application/app_web.tf`.

Deliberately one-directional: this can turn an UNKNOWN into a CONFIRMED
finding, and never turns an UNKNOWN into a CLEAR. Not finding a match proves
nothing — the reference could be inside a module, or a resource type the check
doesn't recognise — and a false CLEAR is the one claim this play refuses to
make. It also never *edits* Terraform, confirmed or not; that fix belongs in
HCL and is a human's call.

**Explicitly out of scope**, documented rather than silently guessed at:
resolving arbitrary shell `$VAR`/`${VAR}` or `${{ env.X }}` references to
their literal values, and writing to `.tf` files at all. Both report `UNKNOWN`
(or `CONFIRMED`, never auto-fixed) when a relevant signal is present.

## Tested against real production repositories, not just fixtures

25+ real public repos pulled via GitHub code search — `entur/*`,
`cal-itp/benefits`, `skkuding/codedang`, `everclearorg/mark`, `bcgov`,
`SevenTV/Website`, `semaphoreui/semaphore`, `sogilis/Voogle`,
`devsecblueprint`, and more. That testing found and fixed four real,
independent bugs no synthetic fixture surfaced:

1. `-var`/`TF_VAR_` matching was hardcoded to the literal word "image" — real
   Terraform modules name this variable anything (`cal-itp/benefits` uses
   `TF_VAR_CONTAINER_TAG`).
2. A real terraform command is frequently multi-line with backslash
   continuation, and the actual `-var` flags often live one or more lines
   below `terraform apply \` itself (`everclearorg/mark`).
3. The tagish-key heuristic used `\btag\b`-style word boundaries, which never
   match inside snake_case identifiers like `CONTAINER_TAG` because `_` counts
   as a word character in regex.
4. The most serious: the job parser silently returned an empty job map, with
   no error, whenever a comment line appeared right after `jobs:` or between
   two job entries — confirmed live on `skkuding/codedang`'s real workflow,
   which has exactly such a comment. Every job in an affected file went
   completely undetected.

## Usage

```bash
# Zero setup: bundled fixture workflows, nothing real touched
rote play run satianurag/ci-digest-guard demo=true

# Report only, against a real repo
rote play run satianurag/ci-digest-guard repo_path=/path/to/your-repo

# Write the fix and open a PR, via your already-authenticated gh CLI
rote play run satianurag/ci-digest-guard repo_path=/path/to/your-repo open_pr=true
```

No third-party dependencies: pure `python3` standard library end to end, so
there's nothing to `pip install` on a machine that may not allow it — this
was a deliberate rebuild after this project's own machine refused a global
`pip install` mid-development (PEP 668), exactly the setup friction this
play exists to avoid.

`git` and `gh` are only needed for `open_pr=true`; a read-only run, including
`demo=true`, needs neither.
