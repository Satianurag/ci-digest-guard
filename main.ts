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
 * description: "Finds a GitHub Actions pipeline that builds a Docker image and then deploys it with Terraform using a mutable tag instead of the immutable digest the build step already produced, and fixes it. A push landing between build and deploy -- or a scheduled re-run -- can otherwise deploy a different image than the one that was actually built and tested; a Docker maintainer has confirmed there are no plans to make buildx share images across GitHub Actions jobs another way, which is why pipelines lean on this pattern in the first place. Covers two build shapes: docker/build-push-action, and a raw `docker build`/`docker buildx build` shell command -- for the raw shape, a plain build prints several different sha256 digests and grabbing the first one seen would silently pick the wrong one, so the fix adds --metadata-file and reads its containerimage.digest field instead, cross-checked against `docker inspect --format {{.RepoDigests}}` on a real build. Also catches the case where --metadata-file is already present but its digest is never actually read anywhere. Matches ANY -var or TF_VAR_ name, not just 'image' -- confirmed necessary against a real repo (cal-itp/benefits names its variable TF_VAR_CONTAINER_TAG) -- and follows a real terraform command across backslash line-continuations (confirmed against everclearorg/mark). When a build+deploy pair exists but no variable's value matches the built image, reports UNKNOWN rather than a false CLEAR: only on a real, visible signal -- a differently-named tag/image/version/sha/digest/ref variable, or a .tfvars/-var-file reference -- confirmed against two real repos where the image reference is assembled inside Terraform HCL (cal-itp) or a generated tfvars file this play correctly cannot see through (skkuding/codedang). For the first kind of UNKNOWN, this play now also reads the repo's own real .tf files (the same `repo_path` it already has) for that exact variable reaching a container/deploy resource without a digest, marking a match [CONFIRMED] with the HCL line as evidence -- verified live against cal-itp/benefits itself. Still never edits Terraform, confirmed or not -- that fix is a human's call. Validated against 25+ real public repos via GitHub code search, not just synthetic fixtures; that search surfaced and fixed a job-parser bug where a comment line right after `jobs:` silently produced zero detected jobs across the entire file, with no error (confirmed live on skkuding/codedang). Detects the pattern whether build and deploy share one job or are linked by needs:, whether the build step has one tag or several, and whether multiple build+deploy pairs exist in one file (a monorepo -- every pair gets fixed, not just the first). Every fix is minimal: it adds an id: to the build step, an output exposing the digest, and rewires only the one line that named the mutable tag -- nothing else in the file is touched, so the diff a reviewer sees is exactly the change and nothing more. Out of scope, stated plainly rather than silently guessed at: resolving arbitrary shell `$VAR`/`${VAR}` or `${{ env.X }}` references to their literal values, and writing to Terraform/.tf files at all -- both report UNKNOWN (or [CONFIRMED], never auto-fixed) rather than a false CLEAR. No third-party dependencies: pure python3 standard library, so there is nothing to pip install on a machine that may not allow it. Read-only unless open_pr=true, which writes the fix to disk on a new branch and opens a PR through your already-authenticated gh CLI -- no new credential. Pass demo=true to run against bundled fixture workflows with zero setup. Not a daily-habit tool by itself -- an already-fixed repo reports CLEAR every time. Run it once per repo to fix what's there, or copy the GitHub Actions template in this repo's resources/templates/ so it gates every PR touching a workflow file (no Rote needed at CI time); verified end to end with a real git diff across a two-branch repo."
 * source: https://github.com/Satianurag/ci-digest-guard
 * provenance:
 *   author: Satianurag <anuragsati6476@gmail.com>
 *   workspace: satianurag/ci-digest-guard
 * metadata:
 *   version: 0.5.1
 *   rote_version: 0.80.0
 *   status: draft
 *   kind: atomic
 *   flow_type: parallel
 *   execution_model: steps_with_presentation
 *   format: typescript
 *   requires_endpoints: []
 *   requires_sessions: false
 *   write_permissions:
 *   - tool: open_pr
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
 *   description: "Run against five bundled fixture workflows instead of your real repository, for a zero-setup first look. Never writes anything and never opens a PR, even if open_pr=true."
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
 * # Everything this play writes, in the two places the platform actually
 * # reads: metadata.write_permissions (above) is passed through verbatim to
 * # the registry card's effects.declaredWrites, which is what the consent
 * # panel prints before a run; this top-level list is what the audit reach
 * # table renders. Both are empty for the overwhelming majority of the
 * # registry, so an empty panel there means "undeclared", not "read-only".
 * writes:
 * - "<repo>/.github/workflows/*.yml (replace) -- only when open_pr=true and a fix was found: the matched workflow file is rewritten with the minimal fix (id:, outputs:, and the one corrected line) before being committed. Never touched when open_pr=false (the default) or when demo=true."
 * - "local git branch ci-digest-guard/pin-image-digest, and its PR on the configured remote (create) -- only when open_pr=true and a fix was found. Uses the git and gh already configured on this machine; no new credential is requested or stored."
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
  confirmed?: boolean;
  confirmed_evidence?: string | null;
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
          const tag = u.confirmed ? "[CONFIRMED] " : "";
          lines.push(`           · ${tag}job '${u.deploy_job}' (build '${u.build_job}'), line ${u.line}: ${u.reason}`);
          if (u.confirmed && u.confirmed_evidence) {
            lines.push(`             -> ${u.confirmed_evidence}`);
          }
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
    const confirmedCount = scanOut.results.reduce(
      (n, r) => n + r.unknowns.filter((u) => u.confirmed).length,
      0,
    );
    if (confirmedCount > 0) {
      lines.push("");
      lines.push(`  [CONFIRMED] means: this repo's own Terraform code was read directly and`);
      lines.push("  proves the mutable tag really does reach a deployed container resource --");
      lines.push("  not fixed automatically because this play only edits GitHub Actions YAML,");
      lines.push("  never Terraform/.tf files, but the fix belongs on the Terraform side (e.g.");
      lines.push("  accept a full image@digest reference instead of a bare tag).");
    }
  }

  out.human(lines.join("\n"));

  // A discover-step warning (bad/missing repo_path, no workflows
  // directory) means the scan step never actually examined anything --
  // total_files/total_issues/total_unknowns are all zero not because the
  // repo is clean, but because there was nothing to scan in the first
  // place. Confirmed a real false claim of safety: with scanOut.total_
  // issues===0 and total_unknowns===0 both trivially true in that case,
  // this used to render the exact same "0 file(s) — CLEAR" summary (and
  // omit the warning from the JSON result entirely) as a genuinely clean
  // repo, indistinguishable to anyone reading only the summary or json
  // output mode -- only the human view happened to also print the
  // warning as a line. Both modes now say plainly that nothing was
  // scanned, and the warning travels into the canonical result too.
  const noScanOccurred = Boolean(discoverOut.warning) && scanOut.total_files === 0;

  out.summary(
    noScanOccurred
      ? `not scanned — ${discoverOut.warning}`
      : scanOut.total_issues === 0 && scanOut.total_unknowns === 0
      ? `${scanOut.total_files} file(s) — CLEAR${badge}`
      : `${scanOut.total_files} file(s), ${scanOut.total_issues} issue(s) found${unknownNote}${
          openPrOut && !openPrOut.skipped ? " · PR opened" : ""
        }${badge}`,
  );

  out.result({
    schema: "ci-digest-guard/1",
    demo: scanOut.demo,
    path: discoverOut.path,
    warning: discoverOut.warning ?? null,
    scanned: !noScanOccurred,
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
    play_version: "0.5.1",
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
