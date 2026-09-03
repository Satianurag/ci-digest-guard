#!/usr/bin/env python3
"""v8: same stdlib-only engine as v7, but fixes a real bug v7 crashed on --
a multi-service file with 2+ independent build+deploy pairs. v7 tried to
manually track how much every later line number shifted after inserting
lines for an earlier fix, and got it wrong (crashed on the 2nd fix).

v8's fix: never trust stale line numbers across mutations. Apply exactly
ONE finding, then re-scan the file from scratch (cheap for a workflow
file) before applying the next one, repeating until nothing is left to
find. Each fix is always computed against line numbers that are correct
for the text as it currently exists.
"""
import re
import sys


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def scan_jobs(lines):
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
        if l.strip() == "":
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
        while i < len(lines) and (lines[i].strip() == "" or indent_of(lines[i]) > job_key_indent):
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
    """`with: {push: true, tags: repo:tag}` on one line. Return the dict
    of key->raw-value-text if this step's `with:` is flow-style, else None."""
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
    return re.compile(r'''-var[= ]+["']image=''' + re.escape(repo) + r'''(?::[^"'@]+)?["']''')


def find_build_step(lines, start, end):
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


def find_mutable_ref(lines, start, end, repos):
    tf_idx, _ = find_line(lines, start, end, r"terraform (apply|plan)")
    if tf_idx is not None:
        for repo in repos:
            m = cli_var_regex(repo).search(lines[tf_idx])
            if m and "@" not in m.group(0):
                return ("cli", tf_idx, repo)
    for i in range(start, end):
        m = re.match(r"^(\s*)TF_VAR_[Ii][Mm][Aa][Gg][Ee]:\s*(\S+)", lines[i])
        if m:
            val = m.group(2)
            for repo in repos:
                if val.startswith(repo + ":") and "@" not in val:
                    return ("env", i, repo)
    return None


def find_one(lines):
    """Return a single finding (or None), always computed fresh against
    the CURRENT state of `lines` -- never reused across mutations."""
    jobs = scan_jobs(lines)
    for build_job, (jstart, jend, jindent) in jobs.items():
        build_step_line = find_build_step(lines, jstart, jend)
        if build_step_line is None:
            continue
        tags_text = extract_tags_value(lines, build_step_line, jend)
        repos = repos_from_tags_text(tags_text)
        if not repos:
            continue

        same_hit = find_mutable_ref(lines, jstart, jend, repos)
        if same_hit:
            kind, line_idx, repo = same_hit
            return {"scope": "same-job", "kind": kind, "line": line_idx,
                    "build_job": build_job, "deploy_job": build_job,
                    "build_step_line": build_step_line, "image_repo": repo}

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
                        "build_step_line": build_step_line, "image_repo": repo}
    return None


def taken_step_ids(lines):
    ids = set()
    for l in lines:
        m = re.match(r"^\s*-?\s*id:\s*(\S+)", l)
        if m:
            ids.add(m.group(1))
    return ids


def apply_one_fix(lines, f):
    """Mutates and returns a NEW lines list with exactly one finding fixed.
    Every index used here is recomputed fresh from the current `lines`,
    never carried over from a previous mutation."""
    lines = list(lines)
    step_line = f["build_step_line"]

    id_window = lines[max(0, step_line - 1): step_line + 1]
    existing_id_m = next((re.match(r"^\s*-?\s*id:\s*(\S+)", l) for l in id_window if re.match(r"^\s*-?\s*id:\s*(\S+)", l)), None)

    if existing_id_m:
        step_id = existing_id_m.group(1)
    else:
        taken = taken_step_ids(lines)
        candidate, n = "push", 2
        while candidate in taken:
            candidate = f"push{n}"
            n += 1
        step_id = candidate
        m = re.match(r"^(\s*)(-)(\s*)uses:(.*)$", lines[step_line])
        dash_indent, dash, gap, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        lines[step_line] = f"{dash_indent}{dash}{gap}id: {step_id}\n"
        lines.insert(step_line + 1, f"{dash_indent}  uses:{rest}\n")

    # re-find the mutable-reference line fresh (its index may have shifted
    # by the single `id:` line we just possibly inserted).
    jobs = scan_jobs(lines)
    if f["scope"] == "same-job":
        jstart, jend, _ = jobs[f["build_job"]]
        kind_line_repo = find_mutable_ref(lines, jstart, jend, {f["image_repo"]})
        digest_ref = f"${{{{ steps.{step_id}.outputs.digest }}}}"
    else:
        ostart, oend, _ = jobs[f["deploy_job"]]
        kind_line_repo = find_mutable_ref(lines, ostart, oend, {f["image_repo"]})
        digest_ref = f'${{{{ needs.{f["build_job"]}.outputs.image_digest }}}}'

        jstart, jend, jindent = jobs[f["build_job"]]
        steps_idx, _ = find_line(lines, jstart, jend, r"^\s*steps:\s*$")
        out_idx, _ = find_line(lines, jstart, jend, r"^\s*outputs:\s*$")
        body_indent = jindent + 2
        if out_idx is None:
            lines[steps_idx:steps_idx] = [
                " " * body_indent + "outputs:\n",
                " " * (body_indent + 2) + f"image_digest: ${{{{ steps.{step_id}.outputs.digest }}}}\n",
            ]
        else:
            lines.insert(out_idx + 1, " " * (body_indent + 2) + f"image_digest: ${{{{ steps.{step_id}.outputs.digest }}}}\n")
        # inserting into the build job may have shifted the deploy job's
        # lines too if deploy comes after build in the file -- re-find it.
        jobs = scan_jobs(lines)
        ostart, oend, _ = jobs[f["deploy_job"]]
        kind_line_repo = find_mutable_ref(lines, ostart, oend, {f["image_repo"]})

    kind, line_idx, repo = kind_line_repo
    new_image = f"{repo}@{digest_ref}"
    if kind == "cli":
        m = cli_var_regex(repo).search(lines[line_idx])
        quote = m.group(0)[-1]
        sep = "=" if re.match(r"-var=", m.group(0)) else " "
        replacement = f'-var{sep}{quote}image={new_image}{quote}'
        lines[line_idx] = lines[line_idx][:m.start()] + replacement + lines[line_idx][m.end():]
    else:
        m = re.match(r"^(\s*TF_VAR_[Ii][Mm][Aa][Gg][Ee]:\s*)\S+", lines[line_idx])
        lines[line_idx] = m.group(1) + new_image + "\n"

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
        except Exception as e:
            print(f"SKIPPED {path}: could not analyze/fix ({e.__class__.__name__}: {e})")
            continue
        if not findings:
            print(f"CLEAR {path}: no mutable-tag-into-terraform pattern detected")
            continue
        for f in findings:
            scope_desc = "same job" if f["scope"] == "same-job" else f"job '{f['deploy_job']}' (needs build job '{f['build_job']}')"
            print(f"FOUND {path} [{f['kind']}, {scope_desc}]: terraform deploys "
                  f"{f['image_repo']}:<mutable tag> instead of the digest the build step already produced")
        out_path = path + ".v9fixed"
        open(out_path, "w").writelines(fixed_lines)
        print(f"  -> {len(findings)} issue(s) fixed, wrote {out_path}")
