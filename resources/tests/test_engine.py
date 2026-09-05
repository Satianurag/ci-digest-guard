"""ci-digest-guard engine tests.

Every case here is a bug this engine actually had, found by running it against
real repositories. They are pinned rather than described because each one
produced either a silently unfixed pipeline or a workflow file that no longer
parses -- and both are worse than doing nothing.

    python3 -m unittest discover -s resources/tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import engine  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:                                    # pragma: no cover
    HAVE_YAML = False


def fix(text: str):
    fixed, findings = engine.fix_all(text.splitlines(keepends=True))
    return "".join(fixed), findings


def repos_fixed(findings):
    return sorted(f["image_repo"] for f in findings)


class Base(unittest.TestCase):
    def assertValidYaml(self, text):
        """Only meaningful where PyYAML exists. This play ships stdlib-only on
        purpose, so the check is skipped rather than turned into a dependency;
        it still runs in any environment that happens to have it."""
        if not HAVE_YAML:
            self.skipTest("PyYAML not installed; structural assertions still ran")
        import yaml as y
        y.safe_load(text)


BUILD_ACTION = """name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: myorg/app:${{ github.sha }}
"""


class OneBuildManyDeploys(Base):
    """A build feeding staging AND prod is an ordinary shape, and it broke
    twice: a duplicate outputs key that made the workflow unparseable, and a
    second deploy job left silently on its mutable tag."""

    YAML = BUILD_ACTION + """  deploy-staging:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
  deploy-prod:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
"""

    def test_both_deploy_jobs_are_fixed(self):
        out, findings = fix(self.YAML)
        self.assertEqual(len(findings), 2)
        self.assertNotIn(":${{ github.sha }}\"", out.replace("tags: myorg/app:${{ github.sha }}", ""))

    def test_no_duplicate_output_key(self):
        """Two inserts of the same mapping key is a workflow GitHub refuses to
        load at all -- worse than the bug being fixed."""
        out, _ = fix(self.YAML)
        self.assertEqual(out.count("_digest: ${{ steps."), 1)
        self.assertValidYaml(out)


class TwoBuildsInOneJob(Base):
    """frontend + backend in one job. Only the first build was ever examined,
    so the second image deployed a mutable tag while the run reported success.
    Each build also needs its OWN output name, or the second deploy silently
    points at the first image's digest."""

    YAML = """name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build frontend
        run: |
          docker buildx build --push -t myorg/frontend:${{ github.sha }} ./frontend
      - name: Build backend
        run: |
          docker buildx build --push -t myorg/backend:${{ github.sha }} ./backend
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: |
          terraform apply -auto-approve \\
            -var "frontend_image=myorg/frontend:${{ github.sha }}" \\
            -var "backend_image=myorg/backend:${{ github.sha }}"
"""

    def test_both_images_are_fixed(self):
        _, findings = fix(self.YAML)
        self.assertEqual(repos_fixed(findings), ["myorg/backend", "myorg/frontend"])

    def test_each_build_gets_its_own_digest_output(self):
        out, _ = fix(self.YAML)
        self.assertNotIn("frontend_image=myorg/frontend@${{ needs.build.outputs.push2_digest }}", out)
        outputs = [ln.strip() for ln in out.splitlines() if ln.strip().endswith("_digest }}")]
        self.assertEqual(len(set(outputs)), 2, "the two images must not share one output")
        self.assertValidYaml(out)


