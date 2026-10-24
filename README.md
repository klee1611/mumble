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
- **One thing leaves for a closed model, and only if you turn it on.** The weekly review sends
  that week's item titles and details (never audio or transcripts) to Ultramarine by
  Paper Instruments. No `PAPERINSTRUMENTS_API_KEY`, no review, and nothing leaves.

```
Phone PWA ──▶ Vercel (free static site)
    │
    └─ /api ──▶ Vercel Function (Python, free) ──▶ Postgres + pgvector (Tiger Data, free)
                                                                 ▲
          3 AM cron ──▶ GitHub Actions (free CPU runner): Ollama + Gemma + Whisper
```

| Piece | Where | Cost |
|---|---|---|
| Phone app (`web/`) | Vercel static files | free (Hobby) |
| API (`api/index.py` → `mumble/api.py`) | Vercel Function | free (Hobby) |
| 3 AM alarm | GitHub Actions schedule | free |
| Night shift (`mumble/nap.py`) | GitHub Actions, public repo | free |
| Database | Tiger Data (Postgres + pgvector) | free tier |

## Project layout

```
web/          the phone app (PWA): plain HTML, CSS and JS, no build step
api/index.py  Vercel's entrypoint; hands each /api request to mumble/api.py
mumble/
  core.py       the API's logic, shared by the cloud and the local server
  api.py        the cloud API (only needs psycopg)
  main.py       the local dev server (FastAPI)
  nap.py        the night shift: Whisper, then Gemma, then embeddings
  transcribe.py / structure.py   speech to text / text to to-dos, events, notes
  review.py     the Sunday review by Ultramarine
  db.py, config.py
scripts/      demo_week.py seeds a week of spoken memos
tests/
.github/workflows/nap.yml   the 3 AM schedule
vercel.json   hosting: static web/, /api rewrite, security headers
```

## Run it locally

Needs [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com), ffmpeg and Docker.

```bash
ollama pull gemma4:e2b-it-qat && ollama pull embeddinggemma
docker compose up -d                  # local Postgres + pgvector
cp .env.example .env                  # set MUMBLE_TOKEN to something random
uv sync --all-groups                  # the API alone needs only psycopg; "nap" has the models
uv run uvicorn mumble.main:app --reload
```

Open `http://localhost:8000/#t=<your MUMBLE_TOKEN>`, record something, then tap
**Can't wait? Nap now** (or run `uv run python -m mumble.nap`).

The microphone only works on `localhost` or over HTTPS. Locally, search also
matches by meaning (the query is embedded with Ollama); the serverless API
searches by keyword.

## Deploy (all free)

1. **Database.** Create a free Tiger Cloud service and copy its connection string.
   Create the tables once: `DATABASE_URL='postgres://...' uv run python -c "from mumble import db; db.init()"`
2. **Night shift.** Push this repo to GitHub (public, so Actions minutes are free) and add
   the secret `DATABASE_URL` under *Settings → Secrets and variables → Actions*.
3. **App + API.** Import the repo in Vercel (*Add New → Project*, Hobby plan, no card).
   `vercel.json` already says what to do: serve `web/`, run `api/index.py` for `/api/*`.
   Add two environment variables, `DATABASE_URL` and `MUMBLE_TOKEN` (24+ random characters),
   and deploy. The function only installs `psycopg`; the heavy model packages are in the
   `nap` dependency group, which only GitHub Actions and your laptop use.
4. On your phone, open `https://<your-app>/#t=<MUMBLE_TOKEN>` once and *Add to Home Screen*.

## Tests

```bash
uv run pytest
```

## License

MIT
