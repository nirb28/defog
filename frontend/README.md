# Defog frontend

React + Vite + TypeScript web app: sign-up and login (including Google), onboarding, the Connections page for Canvas and Google Calendar, account settings, and the chat. Answers stream in as they're written and render Markdown, tables and math (KaTeX).

## Run it locally

Start the backend first (see `../backend/README.md`), then in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. In development, Vite forwards every `/api` request to the backend on port 8000, so no extra setup is needed.

## Build

```powershell
npm run build
```

This type-checks the code and writes the production site to `dist/`.

## Deploy to Cloudflare Pages

In the Cloudflare dashboard, go to **Workers & Pages → Create → Pages → Connect to Git** and pick the `defog` repo. Then use:

| Setting | Value |
| --- | --- |
| Root directory | `frontend` |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Environment variable | `VITE_API_URL` = your backend's public address |

Once it's deployed, add `defog.fit` under the project's **Custom domains**.

## Layout

```
src/
  App.tsx          sign-in gate, page routing, header
  router.tsx       tiny client-side router (no library)
  api.ts           backend calls, CSRF handling, streamed chat replies
  AuthPages.tsx    log in, sign up, confirm email, reset password, link Google, finish profile
  Connections.tsx  Canvas and Google Calendar: connect, pick calendars, sync, disconnect
  Chat.tsx         chat list, messages, streaming, calendar confirmation cards
  Settings.tsx     profile, log out (everywhere), download data, delete account
  forms.tsx        shared form fields (time zone, year, school)
  Markdown.tsx     Markdown + math rendering (loaded only after the first answer)
  styles.css       colours, type and layout, with light and dark themes
```

The session lives in an HttpOnly cookie set by the API. If the API runs on its own subdomain (e.g. `api.defog.fit`), it must be on the same site as the web app (`defog.fit`) so the browser sends that cookie.
