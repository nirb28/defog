# Defog backend

FastAPI service that the web and mobile apps call. It handles accounts and sessions, connects to Canvas and Google Calendar, keeps a synced copy of each student's data in Supabase Postgres, and runs the chatbot. API keys, Canvas tokens and Google tokens stay here and never reach a browser or the AI model.

## Run it locally (Windows PowerShell)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env        # then fill in DATABASE_URL (see below)
alembic upgrade head          # creates the tables in your database
uvicorn app.main:app --reload
```

On macOS/Linux, activate with `source .venv/bin/activate` and use `cp` instead of `copy`.

The API runs at http://localhost:8000, with interactive docs at http://localhost:8000/docs. Open the app through the frontend (http://localhost:5173), which forwards `/api` to the backend.

Without other settings the app still runs: the AI is in **mock mode** (canned answers that still call the real data tools), and verification and password-reset emails are printed in the server log instead of sent.

## Supabase (the database)

1. In the Supabase dashboard, open your project and click **Connect**.
2. Copy the **Session pooler** connection string. It works on IPv4 networks; the "Direct connection" only works over IPv6 unless you buy the IPv4 add-on.
3. Put it in `.env` as `DATABASE_URL`, with your database password filled in. Percent-encode special characters in the password (`@` becomes `%40`). Defog adds `sslmode=require` itself.
4. Run `alembic upgrade head`. Run it again after every update that adds a migration.

Supabase also publishes tables in the `public` schema through its Data API, for browser apps that talk to the database directly. Defog never does that: only this backend reads the tables. So the migration turns on row-level security for every table with no policies, and removes the Data API roles' access. Student data can't be read with your project's public (anon) key. Any table added later must call `lock_down()` in its migration; a test fails if one doesn't.

Use the **Session pooler** or a direct connection, not the **Transaction pooler** (port 6543). Transaction mode breaks the locks that stop two syncs of the same account running at once.

## Google sign-in and Google Calendar

One Google OAuth client covers both.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project.
2. **APIs & Services → Library**: enable the **Google Calendar API**.
3. **OAuth consent screen**: choose External and add the scopes `openid`, `email`, `profile`, `.../auth/calendar.readonly` and `.../auth/calendar.events`. While the app is in Testing mode, only accounts listed under **Test users** can connect a calendar.
4. **Credentials → Create credentials → OAuth client ID → Web application**. Add these redirect URIs, swapping in your `API_URL` for production:
   - `http://localhost:5173/api/auth/google/callback`
   - `http://localhost:5173/api/connections/google-calendar/callback`
5. Put the client ID and secret in `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

Calendar scopes are classed as sensitive, so Google must verify the app before anyone outside the test-user list can connect a calendar. Sign-in alone (`openid email profile`) doesn't need verification.

## Canvas

Students can connect in two ways:

- **Personal access token** (works today at most schools): the student creates a token in Canvas under Account → Settings → New Access Token and pastes it into Defog. Some schools turn these tokens off; Defog tells the student when Canvas refuses one.
- **Sign in with Canvas** (OAuth): needs a developer key from the school's Canvas admin, with the redirect URI `{API_URL}/api/connections/canvas/callback`. Add each school's key to `CANVAS_OAUTH_CLIENTS`. For those schools, the Connections page shows a "Sign in with Canvas" button.

Defog only reads from Canvas. It never submits or posts anything.

## The AI provider

Set `LLM_PROVIDER` in `.env`:

| Provider | Settings in `.env` |
| --- | --- |
| Claude | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...` (model defaults to `claude-haiku-5-5`) |
| OpenAI-compatible (OpenAI, Meta, Azure, Ollama) | `LLM_PROVIDER=openai`, `OPENAI_BASE_URL=...`, `OPENAI_MODEL=...`, `OPENAI_API_KEY=...` |

The chatbot reads student data through **tool calls**, so the model must support tool (function) calling. With a model that doesn't, it can still chat but can't see any coursework or calendar data.

## Run the tests

The tests need a Postgres database they're allowed to wipe; they refuse to run against Supabase. The quickest way to get one is Docker:

```powershell
docker run --name defog-test-db -e POSTGRES_USER=defog -e POSTGRES_PASSWORD=defog -e POSTGRES_DB=defog_test -p 5432:5432 -d postgres:16
python -m pytest
```

To use a different database, set `TEST_DATABASE_URL`. The tests never use the real internet or your `.env`: Canvas, Google and the breached-password service are replaced with in-memory fakes.

## Deploy

