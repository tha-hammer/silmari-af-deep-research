# silmari-af-deep-research

AgentField deep-research node: reasoners/skills in `main.py` (`node_id="meta_deep_research"`),
plus a SuperTokens-gated web UI under `ui/`. Part of the `silmari-agentfield-system`
meta-repo — see its root `ARCHITECTURE.md` for doctrine.

## Ops notes — deep-research UI

- **Deploy:** Railway service `deep-research-ui` (project `silmari-deep-research`) is
  repo-as-source from `tha-hammer/silmari-af-deep-research` `main`, **Root Directory = `ui/`**
  (dashboard-only — the Railway CLI can't set root dir), `RAILWAY_DOCKERFILE_PATH=Dockerfile`
  (i.e. `ui/Dockerfile`). Because root=`ui/`, `ui/`'s contents land at `/app`, so `ui/Dockerfile`
  does `COPY . /app/ui/` + `CMD ["python","-m","ui.app"]` so the app's absolute `ui.*` imports
  resolve at runtime. UI→control-plane is HTTP only (`server.py` `cp_get`/`cp_post`); no `af` CLI.
- **Run persistence:** durable per-user index `deepresearch.research_run` on the Railway
  **`user_data`** Postgres (UI var `DEEPRESEARCH_DATABASE_URL=${{user_data.DATABASE_URL}}`).
  Index-only: the row stores `result_ref = root_execution_id`; the report body stays in the
  control plane and is fetched by that id. `created_by = UUID(supertokens_user_id)` (SuperTokens
  ids are uuids), `org_id` = default org from `ui/config/tenancy.json`. Reads are
  `WHERE org_id=… AND created_by=…` (per-user, fail-closed). Domain lives in `ui/workspace/` +
  `ui/tenancy/`.
- **Control plane:** must run `AGENTFIELD_STORAGE_MODE=postgres` — `local`/`dev` storage loses
  the node registry on restart → agent heartbeat 404 (`node not found`) + `agent_error` runs.
- **Railway:** use the `railway` CLI (authed as `maceo.jourdan@gmail.com`); the Railway MCP token
  is stale/unauthorized. Read secrets via `railway variables --service <name> --json`.

## Tests

```bash
# unit + e2e (fakes, no DB)
uv run --extra dev pytest tests/ui/ -m "not integration"
# psycopg adapter + migration (needs a Postgres)
TEST_DATABASE_URL=postgresql://… uv run --extra dev pytest -m integration tests/ui/
```

The scaffold `tests/test_agent.py` is broken upstream cruft (imports a non-existent `agent`
package); `tests/conftest.py` ignores it so `tests/ui/` collects.
