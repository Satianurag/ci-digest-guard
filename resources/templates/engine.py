#!/usr/bin/env python3
"""v2 extension: adds detection+fix for a RAW `docker build`/`docker
buildx build` shell command (no docker/build-push-action), which needs
different handling because there's no single canonical `outputs.digest`
-- confirmed by testing real docker builds:
  - plain build output shows several different sha256 digests (manifest,
    config, attestation manifest, manifest list); grabbing "the first
    sha256 seen" would silently pick the wrong one.
  - `--metadata-file` is the correct, structured way to get it: its
    `containerimage.digest` field was cross-checked against
    `docker inspect --format '{{.RepoDigests}}'` on a real build and
    matched exactly. Same field name docker/actions-toolkit's own
    resolveDigest() reads internally, so this isn't a different
    mechanism, just the same one without the Action wrapping it.

Everything from v9/engine.py (GH-Action detection, same-job/cross-job,
CLI/env var mutable-tag forms, multi-tag, flow-style YAML, id-collision
safety, byte-stable output on untouched files) is unchanged and reused.
"""
import os
import re
import sys


CONTAINER_RESOURCE_HINT_RE = re.compile(
    r'\bresource\s+"[^"]*(?:container|task_definition|cloud_run|app_service|'
    r'function_app|kubernetes|k8s|helm_release|docker_image|docker_container|'
    r'lambda_function)[^"]*"',
    re.IGNORECASE,
)


def find_hcl_files(repo_path):
    """Every .tf file under repo_path, skipping directories a real repo
    commonly has that are either irrelevant or enormous (a populated
    .terraform/ provider plugin cache can be hundreds of MB of vendored
    .tf-adjacent files that have nothing to do with this repo's own
    infrastructure code)."""
    tf_files = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (".terraform", "node_modules", ".git")]
        for fn in files:
            if fn.endswith(".tf"):
                tf_files.append(os.path.join(root, fn))
    return tf_files


def var_ref_candidates(var_name, var_source):
    """The HCL `var.<name>` reference form(s) a workflow-level variable
    name could appear as inside Terraform code. For a TF_VAR_<X>
    environment variable, Terraform maps it to var.<X> case-sensitively --
    but real-world Terraform variable names are overwhelmingly lower_snake_
    case even when the shell env var naming it is upper-cased (cal-itp/
    benefits' own TF_VAR_CONTAINER_TAG names a variable that is, by
    Terraform convention, almost certainly declared as `container_tag`,
    not `CONTAINER_TAG`) -- so both the exact and lower-cased forms are
    checked. A `-var key=value` CLI flag names the variable directly,
    case-sensitive, with no such ambiguity."""
    if var_source == "env":
        # var_name here is the FULL env var name (tf_var_env_regex's own
        # capture group includes the TF_VAR_ prefix) -- strip it to get
        # the actual Terraform variable name Terraform itself maps it to.
        name = re.sub(r"^TF_VAR_", "", var_name)
        return {f"var.{name}", f"var.{name.lower()}"}
    if var_source == "cli":
        return {f"var.{var_name}"}
    return set()


def hcl_confirms_mutable_image(tf_text, ref_candidates):
    """A real, deliberately conservative text-level check -- not a full
    HCL parser. Terraform's actual grammar (expressions, functions,
    dynamic blocks, module boundaries, for_each) is far beyond what a
    regex can safely claim to fully understand, and this play's own
    stated philosophy is to never report more certainty than it actually
    has. So this only ever looks for one narrow, concrete, low-risk
    signal: a line inside a resource block whose TYPE NAME contains a
    real, recognizable container/deploy keyword, that both interpolates
    one of the candidate `var.<name>` references AND has no `@` (digest
    pin) on that same line.

    Deliberately asymmetric: this can only turn an UNKNOWN into a
    CONFIRMED finding (a real, visible HCL line proving the mutable tag
    reaches a deploy resource) -- it never turns an UNKNOWN into CLEAR.
    Finding no matching line proves nothing (the reference could be
    indirect, inside a module this walk didn't expand, or the resource
    type simply isn't one of the recognized keywords), and a false CLEAR
    is the one claim this entire play refuses to make without certainty."""
    lines = tf_text.splitlines()
    in_block = False
    depth = 0
    for line in lines:
        if not in_block and CONTAINER_RESOURCE_HINT_RE.search(line):
            in_block = True
            depth = line.count("{") - line.count("}")
            continue
        if in_block:
            depth += line.count("{") - line.count("}")
            if "@" not in line and any(ref in line for ref in ref_candidates):
                return line.strip()
            if depth <= 0:
                in_block = False
    return None


def confirm_unknown_via_hcl(unknown, repo_path, tf_files=None):
    """Cross-references one UNKNOWN finding (from find_unknowns) against
    the repo's own real .tf files. Returns the confirming HCL evidence
    line (a string) if found, else None. `tf_files` can be precomputed
    once per scan (find_hcl_files(repo_path)) and passed in to avoid
    re-walking the whole repo tree for every single UNKNOWN in a file."""
    var_name = unknown.get("var_name")
    var_source = unknown.get("var_source")
    if not var_name or not var_source:
        return None
    ref_candidates = var_ref_candidates(var_name, var_source)
    if not ref_candidates:
        return None
    if tf_files is None:
        tf_files = find_hcl_files(repo_path)
    for tf_path in tf_files:
        try:
            with open(tf_path, errors="replace") as f:
                tf_text = f.read()
        except OSError:
            continue
        evidence = hcl_confirms_mutable_image(tf_text, ref_candidates)
        if evidence:
            rel = os.path.relpath(tf_path, repo_path)
            return f"{rel}: {evidence}"
    return None


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def is_blank_or_comment(line):
    s = line.strip()
    return s == "" or s.startswith("#")


