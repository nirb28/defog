# Defog

An AI study assistant for students. Students sign up (with a form or with Google), connect Canvas and Google Calendar, and ask a chatbot about their own assignments, grades, announcements and free time, as well as anything they're studying.

| Folder | What it is | Stack | Hosting |
| --- | --- | --- | --- |
| `frontend/` | Web app | React, Vite, TypeScript | Cloudflare Pages (defog.fit) |
| `backend/` | API: accounts, Canvas and Calendar sync, chatbot | Python, FastAPI | Any Python host (e.g. Render) |
| (Supabase) | Database | Postgres | Supabase |

The browser never sees an API key or a Canvas or Google token: the web app calls the backend, and only the backend talks to the AI model, Canvas and Google.

## Quick start

1. **Backend** (terminal 1). Put your Supabase connection string in `.env` first (see `backend/README.md`):

   ```powershell
   cd backend
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements-dev.txt
   copy .env.example .env
   alembic upgrade head
   uvicorn app.main:app --reload
   ```

2. **Frontend** (terminal 2):

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

3. Open http://localhost:5173 and create an account. In development the confirmation link is printed in the backend terminal.

The backend starts with the AI in mock mode, so it runs with no AI key and costs nothing. `backend/README.md` explains how to set up Supabase, Google sign-in, Google Calendar, Canvas and a real AI provider.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer
- A Supabase project (or any Postgres 14+ database)