1. Set the environment variables from `.env.example` on your host, with `ENVIRONMENT=production`, `https://` values for `APP_URL` and `API_URL`, real `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` values, `EMAIL_BACKEND=smtp` and SMTP settings. In production the server refuses to start without these.
2. Run `alembic upgrade head` against Supabase.
3. Start the server with `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. One worker is enough at launch. The background sync loop runs inside the server process.

Keep `TOKEN_ENCRYPTION_KEY` safe and unchanged. If it changes, stored Canvas and Google tokens can't be read, and every student has to reconnect.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/health` | Server is up; shows the AI provider |
| GET | `/api/auth/options` | Whether Google sign-in is available |
| POST | `/api/auth/register` | Sign up with the form (sends a confirmation email) |
| POST | `/api/auth/verify-email` | Confirm the email from the emailed link |
| POST | `/api/auth/resend-verification` | Send the confirmation link again |
| POST | `/api/auth/login` | Email + password login (5 failures per 15 minutes, then locked) |
| POST | `/api/auth/logout`, `/api/auth/logout-all` | Sign out this device, or every device |
| POST | `/api/auth/refresh` | Mobile: new access + refresh token |
| POST | `/api/auth/forgot-password`, `/api/auth/reset-password` | Emailed single-use reset link, valid 1 hour |
| GET | `/api/auth/google/start`, `/api/auth/google/callback` | Continue with Google |
| POST | `/api/auth/google/link` | Link Google to an existing password account (password once) |
| POST | `/api/auth/mobile/exchange` | Mobile: finish Google sign-in |
| GET, PATCH, DELETE | `/api/me` | Profile; update; delete account and all data |
| GET | `/api/me/export` | Download all of the student's data as JSON |
| GET | `/api/connections` | Status of Canvas and Google Calendar |
| POST | `/api/connections/canvas/token` | Connect Canvas with a personal access token |
| GET | `/api/connections/canvas/start`, `/callback` | Connect Canvas with OAuth |
| GET | `/api/connections/google-calendar/start`, `/callback` | Connect Google Calendar |
| GET | `/api/connections/{id}/calendars` | Calendars the student can choose from |
| PATCH | `/api/connections/{id}` | Pick calendars; turn "Let Defog add events" on or off |
| POST | `/api/connections/{id}/sync` | Sync now |
| DELETE | `/api/connections/{id}` | Disconnect: revoke access and delete synced data |
| GET, POST | `/api/chats` | List or start chats |
| GET, DELETE | `/api/chats/{id}` | Read or delete a chat |
| POST | `/api/chats/{id}/messages` | Send a message; the answer streams back as Server-Sent Events |
| POST | `/api/chats/{id}/actions/{action_id}/confirm`, `/cancel` | Confirm or cancel a proposed calendar event |

Web clients authenticate with an HttpOnly session cookie and send `X-CSRF-Token` (from login or `GET /api/me`) on every POST, PATCH and DELETE. Mobile clients send `Authorization: Bearer <access token>`.

## How the chatbot uses student data

The model gets seven tools: `get_assignments`, `get_grades`, `get_announcements`, `search_course_content`, `get_events`, `find_free_time` and `create_event`. Each tool reads Defog's synced copy of the data and is limited to the logged-in student's user ID on the server side. The model never chooses whose data it sees and never sees tokens. If a source's data is more than an hour old, it's refreshed before the tool runs. `create_event` only proposes an event: nothing is written to Google Calendar until the student taps Confirm.

## Layout

```
app/
  main.py            app setup, routers, background sync loop
  config.py          settings read from .env
  db.py, models.py   Supabase/Postgres connection and tables
  security.py        password hashing, token encryption, breached-password check
  emails.py          verification and reset emails
  account.py         /api/me: profile, export, delete
  auth/              sign-up, login, sessions, CSRF, Google sign-in
  connections/       Canvas and Google Calendar: OAuth, tokens, sync
  chat/              chat endpoints, streaming, tools over student data
  llm.py, ai.py      AI providers (mock, Anthropic, OpenAI-compatible) with tool calling
  prompts.py         Defog's system prompt
migrations/          Alembic migrations (run: alembic upgrade head)
tests/               pytest suite with fake Canvas, Google and AI
```

## Cost guards

Each request is capped by `MAX_OUTPUT_TOKENS`, `MAX_HISTORY_MESSAGES`, `MAX_MESSAGE_CHARS` and `MAX_TOOL_ROUNDS`. Each student is also capped at `DAILY_TOKEN_LIMIT` tokens per day (UTC).