def strip_trailing_comment(line):
    """Removes a trailing ` # comment` from a YAML line, but only when the
    `#` is preceded by whitespace (or starts the line) -- the standard
    YAML rule for telling a real comment apart from a literal `#` inside
    an unquoted scalar. Confirmed a real, still-live gap even after the
    "comment right after jobs:" bug was fixed: that fix only handled a
    comment on its OWN line; `jobs:  # all the jobs` and `build:  # docs`
    both carry the comment on the SAME line as content that must still be
    recognized as `jobs:` / a job key, and previously were not."""
    m = re.search(r"(?:^|\s)#.*$", line.rstrip("\n"))
    if not m:
        return line
    return line[:m.start()].rstrip() + "\n"


def scan_jobs(lines):
    """Confirmed real bug found by testing against a real repo
    (skkuding/codedang): a comment line (`  # TODO: ...`) sitting right
    after `jobs:` made the old version give up and return an EMPTY job
    map -- silently, with no error -- because it only skipped blank
    lines, not comments, so it saw the comment, failed to match it as a
    job key, and broke out of the scan entirely. A single stray comment
    anywhere between job entries would have the same effect. Skipping
    comment lines the same way blank lines are already skipped, in both
    the outer scan and the inner per-job body walk, fixes this.

    A trailing (same-line) comment on the `jobs:` line itself, or on a
    job's own key line, is a second, separate case that survived that
    fix -- `jobs:  # all the jobs` still failed to match `^jobs:\\s*$`
    at all, silently returning an empty job map for the WHOLE file, and
    `build:  # builds the image` still broke the scan loop, silently
    losing that job and every job after it. Comments are stripped before
    matching either pattern to close this."""
    jobs_line = None
    for i, l in enumerate(lines):
        if re.match(r"^jobs:\s*$", strip_trailing_comment(l)):
            jobs_line = i
            break
    if jobs_line is None:
        return {}
    job_key_indent = None
    jobs = {}
    i = jobs_line + 1
    while i < len(lines):
        l = lines[i]
        if is_blank_or_comment(l):
            i += 1
            continue
        m = re.match(r'''^(\s+)(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+)):\s*$''', strip_trailing_comment(l))
        if not m:
            break
        indent = len(m.group(1))
        if job_key_indent is None:
            job_key_indent = indent
        elif indent < job_key_indent:
            break
        elif indent > job_key_indent:
            i += 1
            continue
        name = m.group(2) or m.group(3) or m.group(4)  # double-quoted, single-quoted, or bare key
        start = i
        i += 1
        while i < len(lines) and (is_blank_or_comment(lines[i]) or indent_of(lines[i]) > job_key_indent):
            i += 1
        jobs[name] = (start, i, job_key_indent)
    return jobs


def find_line(lines, start, end, pattern):
    for i in range(start, end):
        m = re.search(pattern, lines[i])
        if m:
            return i, m
    return None, None


def repos_from_tags_text(text):
    parts = re.split(r"[\n,]", text)
    repos = set()
    for p in parts:
        p = p.strip()
        if p:
            repos.add(p.rsplit(":", 1)[0] if ":" in p else p)
    return repos


def flow_style_with(lines, build_step_line, job_end):
    idx, m = find_line(lines, build_step_line, job_end, r"^\s*with:\s*\{(.*)\}\s*$")
    if idx is None:
        return None
    body = m.group(1)
    result = {}
    for part in body.split(","):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        result[k.strip()] = v.strip()
    return result


def extract_tags_value(lines, build_step_line, job_end):
    flow = flow_style_with(lines, build_step_line, job_end)
    if flow is not None:
        return flow.get("tags", "")
    idx, m = find_line(lines, build_step_line, job_end, r"^\s*tags:\s*(.*)$")
    if idx is None:
        return ""
    rest = m.group(1).strip()
    if rest and rest != "|":
        return rest
    base_indent = indent_of(lines[idx])
    collected = []
    j = idx + 1
    while j < job_end and (lines[j].strip() == "" or indent_of(lines[j]) > base_indent):
        if lines[j].strip():
            collected.append(lines[j].strip())
        j += 1
    return "\n".join(collected)


def cli_var_regex(repo):
    """Matches ANY -var key name, not just literally 'image' -- confirmed
    necessary by testing against a real repo (cal-itp/benefits uses
    TF_VAR_CONTAINER_TAG, not TF_VAR_IMAGE; a real Terraform module can
    name this variable anything). Captures the key so a fix can reuse the
    module's own name instead of guessing "image".

    Quotes around the key=value pair are OPTIONAL here -- confirmed a
    real gap: `-var image=ghcr.io/acme/api:latest` (no spaces in the
    value, so unquoted is valid and common) previously required a
    quote on both sides and matched nothing, reporting CLEAR on a
    genuinely vulnerable line."""
    escaped_repo = re.escape(repo)
    return re.compile(
        r'''-var[= ]+(?:'''
        r'''["']([a-zA-Z_][a-zA-Z0-9_]*)=''' + escaped_repo + r'''(?::[^"'@]+)?["']'''
        r'''|'''
        r'''([a-zA-Z_][a-zA-Z0-9_]*)=''' + escaped_repo + r'''(?::[^"'@\s]+)?(?=\s|$)'''
        r''')'''
    )


TAGISH_WORDS = {"image", "tag", "digest", "version", "ref", "sha"}


def is_tagish_key(key):
    """Whole-token match on snake_case/camelCase/kebab-case pieces, not a
    plain \\b regex -- confirmed necessary by testing: \\btag\\b does NOT
    match inside CONTAINER_TAG, because '_' counts as a word character so
    there is no boundary before "TAG". CONTAINER_TAG is cal-itp/benefits'
    own real variable name. Splitting on underscore/hyphen/camelCase
    boundaries and matching whole pieces avoids both that miss and
    noisy substring false-positives (a plain substring search would flag
    unrelated names like PREFERENCE or REFRESH_TOKEN on "ref"/"sha")."""
    pieces = re.split(r"[_\-]+|(?<=[a-z0-9])(?=[A-Z])", key)
    return any(p.lower() in TAGISH_WORDS for p in pieces if p)


