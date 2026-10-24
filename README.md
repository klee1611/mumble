# Mumble 🦥

Mumble at your phone all day. Open models sort it out overnight.

Record rambling voice memos whenever something pops into your head. At 3 AM, a
free GitHub Actions runner wakes up, transcribes them with Whisper, and has
Gemma turn them into to-dos, calendar events and notes. In the morning you open
one screen and tap **Done**, **Add to calendar** or **Nah**.

It costs nothing to run.

- **Open models do all the thinking:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech,
  Gemma (`gemma4:e2b-it-qat`) for structure and `embeddinggemma` for memory, all served by [Ollama](https://ollama.com).
- **Gemma never does date math.** It copies the exact time phrase you said ("Tuesday at 10am");
  plain code turns that into a timestamp. A phrase you never said is thrown away.
- **Slow on purpose.** CPU only. Nobody is awake at 3 AM to wait for it.
- **Your audio doesn't stick around.** Each recording is deleted as soon as it's sorted.

```
Phone PWA ──▶ DigitalOcean App Platform (free static site)
    │
    └─ API ──▶ DigitalOcean Functions (free tier) ──▶ Postgres + pgvector (Tiger Data, free)
                    │                                        ▲
     3 AM: scheduled "wake" function ──repository_dispatch──┐ │
                                                            ▼ │
                          GitHub Actions (free CPU runner): Ollama + Gemma + Whisper
```

| Piece | Where | Cost |
|---|---|---|
| Phone app (`web/`) | DigitalOcean App Platform static site | free |
| API (`functions/`) | DigitalOcean Functions | free tier |
| 3 AM alarm | DigitalOcean Functions scheduled trigger | free tier |
| Night shift (`mumble/nap.py`) | GitHub Actions, public repo | free |
| Database | Tiger Data (Postgres + pgvector) | free tier |

## Run it locally

Needs [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), ffmpeg and Docker.

```bash
ollama pull gemma4:e2b-it-qat && ollama pull embeddinggemma
docker compose up -d                  # local Postgres + pgvector
cp .env.example .env                  # set MUMBLE_TOKEN to something random
uv sync
uv run uvicorn mumble.main:app --reload
```

Open `http://localhost:8000/?t=<your MUMBLE_TOKEN>`, record something, then tap
**Can't wait? Nap now** (or run `uv run python -m mumble.nap`).

The microphone only works on `localhost` or over HTTPS. Locally, search also
matches by meaning (the query is embedded with Ollama); the serverless API
searches by keyword.

## Deploy (all free)

1. **Database.** Create a free Tiger Cloud service and copy its connection string.
   Create the tables once: `DATABASE_URL='postgres://...' uv run python -c "from mumble import db; db.init()"`
2. **Night shift.** Push this repo to GitHub (public, so Actions minutes are free) and add
   the secret `DATABASE_URL` under *Settings → Secrets and variables → Actions*.
3. **API + alarm.** Fill in `functions/.env` (see `functions/.env.example`), then
   `scripts/deploy-functions.sh`. It prints the API URL.
4. **Phone app.** Put the API URL in `web/config.js` (`window.MUMBLE_API = "https://.../mumble/api"`),
   push, then `doctl apps create --spec .do/app.yaml`.
5. On your phone, open `https://<your-app>/?t=<MUMBLE_TOKEN>` once and *Add to Home Screen*.

## Tests

```bash
uv run pytest
```

## License

MIT
