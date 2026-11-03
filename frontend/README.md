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

## Deploy to Cloudflare

The site is a Cloudflare Worker named `defog` (`wrangler.jsonc`). It serves `dist/` (unknown paths get `index.html`, so client-side routes work), and `worker/index.js` forwards every `/api/*` request to the backend on Cloud Run. The browser only ever talks to one site, so the session cookie is first-party and no CORS is needed. Leave `VITE_API_URL` empty.

1. Deploy the backend first (`../backend/README.md`, "Deploy").
2. Put the Cloud Run URL it prints in `wrangler.jsonc` as `BACKEND_URL`.
3. From the `frontend` folder, with `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` set:

   ```bash
   npm run deploy
   ```

The Worker is live at https://defog.defog-frontend.workers.dev. To serve it at `defog.fit`, add the domain under the Worker's **Settings → Domains & Routes** in the Cloudflare dashboard (this replaces the domain's current A record). `APP_URL` in `../backend/deploy/cloudrun.env.yaml` must match the address students use.

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
worker/
  index.js         Cloudflare Worker: serves the site, forwards /api/* to the backend
```

The session lives in an HttpOnly cookie set by the API. If the API runs on its own subdomain (e.g. `api.defog.fit`), it must be on the same site as the web app (`defog.fit`) so the browser sends that cookie.
