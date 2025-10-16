# TODOs

## Reddit Ingestion Follow‑up

- [ ] Provide credentials for read‑only API access:
  - `REDDIT_CLIENT_ID`
  - `REDDIT_CLIENT_SECRET`
  - `REDDIT_USER_AGENT` (e.g., `ai-briefing/1.0 <contact>`)
- [ ] Re-run `make reddit` after setting the env vars (via `.env` or Docker env).
- [ ] Improve error handling on 401 with a clear hint to missing creds.
- [ ] Optional: add a guard in `make all` to skip Reddit when creds are missing.

Context: Current run fails with `401 Unauthorized` from PRAW due to missing credentials (see `briefing/sources/reddit_adapter.py`). HN/Twitter are already validated end-to-end with the new staging flags enabled.