def tf_var_env_regex():
    """Any TF_VAR_<name>: value line -- name unrestricted, confirmed
    necessary the same way as cli_var_regex above."""
    return re.compile(r"^(\s*)(TF_VAR_[A-Za-z0-9_]+):\s*(\S+)")


def find_build_step(lines, start, end):
    """The docker/build-push-action shape."""
    idx, _ = find_line(lines, start, end, r"uses:\s*docker/build-push-action")
    if idx is None:
        return None
    window_end = min(end, idx + 8)
    flow = flow_style_with(lines, idx, window_end)
    if flow is not None:
        if flow.get("push", "").strip().lower() == "false":
            return None
        return idx
    _, push_m = find_line(lines, idx, window_end, r"^\s*push:\s*(\S+)")
    if push_m and push_m.group(1).strip().lower() == "false":
        return None
    return idx


DOCKER_BUILD_CMD_RE = re.compile(r"\bdocker\s+(?:buildx\s+)?build\b")


def find_raw_build_step(lines, start, end):
    """A `run:` step containing a raw `docker build` / `docker buildx
    build` command line. Returns the step's own line index (the `- run:`
    or `- name:`/`run:` line), or None. Only claims a step that is
    actually pushed (`--push` on the command, or a `docker push` line
    for the same tag elsewhere in the same run block) -- an unpushed
    local build has nothing for terraform to deploy anyway."""
    for i in range(start, end):
        if not DOCKER_BUILD_CMD_RE.search(lines[i]):
            continue
        step_line = find_owning_run_step(lines, i, start)
        if step_line is None:
            continue
        return step_line, i
    return None


def find_owning_run_step(lines, cmd_line_idx, job_start):
    """Walk back from a line inside a run: block to the `- run:` (or
    preceding `- id:`/`- name:`) line that starts this step."""
    for i in range(cmd_line_idx, job_start - 1, -1):
        if re.match(r"^\s*-\s*(id:|name:|run:|uses:)", lines[i]):
            return i
    return None


def find_step_start(lines, content_line_idx, max_back=15):
    """Walk back from ANY line inside a step to the actual `- ` line that
    starts it, regardless of what key (if any) is written on that same
    line. Confirmed necessary: a step is very commonly written as

        - name: Build and push
          uses: docker/build-push-action@v6

    -- literally how docker/build-push-action's own README shows it --
    where `uses:` is not on the dash line at all. Code that assumed the
    line matching `uses:\\s*docker/build-push-action` (or any other
    single key) WAS the dash line crashed outright (a regex expecting a
    leading `-` matched nothing, and `.group()` on that None crashed) or,
    once guarded against crashing, computed a wrong insertion column
    that produced invalid YAML. find_owning_run_step already handled
    this for one caller by requiring a specific key on the dash line;
    this is the same walk, generalized to accept ANY `-`-prefixed line
    as a valid step boundary, since a bare `- name: ...` line with
    nothing else on it is just as valid a step start."""
    limit = max(0, content_line_idx - max_back)
    for i in range(content_line_idx, limit - 1, -1):
        if re.match(r"^\s*-\s", lines[i]):
            return i
    return content_line_idx


def build_command_block(lines, cmd_line, limit):
    """The line range covering one logical shell command starting at
    cmd_line, following `\\` line-continuations. Confirmed necessary: a
    completely standard, common way to write a buildx invocation splits
    its flags across continued lines for readability --
        docker buildx build \\
          --push \\
          -t repo:tag \\
          .
    -- and treating cmd_line as the command's only line silently missed
    both --tag and --push whenever they landed on a continuation line,
    reporting CLEAR for a genuinely vulnerable, unmodified pipeline."""
    end = cmd_line
    while end < limit - 1 and lines[end].rstrip().endswith("\\"):
        end += 1
    return cmd_line, end + 1


def raw_build_extract_tags(lines, build_cmd_line, step_end):
    """-t/--tag repo:tag, possibly repeated, scanned across the build
    command's full continuation block (see build_command_block)."""
    bstart, bend = build_command_block(lines, build_cmd_line, step_end)
    text = "".join(lines[bstart:bend])
    tags = re.findall(r'(?:-t|--tag)[= ]+["\']?([^\s"\']+)', text)
    return set(t.rsplit(":", 1)[0] if ":" in t else t for t in tags)


def raw_build_is_pushed(lines, step_line, job_end):
    end = job_end
    # scope the search to just this step's own block (up to the next `- ` step)
    for i in range(step_line + 1, job_end):
        if re.match(r"^\s*-\s", lines[i]) and indent_of(lines[i]) <= indent_of(lines[step_line]):
            end = i
            break
    return bool(find_line(lines, step_line, end, r"--push\b")[0] is not None
                or find_line(lines, step_line, end, r"\bdocker\s+push\b")[0] is not None), end


