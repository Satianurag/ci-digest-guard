/**
 * CI Digest Guard
 *
 * Finds a GitHub Actions pipeline that builds a Docker image and then
 * deploys it via Terraform using a mutable tag (:latest, a branch name,
 * or no tag at all) instead of the immutable digest the build step
 * already produced -- and fixes it. Covers both docker/build-push-action
 * and a raw `docker build`/`docker buildx build` shell command. No LLM
 * anywhere: pure structural detection over the workflow YAML, and a
 * minimal, format-preserving text patch. Same input always produces the
 * same fix.
 *
 * @rote-frontmatter
 * ---
 * name: ci-digest-guard
 * description: "Finds a GitHub Actions pipeline that builds a Docker image and then deploys it with Terraform using a mutable tag instead of the immutable digest the build step already produced, and fixes it. A push landing between build and deploy -- or a scheduled re-run -- can otherwise deploy a different image than the one that was actually built and tested; a Docker maintainer has confirmed there are no plans to make buildx share images across GitHub Actions jobs another way, which is why pipelines lean on this pattern in the first place. Covers two build shapes: docker/build-push-action (any version tag or commit-SHA pin), and a raw `docker build`/`docker buildx build` shell command -- for the raw shape, a plain build prints several different sha256 digests (manifest, config, attestation, manifest list) and grabbing the first one seen would silently pick the wrong one, so the fix adds --metadata-file and reads its containerimage.digest field instead, the same field name the Action uses internally, cross-checked against `docker inspect --format {{.RepoDigests}}` on a real build. Also catches the case where --metadata-file is already present but its digest is never actually read anywhere. Matches ANY -var or TF_VAR_ name, not just literally 'image' or 'IMAGE' -- confirmed necessary against a real repo (cal-itp/benefits names its variable TF_VAR_CONTAINER_TAG) -- and follows a real terraform command across backslash line-continuations onto later lines, where the actual -var flags often live (confirmed against everclearorg/mark). When a build+deploy pair exists but no variable's value can be matched to the built image at all, reports UNKNOWN rather than a false CLEAR: this fires only on a real, visible signal -- a differently-named tag/image/version/sha/digest/ref variable whose value doesn't match, or a .tfvars/-var-file reference -- confirmed against two more real production repos where the image reference is either assembled inside Terraform HCL from a bare `${{ github.sha }}` (cal-itp) or written into a generated tfvars file from a GitHub Secret this play correctly cannot and should not see through (skkuding/codedang). Validated end to end against 25+ real public repositories found via GitHub code search, not just synthetic fixtures; that search surfaced and fixed a job-parser bug where a single comment line placed right after `jobs:` or between two job blocks silently produced zero detected jobs across the entire file, with no error -- confirmed live on skkuding/codedang's real workflow. Detects the pattern whether build and deploy share one job or are linked by needs:, whether the build step has one tag or several (multi-line block, or repeated -t/--tag flags), and whether multiple independent build+deploy pairs exist in one file (a monorepo with several services -- every pair gets fixed, not just the first). Every fix is minimal: it adds an id: to the build step, an output exposing the digest, and rewires only the one line that named the mutable tag -- nothing else in the file is touched, so the diff a reviewer sees is exactly the change and nothing more. Out of scope, stated plainly rather than silently guessed at: resolving arbitrary shell `$VAR`/`${VAR}` or `${{ env.X }}` references to their literal values, and reading actual Terraform .tf/HCL files to see how a bare tag variable gets assembled into a full image reference -- both report UNKNOWN when a relevant signal is present, never a false CLEAR. No third-party dependencies: pure python3 standard library, so there is nothing to pip install on a machine that may not allow it. Read-only unless open_pr=true, which writes the fix to disk on a new branch and opens a PR through your already-authenticated gh CLI -- no new credential. Pass demo=true to run against bundled fixture workflows with zero setup. Honest about what this is: not a daily-habit tool -- run it again on an already-fixed repo and it reports CLEAR every time, because the thing it checks doesn't change on its own between runs. Two real usage patterns instead: run it once per repo to fix what's already there, or copy the GitHub Actions template in this play's own repo (github.com/Satianurag/ci-digest-guard, templates/) into your repo so it gates every pull request that touches a workflow file -- far more frequent than daily for an active repo, and exactly the moment this bug gets introduced. That gate needs no Rote at CI time, only the same zero-dependency python3 engine; verified end to end against a real two-branch repo with a real git diff: a PR introducing the bug fails with a GitHub error annotation, a PR fixing it passes, a PR with only an UNKNOWN signal warns without blocking, and a PR that doesn't touch workflows is skipped entirely."
 * source: https://github.com/Satianurag/ci-digest-guard
 * provenance:
 *   author: Satianurag <anuragsati6476@gmail.com>
 *   workspace: satianurag/ci-digest-guard
 * metadata:
 *   version: 0.4.0
 *   rote_version: 0.78.0
 *   status: draft
 *   kind: atomic
 *   flow_type: parallel
 *   execution_model: steps_with_presentation
 *   format: typescript
 *   requires_endpoints: []
 *   requires_sessions: false
 *   contract:
 *     atomic: true
 *     input:
 *       type: none
 *     output:
 *       format: json
 *       destination: stdout
 *     composable: true
 *   discoverability:
 *     tags:
 *     - typescript
 *     - docker
 *     - terraform
 *     - github-actions
 *     - ci-cd
 *     - supply-chain
 *     - deterministic
 *     - zero-dependencies
 *     - effect-optional-write
 * parameters:
 * - name: repo_path
 *   param_type: string
 *   required: false
 *   default: ""
 *   description: "Absolute path to the target repository's root. Required outside demo mode: every step in this play runs inside an isolated rote workspace directory, not wherever your terminal happened to be, so there is no current directory to fall back on -- an agent invoking this on someone's behalf already knows its own absolute working directory and should pass it through directly (e.g. repo_path=/Users/you/your-repo, or repo_path=$(pwd) if standing in it). Ignored in demo mode."
 *   example: "/Users/you/your-repo"
 * - name: workflows_subdir
 *   param_type: string
 *   required: false
 *   default: ".github/workflows"
 *   description: "Path to the workflow directory, relative to repo_path. Ignored in demo mode."
 *   example: ".github/workflows"
 * - name: open_pr
 *   param_type: boolean
 *   required: false
 *   default: "false"
 *   description: "If true, writes every fix to disk on a new branch (ci-digest-guard/pin-image-digest) and opens a PR via gh. If false (default), only reports findings -- fully read-only. Always false in demo mode regardless of what is passed."
 *   example: "false"
 * - name: demo
 *   param_type: boolean
 *   required: false
 *   default: "false"
 *   description: "Run against three bundled fixture workflows instead of your real repository, for a zero-setup first look. Never writes anything and never opens a PR, even if open_pr=true."
 *   example: "true"
 * tags:
 * - docker
 * - terraform
 * - github-actions
 * - ci-cd
 * - supply-chain
 * - deterministic
 * - zero-dependencies
 * discoverability:
 *   tags:
 *   - docker
 *   - terraform
 *   - github-actions
 *   - ci-cd
 *   - deterministic
 *   - zero-dependencies
 * # Every path this play writes, with mode and reason -- disclosed here
 * # because a process.exec play cannot populate the registry's own
 * # effects.declaredWrites; `rote play inspect` will still print "Write
 * # permissions: none declared" for a play shaped this way.
 * write_permissions:
 * - resource: "<repo>/.github/workflows/*.yml matched by `path`"
 *   mode: replace
 *   why: "Only when open_pr=true and a fix was found: the exact workflow file is rewritten with the minimal fix (id:, outputs:, and the one corrected line) before being committed. Never touched when open_pr=false (the default) or when demo=true."
 * - resource: "local git branch ci-digest-guard/pin-image-digest, and its PR on the configured remote"
 *   mode: create
 *   why: "Only when open_pr=true and a fix was found. Uses the git and gh already configured on this machine -- no new credential is requested or stored."
 * steps:
 *   discover:
 *     type: process.exec
 *     timeout_ms: 10000
 *     argv:
 *     - python3
 *     - '@resource{scripts/discover.py}'
 *     - $repo_path
 *     - $workflows_subdir
 *     - $demo
 *   scan:
 *     type: process.exec
 *     timeout_ms: 20000
 *     depends_on:
 *     - discover
 *     argv:
 *     - python3
 *     - '@resource{scripts/scan.py}'
 *     - '@discover{$.stdout.text | fromjson | .packed}'
 *   open_pr:
 *     type: process.exec
 *     timeout_ms: 30000
 *     depends_on:
 *     - scan
 *     execution:
 *       mode: deferred
 *       condition:
 *         compare:
 *           left:
 *             param: open_pr
 *           op: eq
 *           right: true
 *     argv:
 *     - python3
 *     - '@resource{scripts/open_pr.py}'
 *     - '@scan{$.stdout.text | fromjson | .packed}'
 * presentation_fixtures:
 *   discover: resources/presentation-fixtures/discover/fixture.yaml
 *   scan: resources/presentation-fixtures/scan/fixture.yaml
 *   open_pr: resources/presentation-fixtures/open_pr/fixture.yaml
 * ---
 *
 * Usage:
 *   rote play run ci-digest-guard demo=true
 *   rote play run ci-digest-guard repo_path=/Users/you/your-repo
 *   rote play run ci-digest-guard repo_path=/Users/you/your-repo open_pr=true
 *
 * Output modes (the runner's --output flag):
 *   human (default)  the finding list, or CLEAR, per file
 *   summary          "2 files, 1 issue found · ready to fix" style one-liner
 *   json             the canonical ci-digest-guard/1 result object
 */

