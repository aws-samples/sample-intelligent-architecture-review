# Tests

Every test in the repo lives here, grouped by what it exercises.

| Directory | Covers | Runner |
|---|---|---|
| `backend/` | FastAPI routes, services, agents, the pre-token Lambda | `pytest` |
| `infra/` | Terraform config invariants (parsed as text — no AWS calls) | `pytest` |
| `frontend/` | API client, backend↔frontend vocabulary contract | `vitest` |
| `e2e/` | A **deployed** environment, end to end | `pytest -m e2e` |

## Running them

```bash
# Backend + infra (the default suite). No env setup needed — tests/conftest.py
# injects fake AWS credentials and the required config vars.
pytest

# One file or one test
pytest tests/backend/test_icon_legend.py
pytest tests/backend/test_icon_legend.py::test_create_status_fails_closed

# Frontend
cd frontend && npm test

# Everything except E2E, with coverage (the CI gate is 30%)
pytest --cov=backend/app
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs five jobs on every push to
`main` and every pull request:

| Job | Runs |
|---|---|
| `backend` | `pytest tests/backend tests/infra` on Python 3.11 (matches `backend/Dockerfile`) |
| `frontend` | `tsc --noEmit`, `eslint`, `vitest run`, then `next build` |
| `terraform` | `fmt -check -recursive`, `init -backend=false`, `validate` |
| `dependency-audit` | `pip-audit` on `backend/requirements.txt`, gated on zero unaccepted advisories |
| `checkov` | Policy scan of `infrastructure/`, gated on zero failures |

### Coverage gates

| Suite | Gate | Actual |
|---|---|---|
| backend + infra | 68% lines (`pytest.ini`) | ~70% |
| frontend | 6% lines (`frontend/vitest.config.ts`) | ~6% |

Frontend coverage is measured over `src/lib`, `src/components` (excluding the
vendored shadcn primitives in `components/ui/`), and the stateful `client.tsx`
pages. Layout and page shells are excluded, because asserting that a wrapper
renders its children raises the number without catching defects.

The frontend gate is a ratchet set just below the current value, not a target:
component and page tests are still to be written. Raise it as suites land; do
not lower it to make a build pass.

All four are hermetic — no AWS credentials, no deployment, no model spend.
`tests/conftest.py` injects fake credentials and config vars, so the stores fall
back to their in-memory paths. `terraform init` runs with `-backend=false` so it
resolves providers without touching the S3 state bucket.

**E2E is not in CI** — it needs a live deployment, real Cognito logins and
Bedrock calls that cost money. Run it manually (below). Deploys also stay
manual: `deploy.sh` is account-guarded and never runs from a pipeline.

## E2E

E2E talks to a real deployment: real Cognito logins, real Bedrock calls (which
cost money), real DynamoDB rows. It is therefore **deselected by default** and
needs both a flag and configuration:

```bash
export E2E_API_URL=https://<cloudfront-domain>   # NOT the ALB: it 403s direct traffic
export E2E_COGNITO_CLIENT_ID=<web app client id>
export E2E_CURATOR_EMAIL=curator@example.com
export E2E_CURATOR_PASSWORD=...
export E2E_VIEWER_EMAIL=viewer@example.com
export E2E_VIEWER_PASSWORD=...

pytest -m e2e --run-e2e
```

`deploy.sh seed-users` creates the four per-role logins these use. Any missing
variable skips the affected test rather than failing it, so a partly configured
environment still runs what it can. Each test cleans up the rows it creates.

## Conventions

- **Backend tests never touch AWS.** Stores expose an in-memory fallback
  (`store._available = False`); use it instead of mocking boto3.
- **Mark slow or credentialed tests** with the markers declared in
  `pytest.ini` (`unit`, `integration`, `slow`, `agents`, `infra`, `e2e`).
- **Infra tests parse `.tf` files as text.** They must stay free of the
  `terraform` binary and AWS so they run anywhere in milliseconds.
- The backend is imported as `app.*` / `app.agents.*`; `pytest.ini` puts
  `backend/` on `pythonpath`, so no `sys.path` juggling is needed in a test.