def raw_build_pushes_itself(lines, build_cmd_line, step_end):
    """True only when `--push` is part of the SAME logical build command
    (its own continuation block, per build_command_block) -- the one
    configuration where --metadata-file's containerimage.digest key is
    guaranteed to exist and to actually correspond to the image that got
    pushed.

    `--push` alone is the correct signal, not literally requiring the
    word "buildx" in the command. Confirmed directly against Docker's own
    current reference docs, not assumed: docs.docker.com/reference/cli/
    docker/build/ -- the page for plain `docker build`, not `docker
    buildx build` -- lists both --push and --metadata-file as supported
    flags. Modern Docker (Engine 23+) makes `docker build` a BuildKit/
    buildx-backed command by default, so a bare `docker build --push ...`
    with no literal "buildx" anywhere is exactly the buildx-backed shape,
    and this play's own bundled raw-buildx-example.yml fixture is written
    exactly that way. --push itself does not exist in the legacy,
    non-BuildKit builder at all, so its presence is sufficient proof on
    its own.

    The remaining real gap this guards against: a --load (or otherwise
    unpushed) build's metadata file has no containerimage.digest key at
    all, confirmed against buildx's own docs and community bug reports --
    --load stores to the local daemon, which never talks to a registry,
    so no registry digest is ever generated for buildx to record. A
    separate, later `docker push` of that loaded image pushes fine, but
    buildx never sees or records THAT digest -- so even though the job
    clearly does end up publishing an image, injecting our extraction
    there would either KeyError on a real, successful build, or (worse)
    silently pin a digest that doesn't match what was actually pushed.
    That case is left for find_unknowns to flag explicitly rather than
    being silently mis-fixed or silently ignored."""
    bstart, bend = build_command_block(lines, build_cmd_line, step_end)
    block_text = "".join(lines[bstart:bend])
    return bool(re.search(r"--push\b", block_text))


def raw_build_metadata_file_path(lines, step_line, step_end):
    """The --metadata-file argument's value if present, else None."""
    idx, m = find_line(lines, step_line, step_end, r"--metadata-file[= ]+(\S+)")
    return m.group(1) if m else None


TERRAFORM_CMD_RE = re.compile(r"\b(?:terraform|terragrunt|tofu)\s+(?:apply|plan)\b")


def find_all_terraform_commands(lines, start, end):
    """Every terraform/terragrunt/tofu apply-or-plan line in [start, end),
    not just the first. Confirmed a real gap: a job that runs
    `terraform plan` before `terraform apply` (a completely standard,
    common CI shape) previously stopped at the plan line -- which never
    carries the real -var flags a deploy actually uses -- and reported
    CLEAR without ever looking at the apply line one step later. Also
    recognizes terragrunt and tofu (OpenTofu), Terraform-compatible CLIs
    with the identical apply/plan/-var contract, previously invisible
    to this engine entirely."""
    return [i for i in range(start, end) if TERRAFORM_CMD_RE.search(lines[i])]


def terraform_command_block(lines, tf_idx, end):
    """A real `terraform apply \\` command frequently continues onto
    following lines with backslash line-continuation, each carrying one
    -var flag -- confirmed by testing against a real repo
    (everclearorg/mark) where checking only the `terraform apply` line
    itself missed every -var, all of which were one line down. Returns
    the inclusive line range [tf_idx, block_end) covering the whole
    logical command."""
    i = tf_idx
    while i < end - 1 and lines[i].rstrip("\n").rstrip().endswith("\\"):
        i += 1
    return tf_idx, i + 1


def find_mutable_ref(lines, start, end, repos):
    # Every terraform/terragrunt/tofu command in the job, not just the
    # first -- a `plan` before the real `apply` (a completely standard
    # shape) must not shadow the apply line's own -var flags.
    for tf_idx in find_all_terraform_commands(lines, start, end):
        block_start, block_end = terraform_command_block(lines, tf_idx, end)
        for i in range(block_start, block_end):
            for repo in repos:
                m = cli_var_regex(repo).search(lines[i])
                if m and "@" not in m.group(0):
                    return ("cli", i, repo)
    for i in range(start, end):
        m = tf_var_env_regex().match(lines[i])
        if m:
            val = m.group(3)
            for repo in repos:
                if val.startswith(repo + ":") and "@" not in val:
                    return ("env", i, repo)
    return None


def find_all_build_steps(lines, start, end):
    """Every build+push step in a job -- both the docker/build-push-action
    shape and the raw docker/buildx build shape, and every OCCURRENCE of
    each, not just the first. Confirmed a real, silent bug: a job
    building two images in two separate steps (frontend + backend, a
    completely standard multi-image pattern) only ever had its FIRST
    build step examined. Once that one was fixed, re-scanning the job
    still located that same first step (find_build_step/
    find_raw_build_step both only ever returned the first match), found
    no more mutable references FOR THAT IMAGE, and moved on to the next
    job entirely -- so the second image's deploy was left on its
    original mutable tag, with no finding and no UNKNOWN, while the
    overall result still reported full success. Returns a list of
    (build_kind, build_step_line, build_cmd_line) tuples, build_cmd_line
    is None for the action kind."""
    steps = []
    i = start
    while i < end:
        idx, _ = find_line(lines, i, end, r"uses:\s*docker/build-push-action")
        if idx is None:
            break
        window_end = min(end, idx + 8)
        flow = flow_style_with(lines, idx, window_end)
        pushed = True
        if flow is not None:
            if flow.get("push", "").strip().lower() == "false":
                pushed = False
        else:
            _, push_m = find_line(lines, idx, window_end, r"^\s*push:\s*(\S+)")
            if push_m and push_m.group(1).strip().lower() == "false":
                pushed = False
        if pushed:
            steps.append(("action", idx, None))
        i = idx + 1

    i = start
    while i < end:
        cmd_idx = None
        for j in range(i, end):
            if DOCKER_BUILD_CMD_RE.search(lines[j]):
                cmd_idx = j
                break
        if cmd_idx is None:
            break
        step_line = find_owning_run_step(lines, cmd_idx, start)
        if step_line is not None:
            steps.append(("raw", step_line, cmd_idx))
        i = cmd_idx + 1
    return steps