// ---------------------------------------------------------------------------
// Presentation plane: deprivileged, renders only what the steps produced.
// Imports ONLY the presentation SDK; owns no effects, no fs, no fetch.
// ---------------------------------------------------------------------------

const {
  FlowOutput,
  isProcessExecBody,
  loadPresentationContext,
  stepName,
} = await import("__ROTE_PRESENTATION_SDK__");

const out = new FlowOutput();
const ctx = await loadPresentationContext();

interface Finding {
  kind: string;
  scope: string;
  build_job: string;
  deploy_job: string;
  image_repo: string;
}

interface Unknown {
  build_job: string;
  deploy_job: string;
  line: number;
  reason: string;
}

interface ScanResultEntry {
  path: string;
  status: "CLEAR" | "FOUND" | "UNKNOWN" | "MIXED" | "SKIPPED";
  findings: Finding[];
  unknowns: Unknown[];
  fixed_content?: string | null;
  reason?: string;
}

interface ScanOut {
  ok: boolean;
  demo: boolean;
  total_files: number;
  total_issues: number;
  total_unknowns: number;
  results: ScanResultEntry[];
}

interface DiscoverOut {
  ok: boolean;
  demo: boolean;
  path: string;
  files: string[];
  warning?: string;
}

interface OpenPrOut {
  ok: boolean;
  skipped: boolean;
  reason?: string;
  pr_url?: string;
  branch?: string;
  files_fixed?: string[];
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function execStepStdout(
  label: string,
  step: ReturnType<typeof ctx.step>,
  available: ReturnType<typeof ctx.requireAvailable>,
): Record<string, unknown> {
  const status = step.outcome.status;
  if (status === "failed") {
    const output = asRecord(step.outcome.output);
    throw new Error(`step ${label} failed: ${String(output.message ?? "no message captured")}`);
  }
  if (status === "blocked") {
    throw new Error(`step ${label} was blocked: check upstream step failures`);
  }
  if (!isProcessExecBody(available.body)) {
    throw new Error(`step ${label} did not record a process.exec observation`);
  }
  const exit = available.body.status.exit;
  if (exit.kind !== "code" || exit.code !== 0) {
    throw new Error(
      `step ${label} exited ${exit.kind !== "code" ? exit.kind : exit.code}: ${
        available.body.stderr?.text ?? "no stderr captured"
      }`,
    );
  }
  const text = available.body.stdout?.text;
  if (text === undefined) {
    throw new Error(`step ${label} captured no stdout`);
  }
  try {
    return asRecord(JSON.parse(text));
  } catch (error) {
    throw new Error(`step ${label} stdout is not JSON: ${String(error)}`);
  }
}

function readDiscoverStdout(): DiscoverOut {
  return execStepStdout(
    "discover",
    ctx.step(stepName("discover")),
    ctx.requireAvailable(stepName("discover")),
  ) as unknown as DiscoverOut;
}

function readScanStdout(): ScanOut {
  return execStepStdout(
    "scan",
    ctx.step(stepName("scan")),
    ctx.requireAvailable(stepName("scan")),
  ) as unknown as ScanOut;
}

function failedStepMessage(label: string, step: ReturnType<typeof ctx.step>): string | null {
  if (step.outcome.status !== "failed") return null;
  const output = asRecord(step.outcome.output);
  return `${label}: ${String(output.message ?? "no message captured")}`;
}

async function renderFailureViews(message: string): Promise<void> {
  const failedSteps = [
    failedStepMessage("discover", ctx.step(stepName("discover"))),
    failedStepMessage("scan", ctx.step(stepName("scan"))),
    failedStepMessage("open_pr", ctx.step(stepName("open_pr"))),
  ].filter((entry): entry is string => entry !== null);
  const detail = failedSteps.length > 0 ? failedSteps.join("; ") : message;
  out.human(["ci-digest-guard · run failed", "", "  " + detail].join("\n"));
  out.summary("run failed — " + (failedSteps.length > 0 ? failedSteps[0].split(":")[0] : "see detail"));
  out.result({
    schema: "ci-digest-guard/1",
    error: message,
    run_status: ctx.run.status,
    run_id: ctx.run.run_id,
    representations: {
      human: "failure evidence — the failing step and its diagnostic",
      json: "canonical — error, run_status, run_id",
      summary: "intentionally lossy — failure signal only",
    },
  });
}

async function renderSuccess(): Promise<void> {
  const discoverOut = readDiscoverStdout();
  const scanOut = readScanStdout();

  let openPrOut: OpenPrOut | null = null;
  const openPrStep = ctx.step(stepName("open_pr"));
  if (openPrStep.outcome.status === "completed" || openPrStep.outcome.status === "restored") {
    openPrOut = execStepStdout(
      "open_pr",
      openPrStep,
      ctx.requireAvailable(stepName("open_pr")),
    ) as unknown as OpenPrOut;
  }

  const badge = scanOut.demo ? " [DEMO]" : "";
  const lines: string[] = [];
  const unknownNote = scanOut.total_unknowns > 0 ? ` · ${scanOut.total_unknowns} unverifiable` : "";
  lines.push(`ci-digest-guard · ${scanOut.total_files} file(s) scanned · ${scanOut.total_issues} issue(s)${unknownNote}${badge}`);
  lines.push("");

  if (discoverOut.warning) {
    lines.push(`  ${discoverOut.warning}`);
    lines.push("");
  }

  for (const r of scanOut.results) {
    const short = r.path.split("/").slice(-1)[0];
    if (r.status === "CLEAR") {
      lines.push(`  CLEAR    ${short}`);
    } else if (r.status === "SKIPPED") {
      lines.push(`  SKIP     ${short} — ${r.reason ?? "could not analyze"}`);
    } else {
      if (r.status === "FOUND" || r.status === "MIXED") {
        lines.push(`  FOUND    ${short}`);
        for (const f of r.findings) {
          const where = f.scope === "same-job" ? "same job" : `job '${f.deploy_job}' (needs '${f.build_job}')`;
          lines.push(`           · ${f.image_repo}:<mutable tag> deployed via ${f.kind === "cli" ? "-var" : "TF_VAR_*"} in ${where} — fixed`);
        }
      }
      if (r.status === "UNKNOWN" || r.status === "MIXED") {
        lines.push(`  UNKNOWN  ${short}`);
        for (const u of r.unknowns) {
          lines.push(`           · job '${u.deploy_job}' (build '${u.build_job}'), line ${u.line}: ${u.reason}`);
        }
      }
    }
  }

  if (scanOut.total_issues > 0) {
    lines.push("");
    if (openPrOut) {
      if (openPrOut.skipped) {
        lines.push(`  fix not applied: ${openPrOut.reason}`);
      } else {
        lines.push(`  fix opened: ${openPrOut.pr_url} (branch ${openPrOut.branch})`);
      }
    } else {
      lines.push("  read-only run. Re-run with open_pr=true to write the fix and open a PR.");
    }
  }
  if (scanOut.total_unknowns > 0) {
    lines.push("");
    lines.push("  UNKNOWN means: a real signal shows image/tag identity reaching terraform,");
    lines.push("  but its value could not be verified from this workflow file alone (a");
    lines.push("  differently-named variable, a value assembled elsewhere, or a .tfvars/");
    lines.push("  secret-sourced value). Not fixed automatically -- worth a human look.");
  }

  out.human(lines.join("\n"));

  out.summary(
    scanOut.total_issues === 0 && scanOut.total_unknowns === 0
      ? `${scanOut.total_files} file(s) — CLEAR${badge}`
      : `${scanOut.total_files} file(s), ${scanOut.total_issues} issue(s) found${unknownNote}${
          openPrOut && !openPrOut.skipped ? " · PR opened" : ""
        }${badge}`,
  );

  out.result({
    schema: "ci-digest-guard/1",
    demo: scanOut.demo,
    path: discoverOut.path,
    total_files: scanOut.total_files,
    total_issues: scanOut.total_issues,
    total_unknowns: scanOut.total_unknowns,
    results: scanOut.results.map((r) => ({
      path: r.path,
      status: r.status,
      findings: r.findings,
      unknowns: r.unknowns,
      reason: r.reason ?? null,
    })),
    pr: openPrOut,
    play_version: "0.3.0",
    run_id: ctx.run.run_id,
    representations: {
      human: "complete — per-file status, findings, and PR outcome if requested",
      json: "canonical — full per-file result list plus PR detail",
      summary: "intentionally lossy — file/issue counts and PR status only",
    },
  });
}

try {
  if (ctx.run.status === "failed") {
    await renderFailureViews("the run recorded a failed step; inspect the stage ledger above");
  } else {
    await renderSuccess();
  }
} catch (error) {
  await renderFailureViews(String((error as Error)?.message ?? error));
}
