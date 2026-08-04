# Security

## Automated vulnerability scanning

CI runs two independent CVE scans in the [`Security`](.github/workflows/security.yml)
workflow on every pull request, every push to `main`, weekly on a schedule
(Mondays 06:00 UTC), and on manual dispatch:

| Scan | Tool | Scope | Fails on |
| --- | --- | --- | --- |
| Dependencies | [pip-audit](https://pypi.org/project/pip-audit/) | The full locked dependency set exported from `uv.lock` (including the dev group) | Any known **fixable** vulnerability |
| Base image | [Trivy](https://github.com/aquasecurity/trivy) | The production Docker image built from the `Dockerfile` | `CRITICAL` / `HIGH` vulnerabilities that have a fix available (`ignore-unfixed: true`) |

The weekly schedule exists because most findings arrive *between* merges, when a
new CVE is published against a dependency we already ship. Unfixed image CVEs are
ignored deliberately — there is nothing actionable until an upstream fix ships, and
the weekly run re-checks them once one does.

## Reproducing the dependency scan locally

```bash
just audit
```

This mirrors the CI `pip-audit` invocation exactly (export the locked set, then
audit it). Image scanning is CI-only, as it requires Trivy installed on the host.

## When a scan fails

**Prefer fixing over suppressing.**

1. **Dependency finding (pip-audit):** upgrade the affected package —
   `uv lock --upgrade-package <pkg>` (or bump the pin in `pyproject.toml`), then
   confirm with `just audit` and let CI re-run.
2. **Image finding (Trivy):** bump the base image tag in the `Dockerfile` (or the
   affected OS package) to a version carrying the fix, then let CI rebuild and
   re-scan.

If no fix exists yet, or the finding is a confirmed false positive, suppress it —
always with a justification and a revisit date:

- **Dependency:** add `--ignore-vuln <ID>` to the `pip-audit` step in
  `.github/workflows/security.yml`, with a comment stating the CVE ID, the reason,
  and a date to revisit.
- **Image:** add the CVE ID to a repo-root `.trivyignore` file (Trivy reads it
  automatically), one ID per line, each with a comment stating the reason and a
  revisit date.

Never add a suppression purely to turn CI green — every entry must carry a
justification a reviewer can evaluate.

## Scheduled-run failures

Weekly scheduled runs surface failures through GitHub's default workflow-failure
notifications to the repository owner. Check the
[Actions tab](../../actions/workflows/security.yml) for the failing run and triage
per the steps above.