def find_one(lines):
    jobs = scan_jobs(lines)
    for build_job, (jstart, jend, jindent) in jobs.items():
        for build_kind, build_step_line, build_cmd_line in find_all_build_steps(lines, jstart, jend):
            if build_kind == "action":
                tags_text = extract_tags_value(lines, build_step_line, jend)
                repos = repos_from_tags_text(tags_text)
                existing_meta_file = None
            else:
                pushed, step_end = raw_build_is_pushed(lines, build_step_line, jend)
                if not pushed:
                    continue
                if not raw_build_pushes_itself(lines, build_cmd_line, step_end):
                    # Publishes an image, but not in a form --metadata-file
                    # can safely pin (classic `docker build`, or `--load`
                    # followed by a separate `docker push`) -- see
                    # raw_build_pushes_itself for why. find_unknowns still
                    # flags a real usage of this image in a linked terraform
                    # job explicitly, rather than this silently producing no
                    # finding at all.
                    continue
                existing_meta_file = raw_build_metadata_file_path(lines, build_step_line, step_end)
                repos = raw_build_extract_tags(lines, build_cmd_line, step_end)
            if not repos:
                continue

            same_hit = find_mutable_ref(lines, jstart, jend, repos)
            if same_hit:
                kind, line_idx, repo = same_hit
                return {"scope": "same-job", "kind": kind, "line": line_idx,
                        "build_job": build_job, "deploy_job": build_job,
                        "build_step_line": build_step_line, "build_cmd_line": build_cmd_line,
                        "build_kind": build_kind, "image_repo": repo,
                        "existing_meta_file": existing_meta_file}

            for other_job, (ostart, oend, oindent) in jobs.items():
                if other_job == build_job:
                    continue
                needs_idx, needs_m = find_line(lines, ostart, oend, r"^\s*needs:\s*(.*)$")
                depends = needs_idx is not None and build_job in needs_m.group(1)
                if not depends:
                    m2, _ = find_line(lines, ostart, min(oend, ostart + 4), r"^\s*-\s*" + re.escape(build_job) + r"\s*$")
                    depends = m2 is not None
                if not depends:
                    continue
                hit = find_mutable_ref(lines, ostart, oend, repos)
                if hit:
                    kind, line_idx, repo = hit
                    return {"scope": "cross-job", "kind": kind, "line": line_idx,
                            "build_job": build_job, "deploy_job": other_job,
                            "build_step_line": build_step_line, "build_cmd_line": build_cmd_line,
                            "build_kind": build_kind, "image_repo": repo,
                            "existing_meta_file": existing_meta_file}
    return None


def taken_step_ids(lines):
    ids = set()
    for l in lines:
        m = re.match(r"^\s*-?\s*id:\s*(\S+)", l)
        if m:
            ids.add(m.group(1))
    return ids


def fresh_step_id(lines, base="push"):
    taken = taken_step_ids(lines)
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}{n}"
        n += 1
    return candidate


def apply_one_fix_action(lines, f):
    """`f["build_step_line"]` is wherever `uses: docker/build-push-action`
    was found -- NOT necessarily the step's `- ` line. Confirmed a real
    crash: a step written as

        - name: Build and push
          uses: docker/build-push-action@v6

    (literally how docker/build-push-action's own README shows it) put
    `uses:` one line below the dash. The old code assumed uses_line WAS
    the dash line and matched `^(\\s*)(-)(\\s*)uses:` against it directly,
    which fails on `name:` and crashed on `.group()`. find_step_start
    resolves the real step boundary first; an existing id: is looked for
    anywhere between that boundary and the uses: line (it can be on the
    dash line, or its own line), and a new id: is inserted as a sibling
    of uses: (matching its own indent) rather than by rewriting the dash
    line, which works identically whether uses: is on the dash line or
    several lines below it."""
    uses_line = f["build_step_line"]
    dash_line = find_step_start(lines, uses_line)

    existing_id_m = None
    for i in range(dash_line, uses_line + 1):
        m = re.match(r"^\s*-?\s*id:\s*(\S+)", lines[i])
        if m:
            existing_id_m = m
            break

    if existing_id_m:
        step_id = existing_id_m.group(1)
    else:
        step_id = fresh_step_id(lines)
        if dash_line == uses_line:
            m = re.match(r"^(\s*)(-)(\s*)uses:(.*)$", lines[uses_line])
            dash_indent, dash, gap, rest = m.group(1), m.group(2), m.group(3), m.group(4)
            lines[uses_line] = f"{dash_indent}{dash}{gap}id: {step_id}\n"
            lines.insert(uses_line + 1, f"{dash_indent}  uses:{rest}\n")
        else:
            sibling_indent = " " * indent_of(lines[uses_line])
            lines.insert(uses_line, f"{sibling_indent}id: {step_id}\n")
    return lines, step_id, f"${{{{ steps.{step_id}.outputs.digest }}}}"


