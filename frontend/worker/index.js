// Serves the web app and forwards /api/* to the backend on Cloud Run, so the browser talks
// to a single site: the session cookie stays first-party and no CORS is needed.
// Static files are served by the assets binding; this script only runs for /api/*
// (see run_worker_first in wrangler.jsonc).

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }
    // Only Cloud Scheduler calls this, directly on Cloud Run.
    if (url.pathname.startsWith("/api/internal/")) {
      return new Response("Not Found", { status: 404 });
    }
    if (!env.BACKEND_URL) {
      return Response.json({ detail: "The Defog server isn't set up yet." }, { status: 503 });
    }

    const target = new URL(url.pathname + url.search, env.BACKEND_URL);
    const headers = new Headers(request.headers);
    headers.set("X-Forwarded-Host", url.host);
    headers.set("X-Forwarded-Proto", url.protocol.replace(":", ""));
    return fetch(target, {
      method: request.method,
      headers,
      body: request.body,
      // Pass OAuth redirects (to Google, Canvas) back to the browser instead of following them.
      redirect: "manual",
    });
  },
};