class BuildShapesThatWereInvisible(Base):
    def test_backslash_continuation(self):
        """The standard multi-line buildx style. Reading only the first
        physical line missed both -t and --push, so a genuinely vulnerable
        pipeline reported CLEAR."""
        _, findings = fix("""name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          docker buildx build \\
            --push \\
            -t myorg/app:${{ github.sha }} \\
            .
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(repos_fixed(findings), ["myorg/app"])

    def test_name_and_uses_on_separate_lines(self):
        """Exactly how docker/build-push-action's own README shows it. The
        engine used to assume `uses:` sat on the step's dash line and crashed."""
        _, findings = fix(BUILD_ACTION + """  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(repos_fixed(findings), ["myorg/app"])


class NotSafeToAutoFix(Base):
    """--metadata-file only carries a real registry digest when buildx itself
    pushed. Injecting it anywhere else yields a fix that KeyErrors on a
    successful build, or pins a digest that is not what shipped."""

    def test_load_then_separate_push_is_left_alone(self):
        _, findings = fix("""name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          docker buildx build --load -t myorg/app:${{ github.sha }} .
          docker push myorg/app:${{ github.sha }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(findings, [])

    def test_but_it_is_still_reported_not_silently_dropped(self):
        out, _ = fix("""name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          docker buildx build --load -t myorg/app:${{ github.sha }} .
          docker push myorg/app:${{ github.sha }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertTrue(engine.find_unknowns(out.splitlines(keepends=True)))


class TerraformCommandDetection(Base):
    def test_plan_before_apply_does_not_stop_the_search(self):
        """A plan line carries no real -var flags. Stopping there reported
        CLEAR while the apply one step later deployed a mutable tag."""
        _, findings = fix(BUILD_ACTION + """  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform plan -out=tfplan
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(repos_fixed(findings), ["myorg/app"])

    def test_terragrunt_and_tofu_count_as_terraform(self):
        for cli in ("terragrunt", "tofu"):
            with self.subTest(cli):
                _, findings = fix(BUILD_ACTION + f"""  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: {cli} apply -auto-approve -var "image=myorg/app:${{{{ github.sha }}}}"
""")
                self.assertEqual(repos_fixed(findings), ["myorg/app"])

    def test_unquoted_var_flag(self):
        _, findings = fix(BUILD_ACTION + """  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var image=myorg/app:${{ github.sha }}
""")
        self.assertEqual(repos_fixed(findings), ["myorg/app"])


class YamlParsingEdges(Base):
    def test_comment_on_the_jobs_line_does_not_hide_every_job(self):
        """Found live on skkuding/codedang: a trailing comment on `jobs:` made
        the parser see zero jobs in the whole file, with no error."""
        _, findings = fix("""name: ci
on: push
jobs:  # all of them
  build:  # the build one
    runs-on: ubuntu-latest
    steps:
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: myorg/app:${{ github.sha }}
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(repos_fixed(findings), ["myorg/app"])


class OutputIsSafe(Base):
    def test_already_pinned_file_is_untouched(self):
        """Byte-stability on a clean file: running this must be a no-op, not a
        reformat."""
        clean = BUILD_ACTION + """  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app@sha256:abc"
"""
        out, findings = fix(clean)
        self.assertEqual(findings, [])
        self.assertEqual(out, clean)

    def test_fixing_twice_changes_nothing_the_second_time(self):
        once, _ = fix(OneBuildManyDeploys.YAML)
        twice, findings = fix(once)
        self.assertEqual(findings, [])
        self.assertEqual(twice, once)

    def test_raw_run_block_stays_valid_yaml(self):
        out, findings = fix("""name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - id: push
        name: Build
        run: docker buildx build --push -t myorg/app:${{ github.sha }} .
  deploy:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: terraform apply -auto-approve -var "image=myorg/app:${{ github.sha }}"
""")
        self.assertEqual(len(findings), 1)
        self.assertEqual(out.count("id: push"), 1, "must not insert a second id:")
        self.assertValidYaml(out)


class HclCrossReference(Base):
    """An UNKNOWN is upgraded to CONFIRMED only on real evidence from the
    repo's own .tf files, and is never downgraded to CLEAR by their absence."""

    def test_confirms_when_the_variable_reaches_a_container_resource(self):
        import tempfile, shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "infra"))
        with open(os.path.join(root, "infra", "main.tf"), "w") as fh:
            fh.write('resource "aws_ecs_task_definition" "api" {\n'
                     '  image = "ghcr.io/acme/api:${var.container_tag}"\n}\n')
        unknown = {"var_name": "TF_VAR_CONTAINER_TAG", "var_source": "env"}
        evidence = engine.confirm_unknown_via_hcl(unknown, root, engine.find_hcl_files(root))
        self.assertIsNotNone(evidence)
        self.assertIn("main.tf", evidence)

    def test_does_not_confirm_a_digest_pinned_reference(self):
        import tempfile, shutil
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, "main.tf"), "w") as fh:
            fh.write('resource "aws_ecs_task_definition" "api" {\n'
                     '  image = "ghcr.io/acme/api@${var.container_tag}"\n}\n')
        unknown = {"var_name": "TF_VAR_CONTAINER_TAG", "var_source": "env"}
        self.assertIsNone(
            engine.confirm_unknown_via_hcl(unknown, root, engine.find_hcl_files(root)))


if __name__ == "__main__":
    unittest.main()