def apply_one_fix_raw(lines, f):
    """Inject --metadata-file into the build command, then append an
    extraction+export line to the same run: block. Handles both a
    single-line `run: docker build ...` and a `run: |` block."""
    step_line = f["build_step_line"]  # the step's own dash line
    cmd_line = f["build_cmd_line"]     # the line carrying the docker build command itself

    if f.get("existing_meta_file"):
        # --metadata-file is already on the command; its digest is just
        # never read anywhere. Reuse that path -- adding a second
        # --metadata-file flag would be redundant and confusing in the
        # generated diff.
        meta_file = f["existing_meta_file"]
    else:
        meta_file = "ci-digest-guard-metadata.json"
        lines[cmd_line] = re.sub(
            DOCKER_BUILD_CMD_RE,
            lambda m: m.group(0) + f" --metadata-file {meta_file}",
            lines[cmd_line],
            count=1,
        )

    extract_expr = (
        f'python3 -c "import json;print(json.load(open(\'{meta_file}\'))'
        f"['containerimage.digest'])\""
    )

    # The `run:` line is not always step_line and not always cmd_line --
    # confirmed a real corruption: a step written as
    #     - id: push
    #       name: Build
    #       run: docker buildx build --push -t repo:latest .
    # has `run:` on neither the dash line nor (trivially) matching the
    # old "is this the dash line" check, so the old code fell through to
    # a fallback that inserted the export line as a SIBLING MAPPING KEY
    # at run:'s own column -- not a continuation of its value -- which is
    # invalid YAML, written straight to the file and committed. The
    # actual `run:` line is found by walking back from cmd_line.
    run_line = None
    for i in range(cmd_line, step_line - 1, -1):
        if re.match(r"^\s*-?\s*run:", lines[i]):
            run_line = i
            break
    if run_line is None:
        raise ValueError("ci-digest-guard: could not locate the run: line for a raw docker build step")

    run_m = re.match(r"^(\s*)(-)?(\s*)run:[ \t]*(\|)?[ \t]*(.*)$", lines[run_line])
    dash_indent, dash, gap, block_marker, rest = run_m.groups()
    dash = dash or ""
    is_block = bool(block_marker) and not rest.strip()

    if is_block:
        # Already `run: |`, wherever it started -- append after the block
        # ends, at the block body's own existing indent.
        run_key_indent = indent_of(lines[cmd_line])
        j = cmd_line + 1
        while j < len(lines) and (lines[j].strip() == "" or indent_of(lines[j]) >= run_key_indent):
            j += 1
        insertion_point = j
    else:
        # Single-line run: value -- convert to a block scalar IN PLACE,
        # regardless of whether run_line is the step's dash line or a
        # separate line under a `- id:`/`- name:` header. Confirmed
        # necessary: only handling the "run: on the dash line" case here
        # previously left this shape unconverted, producing the
        # sibling-key corruption described above.
        run_key_col = len(dash_indent) + len(dash) + len(gap)
        body_indent = run_key_col + 2
        lines[run_line] = f"{dash_indent}{dash}{gap}run: |\n"
        lines.insert(run_line + 1, " " * body_indent + rest + "\n")
        run_key_indent = body_indent
        insertion_point = run_line + 2
        if cmd_line >= run_line:
            cmd_line += 1  # shifted down by the inserted block-body line

    # An existing id: can be on the step's own dash line, or its own
    # separate line anywhere between the step's start and run: -- checked
    # across that whole span, not just two hardcoded positions (which
    # previously missed a real `- id: push` / `name: Build` / `run: ...`
    # shape entirely and inserted a SECOND, duplicate id: line).
    existing_id_m = None
    for i in range(step_line, run_line + 1):
        m = re.match(r"^\s*-?\s*id:\s*(\S+)", lines[i])
        if m:
            existing_id_m = m
            break

    if existing_id_m:
        step_id = existing_id_m.group(1)
    else:
        step_id = fresh_step_id(lines)
        if step_line == run_line:
            # No separate dash line: `- run: ...` (now `- run: |`) is the
            # step's only line -- split id: onto the dash, same pattern
            # apply_one_fix_action uses for `- uses:`.
            m = re.match(r"^(\s*)-(\s*)", lines[step_line])
            dash_indent2, gap2 = m.group(1), m.group(2)
            rest_of_line = lines[step_line][m.end():]
            lines[step_line] = f"{dash_indent2}-{gap2}id: {step_id}\n"
            lines.insert(step_line + 1, f"{dash_indent2} {gap2}{rest_of_line}")
            insertion_point += 1
            cmd_line += 1
            run_line += 1
        else:
            # A separate dash line already exists (`- id: x` / `- name: x`)
            # -- insert id: as a sibling at run:'s own indent, rather than
            # rewriting the dash line at all.
            sibling_indent = " " * indent_of(lines[run_line])
            lines.insert(run_line, f"{sibling_indent}id: {step_id}\n")
            insertion_point += 1
            cmd_line += 1
            run_line += 1

    # A build job feeding TWO deploy jobs gets this function called once per
    # deploy job with the same step_line/run_line -- confirmed by testing
    # (the same two-deploy-job shape as the outputs: duplicate-key bug
    # above): the second call found the id: already inserted and correctly
    # reused it, but unconditionally appended a SECOND, byte-identical
    # export line into the run: block regardless, running the same
    # extraction command twice on every build for no reason. Only append
    # it if this run: block doesn't already export image_digest.
    already_exported = any(
        "image_digest=" in lines[i] and "GITHUB_OUTPUT" in lines[i]
        for i in range(run_line, insertion_point)
    )
    if not already_exported:
        export_line = (
            " " * run_key_indent
            + 'echo "image_digest=$(' + extract_expr + ')" >> "$GITHUB_OUTPUT"\n'
        )
        lines.insert(insertion_point, export_line)

    return lines, step_id, f"${{{{ steps.{step_id}.outputs.image_digest }}}}"


