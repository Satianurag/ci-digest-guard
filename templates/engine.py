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
import re
import sys


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def is_blank_or_comment(line):
    s = line.strip()
    return s == "" or s.startswith("#")


def scan_jobs(lines):
    """Confirmed real bug found by testing against a real repo
    (skkuding/codedang): a comment line (`  # TODO: ...`) sitting right
    after `jobs:` made the old version give up and return an EMPTY job
    map -- silently, with no error -- because it only skipped blank
    lines, not comments, so it saw the comment, failed to match it as a
    job key, and broke out of the scan entirely. A single stray comment
    anywhere between job entries would have the same effect. Skipping
    comment lines the same way blank lines are already skipped, in both
    the outer scan and the inner per-job body walk, fixes this."""
    jobs_line = None
    for i, l in enumerate(lines):
        if re.match(r"^jobs:\s*$", l):
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
        m = re.match(r"^(\s+)([A-Za-z0-9_.-]+):\s*$", l)
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
        name = m.group(2)
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
    module's own name instead of guessing "image"."""
    return re.compile(
        r'''-var[= ]+["']([a-zA-Z_][a-zA-Z0-9_]*)=''' + re.escape(repo) + r'''(?::[^"'@]+)?["']'''
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


def raw_build_extract_tags(lines, build_cmd_line):
    """-t/--tag repo:tag, possibly repeated."""
    line = lines[build_cmd_line]
    tags = re.findall(r'(?:-t|--tag)[= ]+["\']?([^\s"\']+)', line)
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


def raw_build_metadata_file_path(lines, step_line, step_end):
    """The --metadata-file argument's value if present, else None."""
    idx, m = find_line(lines, step_line, step_end, r"--metadata-file[= ]+(\S+)")
    return m.group(1) if m else None


def raw_build_digest_already_read(lines, step_line, step_end):
    """Presence of --metadata-file alone is NOT enough -- confirmed by
    testing: a build with --metadata-file whose digest is never actually
    extracted or referenced still deploys the mutable tag untouched.
    Only treat the digest as genuinely wired up if 'containerimage.digest'
    (the field name the file actually carries) shows up somewhere in the
    same job -- i.e. someone is reading it."""
    return find_line(lines, step_line, step_end, r"containerimage\.digest")[0] is not None


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
    tf_idx, _ = find_line(lines, start, end, r"terraform (apply|plan)")
    if tf_idx is not None:
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


def find_one(lines):
    jobs = scan_jobs(lines)
    for build_job, (jstart, jend, jindent) in jobs.items():
        # 1. docker/build-push-action shape (existing, unchanged)
        build_step_line = find_build_step(lines, jstart, jend)
        if build_step_line is not None:
            tags_text = extract_tags_value(lines, build_step_line, jend)
            repos = repos_from_tags_text(tags_text)
            build_kind = "action"
            build_cmd_line = None
            existing_meta_file = None
        else:
            # 2. raw `docker build`/`docker buildx build` shape
            raw = find_raw_build_step(lines, jstart, jend)
            if raw is None:
                continue
            build_step_line, build_cmd_line = raw
            pushed, step_end = raw_build_is_pushed(lines, build_step_line, jend)
            if not pushed:
                continue
            existing_meta_file = raw_build_metadata_file_path(lines, build_step_line, step_end)
            if existing_meta_file and raw_build_digest_already_read(lines, jstart, jend):
                continue  # metadata-file present AND its digest is actually read -- already fine
            repos = raw_build_extract_tags(lines, build_cmd_line)
            build_kind = "raw"
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
    step_line = f["build_step_line"]
    id_window = lines[max(0, step_line - 1): step_line + 1]
    existing_id_m = next((re.match(r"^\s*-?\s*id:\s*(\S+)", l) for l in id_window if re.match(r"^\s*-?\s*id:\s*(\S+)", l)), None)
    if existing_id_m:
        step_id = existing_id_m.group(1)
    else:
        step_id = fresh_step_id(lines)
        m = re.match(r"^(\s*)(-)(\s*)uses:(.*)$", lines[step_line])
        dash_indent, dash, gap, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        lines[step_line] = f"{dash_indent}{dash}{gap}id: {step_id}\n"
        lines.insert(step_line + 1, f"{dash_indent}  uses:{rest}\n")
    return lines, step_id, f"${{{{ steps.{step_id}.outputs.digest }}}}"


def apply_one_fix_raw(lines, f):
    """Inject --metadata-file into the build command, then append an
    extraction+export line to the same run: block. Handles both a
    single-line `run: docker build ...` and a `run: |` block."""
    step_line = f["build_step_line"]
    cmd_line = f["build_cmd_line"]

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

    is_block = re.match(r"^\s*-?\s*run:\s*\|\s*$", lines[step_line]) is not None
    run_key_indent = None
    if not is_block:
        m = re.match(r"^(\s*)(-)(\s*)run:\s*(.*)$", lines[step_line])
        if m:
            dash_indent, dash, gap, rest = m.group(1), m.group(2), m.group(3), m.group(4)
            # block-scalar content must be indented MORE than the column
            # where "run:" itself starts, not just more than the dash --
            # confirmed by testing: using dash_indent+2 landed content at
            # the same column as "run:" itself, which is invalid YAML.
            run_key_col = len(dash_indent) + len(dash) + len(gap)
            body_indent = run_key_col + 2
            lines[step_line] = f"{dash_indent}{dash}{gap}run: |\n"
            lines.insert(step_line + 1, " " * body_indent + rest + "\n")
            run_key_indent = body_indent
            cmd_line += 1  # shifted down by the inserted first block line
            insertion_point = step_line + 2
        else:
            insertion_point = cmd_line + 1
            run_key_indent = indent_of(lines[cmd_line])
    else:
        run_key_indent = indent_of(lines[cmd_line])
        j = cmd_line + 1
        while j < len(lines) and (lines[j].strip() == "" or indent_of(lines[j]) >= run_key_indent):
            j += 1
        insertion_point = j

    existing_id_m = re.match(r"^\s*-\s*id:\s*(\S+)", lines[step_line]) if is_block else None
    if not existing_id_m and step_line >= 1:
        existing_id_m = re.match(r"^\s*id:\s*(\S+)", lines[step_line - 1]) if lines[step_line - 1].strip().startswith("id:") else None

    if existing_id_m:
        step_id = existing_id_m.group(1)
    else:
        step_id = fresh_step_id(lines)
        m = re.match(r"^(\s*)-(\s*)", lines[step_line])
        if m:
            dash_indent, gap = m.group(1), m.group(2)
            rest_of_line = lines[step_line][m.end():]
            lines[step_line] = f"{dash_indent}-{gap}id: {step_id}\n"
            lines.insert(step_line + 1, f"{dash_indent} {gap}{rest_of_line}")
            insertion_point += 1
            cmd_line += 1

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
        output_name = "image_digest"
        digest_ref = f'${{{{ needs.{f["build_job"]}.outputs.{output_name} }}}}'

        jstart, jend, jindent = jobs[f["build_job"]]
        steps_idx, _ = find_line(lines, jstart, jend, r"^\s*steps:\s*$")
        out_idx, _ = find_line(lines, jstart, jend, r"^\s*outputs:\s*$")
        body_indent = jindent + 2
        if out_idx is None:
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
        var_key = m.group(1)  # reuse whatever name the module's own -var used
        quote = m.group(0)[-1]
        sep = "=" if re.match(r"-var=", m.group(0)) else " "
        replacement = f'-var{sep}{quote}{var_key}={new_image}{quote}'
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
    if not build_jobs:
        return []

    findings = []
    for job, (jstart, jend, jindent) in jobs.items():
        tf_idx, _ = find_line(lines, jstart, jend, r"terraform (apply|plan)")
        if tf_idx is None:
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

        block_start, block_end = terraform_command_block(lines, tf_idx, jend)
        block_text = "".join(lines[block_start:block_end])
        if "@sha256:" in block_text:
            continue  # already looks digest-pinned in the visible command

        signal = None
        for i in range(jstart, jend):
            m = tf_var_env_regex().match(lines[i])
            if m and is_tagish_key(m.group(2)) and "@" not in m.group(3):
                signal = (i, f"env var {m.group(2)} looks image/tag-related "
                              f"but its value doesn't match the image this job builds")
                break
        if signal is None:
            for i in range(block_start, block_end):
                m = re.search(r'''-var[= ]+["']([a-zA-Z_][a-zA-Z0-9_]*)=([^"']*)["']''', lines[i])
                if m and is_tagish_key(m.group(1)) and "@" not in m.group(2):
                    signal = (i, f"-var {m.group(1)} looks image/tag-related "
                                 f"but its value doesn't match the image this job builds")
                    break
        if signal is None:
            for i in range(jstart, jend):
                if TFVARS_FILE_RE.search(lines[i]):
                    signal = (i, "a .tfvars file is written or referenced here -- "
                                 "the actual image reference may be set there")
                    break

        if signal:
            line_idx, reason = signal
            findings.append({"build_job": linked_build_job, "deploy_job": job,
                              "line": line_idx + 1, "reason": reason})
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
