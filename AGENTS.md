# Backend Agent Instructions

## AWS CLI

- The AWS CLI is available for backend infrastructure and S3 work.
- Use the named profile `met-galaxy` and region `us-east-1`.
- Verify the active account and identity before any AWS operation:

  ```bash
  aws sts get-caller-identity --profile met-galaxy
  ```

- Pass `--profile met-galaxy` explicitly to AWS commands. Do not assume the default profile points to the correct account.
- If the session has expired, ask the user to run:

  ```bash
  aws login --profile met-galaxy
  ```

- If `aws login` is unavailable and the profile uses IAM Identity Center, ask the user to run `aws sso login --profile met-galaxy` instead. Do not choose or configure an authentication method on the user's behalf.
- Authentication does not authorize unrelated infrastructure changes. Stay within the user's requested scope, inspect resources before mutating them, and confirm exact destructive targets.
- Never add AWS credentials, login caches, account secrets, or temporary tokens to the repository, `.env` files, logs, commits, or agent responses.
- Do not create long-lived access keys or reconfigure, log out, or delete the shared profile unless the user explicitly requests it.

## Neon Database

- The backend uses a shared Neon PostgreSQL database through `DATABASE_URL` in the local `.env` file.
- Treat the configured database as production unless the user explicitly identifies it as a disposable development branch.
- Never print, echo, copy, commit, or include `DATABASE_URL` in commands, logs, patches, or agent responses.
- Prefer the existing Drizzle connection in `src/db/index.ts`, schema in `src/db/schema.ts`, and migrations in `drizzle/`.
- To verify connectivity without changing data, start the backend:

  ```bash
  npm run dev
  ```

  Then call the database-check endpoint from another terminal:

  ```bash
  curl --fail --silent --show-error http://localhost:8080/api/test-db
  ```

- Read-only inspection is allowed when it is relevant to the task. Keep exploratory queries bounded with filters and `LIMIT`.
- Before a schema change, inspect the current schema and existing migrations. `npm run db:generate` may be used to generate a migration for review.
- `npm run db:migrate`, direct DDL, backfills, embedding scripts, bulk updates, and bulk deletes modify the shared database. Run them only when the user's request explicitly includes that change and only after verifying the target database and reviewing the exact operation.
- Never run `drizzle-kit push`, destructive resets, `DROP`, `TRUNCATE`, or unbounded `UPDATE` or `DELETE` statements without explicit user authorization and a recovery plan.
- Database authentication is an available capability, not permission for unrelated data changes.

Before changing image hosting, ingestion, duplicate handling, embeddings, or the PCA build, read `docs/FUTURE_EMBEDDING_IMPROVEMENTS.md` and follow the highest incomplete priority whose prerequisites are satisfied.
