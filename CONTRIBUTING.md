# Contributing and release flow

Trunk-based: **`main` is always deployable**, and merging to it deploys.

## Branches

| Branch | Use |
|---|---|
| `main` | Production. Protected: changes arrive by pull request with CI passing. |
| `feature/<short-name>` | New behaviour |
| `fix/<short-name>` | Bug fixes |
| `chore/<short-name>` | Tooling, docs, dependency bumps |

Keep branches short-lived (days, not weeks) and rebase on `main` before opening the pull request.

## A change, start to finish

1. `git switch -c feature/my-change`
2. Run the app and tests locally (see the README): `docker compose up -d` then
   `docker compose run --rm -e TEST_DATABASE_URL=postgresql+psycopg://rlk:rlk_dev_only@db:5432/rlk_test web pytest -q`
3. Push and open a pull request. CI runs `test`, `lint` and `secrets`; all three must pass.
4. Squash-merge into `main`. The `CI / Deploy` workflow builds the images, pushes them to GHCR and deploys; the
   smoke test confirms the live site reports the new build at `/healthz`.
5. A bad release: re-run the workflow on the last good commit, or revert the merge. `deploy/deploy.sh` also rolls
   back by itself if the new version does not become healthy.

## Commits

Short imperative subject (`Add cue types to the editor`), a body that says *why*. Conventional prefixes are welcome,
not required.

## Rules of the road

- Never commit secrets: `.env` is ignored, and CI scans the history. Production config lives in the `PROD_ENV`
  secret and on the server.
- Database changes are additive (`ALTER ... IF NOT EXISTS` in `app/__init__.py`) so a rollback is safe.
- Anything that shows a Strava route to a user other than its owner is a privacy bug. Read `DESIGN.md` first.