def apply_one_fix(lines, f):
    lines = list(lines)

    if f["build_kind"] == "action":
        lines, step_id, digest_expr_same_job = apply_one_fix_action(lines, f)
    else:
        lines, step_id, digest_expr_same_job = apply_one_fix_raw(lines, f)

    jobs = scan_jobs(lines)
    if f["scope"] == "same-job":
        jstart, jend, _ = jobs[f["build_job"]]
        kind_line_repo = find_mutable_ref(lines, jstart, jend, {f["image_repo"]})
        digest_ref = digest_expr_same_job
    else:
        ostart, oend, _ = jobs[f["deploy_job"]]
        kind_line_repo = find_mutable_ref(lines, ostart, oend, {f["image_repo"]})
        # Keyed by step_id, not a fixed literal -- confirmed a real, wrong
        # fix: a job building TWO images in two separate build steps
        # (frontend + backend, a standard multi-image pattern) got each
        # step assigned its own step_id already, but both cross-job fixes
        # previously wrote to the SAME literal "image_digest" job output.
        # The second insert was then skipped by the already-output guard
        # below (correctly avoiding a duplicate key), which meant BOTH
        # deploy vars silently ended up pointing at the FIRST build step's
        # digest -- backend_image would have deployed the frontend image's
        # digest. Each build step's own step_id keeps these distinct.
        output_name = f"{step_id}_digest"
        digest_ref = f'${{{{ needs.{f["build_job"]}.outputs.{output_name} }}}}'

        jstart, jend, jindent = jobs[f["build_job"]]
        steps_idx, _ = find_line(lines, jstart, jend, r"^\s*steps:\s*$")
        out_idx, _ = find_line(lines, jstart, jend, r"^\s*outputs:\s*$")
        body_indent = jindent + 2
        # A build job feeding TWO deploy jobs (staging + prod, a common
        # shape) gets apply_one_fix called once per deploy job, and each
        # call previously inserted its OWN `image_digest:` output line
        # unconditionally -- two identical mapping keys in one `outputs:`
        # block, which GitHub Actions' own YAML parser rejects outright
        # (a workflow that fails to load at all, not merely a wrong fix).
        # Confirmed by reproducing exactly this two-deploy-job shape.
        # Reuse an existing image_digest: output instead of duplicating it.
        already_output = False
        if out_idx is not None:
            out_body_indent = indent_of(lines[out_idx]) + 2
            j = out_idx + 1
            while j < jend and (lines[j].strip() == "" or indent_of(lines[j]) >= out_body_indent):
                if re.match(r"^\s*" + re.escape(output_name) + r":", lines[j]):
                    already_output = True
                    break
                j += 1
        if already_output:
            pass  # already wired up by an earlier fix in this same run -- nothing to insert
        elif out_idx is None:
            lines[steps_idx:steps_idx] = [
                " " * body_indent + "outputs:\n",
                " " * (body_indent + 2) + f"{output_name}: {digest_expr_same_job}\n",
            ]
        else:
            lines.insert(out_idx + 1, " " * (body_indent + 2) + f"{output_name}: {digest_expr_same_job}\n")
        jobs = scan_jobs(lines)
        ostart, oend, _ = jobs[f["deploy_job"]]
        kind_line_repo = find_mutable_ref(lines, ostart, oend, {f["image_repo"]})

    kind, line_idx, repo = kind_line_repo
    new_image = f"{repo}@{digest_ref}"
    if kind == "cli":
        m = cli_var_regex(repo).search(lines[line_idx])
        # group(1) matched the quoted alternative, group(2) the unquoted
        # one -- exactly one is populated depending on which the line used.
        var_key = m.group(1) or m.group(2)  # reuse whatever name the module's own -var used
        sep = "=" if re.match(r"-var=", m.group(0)) else " "
        # Always emit double-quoted, regardless of the original quoting
        # (single, double, or none): the digest expression this produces
        # (repo@${{ steps.x.outputs.digest }}) is always safe double-quoted
        # shell/Terraform CLI syntax, so there is no need to preserve an
        # unquoted original -- and quoting it is strictly safer.
        replacement = f'-var{sep}"{var_key}={new_image}"'
        lines[line_idx] = lines[line_idx][:m.start()] + replacement + lines[line_idx][m.end():]
    else:
        m = tf_var_env_regex().match(lines[line_idx])
        lines[line_idx] = f"{m.group(1)}{m.group(2)}: {new_image}\n"

    return lines


def fix_all(lines, max_iterations=20):
    lines = list(lines)
    findings_applied = []
    for _ in range(max_iterations):
        f = find_one(lines)
        if f is None:
            break
        findings_applied.append(f)
        lines = apply_one_fix(lines, f)
    return lines, findings_applied


TFVARS_FILE_RE = re.compile(r"(?:>>\s*\S*\.?tfvars\b|-var-file[= ])")


def find_reusable_workflow_call(lines, jstart, jend, jindent):
    """A job written as a call to a reusable workflow (`uses:` at the
    JOB's own body indent -- GitHub Actions' reusable-workflow-call
    syntax -- rather than a `steps:` list) has no steps: for
    find_build_step/find_raw_build_step to ever find, by design; that is
    not itself a bug (a *local* `./.github/workflows/x.yml` reusable
    workflow lives in the same directory discover.py already globs, so it
    gets scanned and fixed as its own file independently). The one real
    gap is an EXTERNAL reusable workflow (`owner/repo/.github/workflows/
    x.yml@ref`) -- genuinely unreachable from this file. Returns
    (ref, with_block_text) if this job calls one, else None."""
    body_indent = jindent + 2
    idx, m = find_line(lines, jstart, jend, r"^\s{" + str(body_indent) + r"}uses:\s*(\S+)")
    if idx is None:
        return None
    with_idx, _ = find_line(lines, jstart, jend, r"^\s{" + str(body_indent) + r"}with:\s*$")
    with_text = ""
    if with_idx is not None:
        j = with_idx + 1
        while j < jend and (lines[j].strip() == "" or indent_of(lines[j]) > body_indent):
            with_text += lines[j]
            j += 1
    return m.group(1), with_text


