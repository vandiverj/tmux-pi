# GL1: GitLab CI Lint and pipeline simulation (loop idea H34), deferred

Owner, 2026-11-10: GitLab is available at work. Evaluate the CI Lint API and pipeline simulation for job selection, includes,
branch context, and the product/common versus unmarked fallback behaviour. **Priority: low until access exists.**
Status: **deferred**. Advisory.

## Access check on this Mac (2026-11-10)
- No `glab`, `gitlab-runner`, `gitlab-ci-local` or docker; no GitLab-related environment variable names; no glab config dir.
- No `.gitlab-ci.yml` in tmux-pi or any project under `~/Documents/Projects`; tmux-pi's remote is GitHub.
- The only GitLab mention in tmux-pi is the `glpat-` token pattern in `lib/redact_rules.py:9`.
So the eval cannot run here, and GitLab-specific conclusions need the work instance.

## Prerequisites (work environment)
1. A project on the work GitLab with its representative CI config, including its `include:` files and the product/common versus
   unmarked rules, readable by the account used.
2. A token with the `api` scope (or `read_api` if the instance allows lint with it), kept out of tmux-pi records; tmux-pi's
   redaction already masks `glpat-` tokens.
3. The instance version (CI Lint API behaviour changed across versions: `POST /projects/:id/ci/lint` with `content`, or `GET`
   for the project's own config; `dry_run=true` with `ref` simulates a pipeline for a branch, `include_jobs=true` lists the jobs).
4. The owner's approval for any call to the work instance; read-only calls only, no pipeline is created.

## Work-environment procedure (small)
1. Record the instance version and the project id.
2. For 4 refs (a product branch, a common-only change, an unmarked change, the default branch), call lint with
   `dry_run=true&include_jobs=true&ref=<ref>`; save the job lists.
3. Write the expected job list per ref by hand from the rules first, then compare; note includes that failed to resolve.
4. Change one rule in a scratch copy of the config (`content=` lint, nothing pushed) and check the job list moves as expected.
5. Score: job-selection agreement, include errors found, time per call, and whether the fallback for unmarked changes is the
   one the team expects. Decide incorporate (a pre-push or pre-review check in tmux-pi), keep on demand, or drop.

## Preparation that can happen here (reported as preparation only)
A local fixture `.gitlab-ci.yml` with includes and the product/common/unmarked rules, checked for YAML structure and with the
expected job lists written per ref. It cannot show what GitLab's lint does, so it proves nothing GitLab-specific.
