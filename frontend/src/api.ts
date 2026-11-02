// Talks to the Defog backend. The web session lives in an HttpOnly cookie; state-changing
// requests also send the CSRF token the server gave us at login (or from /api/me).

// Empty in development (Vite proxies /api to localhost:8000).
// In production, set VITE_API_URL to the backend's address, e.g. https://api.defog.fit
export const API_URL = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null | undefined) {
  csrfToken = token ?? null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

// --- Shapes -----------------------------------------------------------------------------

export type Year = "Freshman" | "Sophomore" | "Junior" | "Senior" | "Graduate" | "Other";
export const YEARS: Year[] = ["Freshman", "Sophomore", "Junior", "Senior", "Graduate", "Other"];

export interface User {
  id: string;
  name: string;
  email: string;
  email_verified: boolean;
  school: string | null;
  year: Year | null;
  major: string | null;
  time_zone: string | null;
  consent_at: string | null;
  profile_complete: boolean;
  has_password: boolean;
  google_linked: boolean;
}

export interface AuthResponse {
  user: User;
  csrf_token?: string | null;
}

export interface Connection {
  provider: "canvas" | "google_calendar";
  id: string | null;
  status: "not_connected" | "syncing" | "connected" | "needs_attention";
  status_detail: string | null;
  auth_type: "oauth" | "token" | null;
  base_url: string | null;
  last_synced_at: string | null;
  last_sync_error: string | null;
  calendar_ids: string[] | null;
  allow_writes: boolean | null;
  can_write: boolean | null;
  course_count: number | null;
}

export interface ConnectionsPage {
  connections: Connection[];
  canvas_oauth_schools: string[];
  google_enabled: boolean;
}

export interface CalendarChoice {
  id: string;
  name: string;
  primary: boolean;
  selected: boolean;
}

export interface ActionCard {
  id: string;
  kind: string;
  status: "pending" | "running" | "done" | "cancelled" | "failed" | "expired";
  summary: string;
  title: string;
  start: string;
  end: string;
  result: { url?: string };
}

export interface ChatMessage {
  id?: string;
  role: "user" | "assistant";
  content: string;
  sources?: string[];
  actions?: ActionCard[];
}

export interface ChatSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ChatDetail extends ChatSummary {
  messages: ChatMessage[];
}

// --- Requests ---------------------------------------------------------------------------

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("Can't reach the Defog server. Check your connection, then try again.", 0);
  }
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    // FastAPI validation errors arrive as a list of { loc, msg } objects.
    if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      const first = body.detail[0];
      const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : "";
      const msg = String(first.msg).replace(/^Value error, /, "");
      return FIELD_NAMES[field] ? `${FIELD_NAMES[field]}: ${msg}` : msg;
    }
  } catch {
    // The response wasn't JSON; fall through to the generic message.
  }
  return `The server returned an error (${res.status}). Try again in a moment.`;
}

const FIELD_NAMES: Record<string, string> = {
  name: "Name",
  email: "Email",
  password: "Password",
  school: "School",
  time_zone: "Time zone",
  accept_terms: "Terms",
  base_url: "Canvas address",
  token: "Access token",
};

export const api = {
  options: () => request<{ google_enabled: boolean }>("GET", "/api/auth/options"),
  me: () => request<{ user: User; csrf_token: string | null }>("GET", "/api/me"),
  register: (body: Record<string, unknown>) => request<AuthResponse>("POST", "/api/auth/register", body),
  login: (email: string, password: string) =>
    request<AuthResponse>("POST", "/api/auth/login", { email, password }),
  logout: () => request<void>("POST", "/api/auth/logout"),
  logoutAll: () => request<void>("POST", "/api/auth/logout-all"),
  verifyEmail: (token: string) => request<User>("POST", "/api/auth/verify-email", { token }),
  resendVerification: () => request<void>("POST", "/api/auth/resend-verification"),
  forgotPassword: (email: string) => request<{ detail: string }>("POST", "/api/auth/forgot-password", { email }),
  resetPassword: (token: string, password: string) =>
    request<void>("POST", "/api/auth/reset-password", { token, password }),
  linkGoogle: (token: string, password: string) =>
    request<AuthResponse>("POST", "/api/auth/google/link", { token, password }),
  updateMe: (body: Record<string, unknown>) => request<User>("PATCH", "/api/me", body),
  deleteMe: (confirm_email: string) => request<void>("DELETE", "/api/me", { confirm_email }),

  connections: () => request<ConnectionsPage>("GET", "/api/connections"),
  connectCanvasToken: (base_url: string, token: string) =>
    request<Connection>("POST", "/api/connections/canvas/token", { base_url, token }),
  calendars: (id: string) => request<CalendarChoice[]>("GET", `/api/connections/${id}/calendars`),
  updateConnection: (id: string, body: { calendar_ids?: string[]; allow_writes?: boolean }) =>
    request<Connection>("PATCH", `/api/connections/${id}`, body),
  syncConnection: (id: string) => request<Connection>("POST", `/api/connections/${id}/sync`),
  disconnect: (id: string) => request<void>("DELETE", `/api/connections/${id}`),

  chats: () => request<ChatSummary[]>("GET", "/api/chats"),
  createChat: () => request<ChatSummary>("POST", "/api/chats"),
  chat: (id: string) => request<ChatDetail>("GET", `/api/chats/${id}`),
  deleteChat: (id: string) => request<void>("DELETE", `/api/chats/${id}`),
  confirmAction: (chatId: string, actionId: string) =>
    request<{ action: ActionCard; message: ChatMessage }>(
      "POST",
      `/api/chats/${chatId}/actions/${actionId}/confirm`,
    ),
  cancelAction: (chatId: string, actionId: string) =>
    request<{ action: ActionCard }>("POST", `/api/chats/${chatId}/actions/${actionId}/cancel`),
};

/** Full-page navigations (OAuth) go straight to the API. */
export function apiHref(path: string, params?: Record<string, string>): string {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  return `${API_URL}${path}${query}`;
}

// --- Streaming chat ---------------------------------------------------------------------

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onTool: (label: string) => void;
  onAction: (card: ActionCard) => void;
  onDone: (message: ChatMessage & { chat_title?: string }) => void;
}

/** Send a message and read the Server-Sent Events reply. Rejects with ApiError on failure. */
export async function streamMessage(
  chatId: string,
  content: string,
  handlers: StreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/chats/${chatId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken ?? "" },
      credentials: "include",
      body: JSON.stringify({ content }),
      signal,
    });
  } catch (err) {
    if (signal.aborted) throw err;
    throw new ApiError("Can't reach the Defog server. Check your connection, then try again.", 0);
  }
  if (!res.ok || !res.body) throw new ApiError(await errorMessage(res), res.status);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (!data.length) continue;
      const payload = JSON.parse(data.join("\n"));
      if (event === "delta") handlers.onDelta(payload.text);
      else if (event === "tool") handlers.onTool(payload.label);
      else if (event === "action") handlers.onAction(payload);
      else if (event === "done") {
        finished = true;
        handlers.onDone(payload.message);
      } else if (event === "error") throw new ApiError(payload.detail, 502);
    }
  }
  if (!finished) throw new ApiError("The answer was cut off. Try again.", 0);
}