def find_unknowns(lines):
    """Runs AFTER fix_all has exhausted every fixable finding. Looks for
    a build+push step linked to a terraform apply/plan where no var value
    could be matched to the built image at all -- but some real signal
    suggests image/tag identity IS reaching terraform some way we cannot
    verify from this file alone (a differently-named tag/image/version/
    sha/digest/ref variable whose value doesn't match, or a .tfvars/
    -var-file reference). Confirmed necessary against two real repos:
    cal-itp/benefits passes only a bare `${{ github.sha }}` tag through
    TF_VAR_CONTAINER_TAG while the registry/repo is assembled inside
    Terraform HCL we cannot see; skkuding/codedang builds a full
    terraform.tfvars file from a GitHub Secret, which is correctly opaque
    -- reporting CLEAR in either case would be a false claim of safety
    neither this engine nor anyone reading only the workflow YAML can
    actually back up. This is intentionally conservative: it only fires
    on a real, visible signal, never merely because docker and terraform
    both appear somewhere in the same file."""
    jobs = scan_jobs(lines)
    build_jobs = set()
    for job, (jstart, jend, jindent) in jobs.items():
        if find_build_step(lines, jstart, jend) is not None:
            build_jobs.add(job)
        elif find_raw_build_step(lines, jstart, jend) is not None:
            raw = find_raw_build_step(lines, jstart, jend)
            pushed, _ = raw_build_is_pushed(lines, raw[0], jend)
            if pushed:
                build_jobs.add(job)

    findings = []

    # External reusable-workflow calls: a real, visible signal (a tag-ish
    # `with:` input) that image identity crosses into a file this engine
    # cannot see at all -- checked unconditionally, not gated on
    # build_jobs being non-empty, since a caller job legitimately has no
    # inline build step of its own to find (the reusable workflow does
    # the whole build+deploy internally).
    for job, (jstart, jend, jindent) in jobs.items():
        reusable = find_reusable_workflow_call(lines, jstart, jend, jindent)
        if reusable is None:
            continue
        ref, with_text = reusable
        if ref.startswith("./") or ref.startswith("../"):
            continue  # local -- discover.py's own glob scans it as its own file already
        for w_line in with_text.splitlines():
            m = re.match(r"^\s*([a-zA-Z_][a-zA-Z0-9_-]*):\s*(.+)$", w_line)
            if m and is_tagish_key(m.group(1)) and "@" not in m.group(2):
                findings.append({
                    "build_job": job, "deploy_job": job, "line": jstart + 1,
                    "reason": f"job calls external reusable workflow {ref} passing "
                              f"{m.group(1)}={m.group(2).strip()} -- cannot verify how that "
                              f"value is used inside a workflow that lives in another repository",
                })
                break

    if not build_jobs:
        return findings

    for job, (jstart, jend, jindent) in jobs.items():
        tf_indices = find_all_terraform_commands(lines, jstart, jend)
        if not tf_indices:
            continue

        linked_build_job = job if job in build_jobs else None
        if linked_build_job is None:
            needs_idx, needs_m = find_line(lines, jstart, jend, r"^\s*needs:\s*(.*)$")
            for bj in build_jobs:
                if needs_idx is not None and bj in needs_m.group(1):
                    linked_build_job = bj
                    break
        if linked_build_job is None:
            continue

        # Union of every terraform/terragrunt/tofu command's own block in
        # this job (e.g. a `plan` immediately before the real `apply`),
        # not just the first -- same reason find_mutable_ref checks every
        # occurrence: a signal living only in the second command must not
        # be invisible just because an earlier one was checked first.
        block_start = min(terraform_command_block(lines, i, jend)[0] for i in tf_indices)
        block_end = max(terraform_command_block(lines, i, jend)[1] for i in tf_indices)
        block_text = "".join(lines[block_start:block_end])
        if "@sha256:" in block_text:
            continue  # already looks digest-pinned in the visible command

        signal = None
        for i in range(jstart, jend):
            m = tf_var_env_regex().match(lines[i])
            if m and is_tagish_key(m.group(2)) and "@" not in m.group(3):
                signal = (i, f"env var {m.group(2)} looks image/tag-related "
                              f"but its value doesn't match the image this job builds",
                          m.group(2), "env")
                break
        if signal is None:
            for i in range(block_start, block_end):
                # Quotes optional here too, matching cli_var_regex's own fix:
                # an unquoted `-var key=value` is valid and common.
                m = re.search(
                    r'''-var[= ]+(?:["']([a-zA-Z_][a-zA-Z0-9_]*)=([^"']*)["']'''
                    r'''|([a-zA-Z_][a-zA-Z0-9_]*)=(\S+))''',
                    lines[i],
                )
                if m:
                    key = m.group(1) or m.group(3)
                    val = m.group(2) if m.group(1) else m.group(4)
                    if is_tagish_key(key) and "@" not in val:
                        signal = (i, f"-var {key} looks image/tag-related "
                                     f"but its value doesn't match the image this job builds",
                                  key, "cli")
                        break
        if signal is None:
            for i in range(jstart, jend):
                if TFVARS_FILE_RE.search(lines[i]):
                    signal = (i, "a .tfvars file is written or referenced here -- "
                                 "the actual image reference may be set there", None, None)
                    break

        if signal:
            line_idx, reason, var_name, var_source = signal
            findings.append({"build_job": linked_build_job, "deploy_job": job,
                              "line": line_idx + 1, "reason": reason,
                              "var_name": var_name, "var_source": var_source})
    return findings


if __name__ == "__main__":
    for path in sys.argv[1:]:
        try:
            with open(path) as fh:
                lines = fh.readlines()
        except Exception as e:
            print(f"SKIPPED {path}: could not read ({e})")
            continue
        try:
            fixed_lines, findings = fix_all(lines)
            unknowns = find_unknowns(fixed_lines)
        except Exception as e:
            print(f"SKIPPED {path}: could not analyze/fix ({e.__class__.__name__}: {e})")
            continue

        if not findings and not unknowns:
            print(f"CLEAR {path}: no mutable-tag-into-terraform pattern detected")
            continue

        for f in findings:
            scope_desc = "same job" if f["scope"] == "same-job" else f"job '{f['deploy_job']}' (needs build job '{f['build_job']}')"
            print(f"FOUND {path} [{f['build_kind']}/{f['kind']}, {scope_desc}]: terraform deploys "
                  f"{f['image_repo']}:<mutable tag> instead of the digest the build step already produced")
        if findings:
            out_path = path + ".v2fixed"
            open(out_path, "w").writelines(fixed_lines)
            print(f"  -> {len(findings)} issue(s) fixed, wrote {out_path}")

        for u in unknowns:
            print(f"UNKNOWN {path} [job '{u['deploy_job']}' (build job '{u['build_job']}'), line {u['line']}]: "
                  f"{u['reason']} -- cannot verify safe or unsafe from this workflow file alone")
