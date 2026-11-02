import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, apiHref, type CalendarChoice, type Connection, type ConnectionsPage } from "./api";
import { errorText, Field, FormError, TextInput } from "./forms";
import { Link, navigate, type Route } from "./router";

const RESULT_MESSAGES: Record<string, string> = {
  canvas: "Canvas is connected. Your courses are syncing now.",
  google_calendar: "Google Calendar is connected. Your events are syncing now.",
  canvas_expired: "The Canvas sign-in took too long. Try again.",
  canvas_cancelled: "Canvas sign-in was cancelled.",
  canvas_failed: "Canvas sign-in didn't work. Try again, or use an access token.",
  google_calendar_expired: "The Google sign-in took too long. Try again.",
  google_calendar_cancelled: "Google Calendar access was cancelled.",
  google_calendar_failed: "Connecting Google Calendar didn't work. Try again.",
  google_calendar_scope: "Defog needs permission to see your calendar. Try again and leave the calendar box ticked.",
};

export function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function ConnectionsPage({ route }: { route: Route }) {
  const [page, setPage] = useState<ConnectionsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const onboarding = route.params.get("onboarding") === "1";
  const connected = route.params.get("connected");
  const failure = route.params.get("error");
  const polls = useRef(0);

  const load = useCallback(async () => {
    try {
      setPage(await api.connections());
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // While a first sync runs, refresh the status every 2 seconds (for up to 2 minutes).
  const syncing = page?.connections.some((c) => c.status === "syncing");
  useEffect(() => {
    if (!syncing) {
      polls.current = 0;
      return;
    }
    if (polls.current > 60) return;
    const timer = window.setTimeout(() => {
      polls.current += 1;
      void load();
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [syncing, page, load]);

  const canvas = page?.connections.find((c) => c.provider === "canvas");
  const google = page?.connections.find((c) => c.provider === "google_calendar");

  return (
    <div className="page">
      <div className="page-inner">
        <h1 className="page-title">{onboarding ? "Connect your accounts" : "Connections"}</h1>
        <p className="page-lede">
          {onboarding
            ? "Defog answers from your own coursework and calendar. Connect both now, or skip and do it later."
            : "Defog reads your coursework and calendar from these accounts. It never posts or submits anything to Canvas."}
        </p>
        {connected && RESULT_MESSAGES[connected] && <p className="banner banner-ok">{RESULT_MESSAGES[connected]}</p>}
        {failure && <p className="banner banner-error">{RESULT_MESSAGES[failure] ?? "That didn't work. Try again."}</p>}
        <FormError message={error} />

        {page && canvas && <CanvasCard conn={canvas} oauthSchools={page.canvas_oauth_schools} onChange={load} />}
        {page && google && <GoogleCard conn={google} enabled={page.google_enabled} onChange={load} />}

        {onboarding && (
          <div className="onboarding-actions">
            <button type="button" className="primary-button" onClick={() => navigate("/")}>
              {canvas?.status === "not_connected" && google?.status === "not_connected" ? "Skip for now" : "Start chatting"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ conn }: { conn: Connection }) {
  const labels: Record<Connection["status"], string> = {
    not_connected: "Not connected",
    syncing: "Syncing…",
    connected: "Connected",
    needs_attention: "Needs attention",
  };
  return <span className={`badge badge-${conn.status}`}>{labels[conn.status]}</span>;
}

function SyncLine({ conn, extra }: { conn: Connection; extra?: string }) {
  return (
    <p className="card-meta">
      {extra && <>{extra} · </>}Last synced {timeAgo(conn.last_synced_at)}
      {conn.last_sync_error && <span className="card-warning"> · {conn.last_sync_error}</span>}
    </p>
  );
}

function ConnectedActions({ conn, onChange }: { conn: Connection; onChange: () => Promise<void> }) {
  const [busy, setBusy] = useState<"sync" | "disconnect" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: "sync" | "disconnect") {
    if (!conn.id) return;
    setBusy(kind);
    setError(null);
    try {
      if (kind === "sync") await api.syncConnection(conn.id);
      else await api.disconnect(conn.id);
      await onChange();
    } catch (err) {
      setError(errorText(err));
      await onChange();
    } finally {
      setBusy(null);
      setConfirming(false);
    }
  }

  return (
    <>
      <div className="card-actions">
        {conn.status !== "needs_attention" && (
          <button type="button" className="ghost-button" onClick={() => run("sync")} disabled={busy !== null || conn.status === "syncing"}>
            {busy === "sync" ? "Syncing…" : "Sync now"}
          </button>
        )}
        {confirming ? (
          <>
            <button type="button" className="danger-button" onClick={() => run("disconnect")} disabled={busy !== null}>
              {busy === "disconnect" ? "Disconnecting…" : "Yes, disconnect and delete synced data"}
            </button>
            <button type="button" className="text-button" onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button type="button" className="text-button" onClick={() => setConfirming(true)} disabled={busy !== null}>
            Disconnect
          </button>
        )}
      </div>
      <FormError message={error} />
    </>
  );
}

// --- Canvas ---------------------------------------------------------------------------------------

function CanvasCard({ conn, oauthSchools, onChange }: { conn: Connection; oauthSchools: string[]; onChange: () => Promise<void> }) {
  const showForm = conn.status === "not_connected" || conn.status === "needs_attention";
  return (
    <section className="card">
      <header className="card-header">
        <div>
          <h2 className="card-title">Canvas</h2>
          <p className="card-sub">Courses, assignments, grades and announcements (read-only)</p>
        </div>
        <StatusBadge conn={conn} />
      </header>
      {conn.status === "needs_attention" && <p className="banner banner-error">{conn.status_detail}</p>}
      {conn.status !== "not_connected" && (
        <SyncLine
          conn={conn}
          extra={`${conn.base_url?.replace("https://", "")}${conn.course_count != null ? ` · ${conn.course_count} courses` : ""}`}
        />
      )}
      {conn.status !== "not_connected" && <ConnectedActions conn={conn} onChange={onChange} />}
      {showForm && <CanvasConnectForm initialUrl={conn.base_url ?? ""} oauthSchools={oauthSchools} onConnected={onChange} />}
    </section>
  );
}

function normalizeHost(raw: string): string {
  const trimmed = raw.trim().toLowerCase();
  if (!trimmed) return "";
  try {
    return new URL(trimmed.includes("://") ? trimmed : `https://${trimmed}`).host;
  } catch {
    return trimmed;
  }
}

function CanvasConnectForm({
  initialUrl,
  oauthSchools,
  onConnected,
}: {
  initialUrl: string;
  oauthSchools: string[];
  onConnected: () => Promise<void>;
}) {
  const [baseUrl, setBaseUrl] = useState(initialUrl.replace("https://", ""));
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const host = normalizeHost(baseUrl);
  const oauth = oauthSchools.some((s) => normalizeHost(s) === host);
  const [useToken, setUseToken] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.connectCanvasToken(baseUrl, token);
      setToken("");
      await onConnected();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const tokenUrl = host ? `https://${host}/profile/settings` : null;

  return (
    <div className="connect-form">
      <Field label="Your school's Canvas address" hint="The address you use to open Canvas, e.g. canvas.upenn.edu">
        <TextInput value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="canvas.yourschool.edu" inputMode="url" autoComplete="url" />
      </Field>
      {oauth && !useToken && (
        <div className="card-actions">
          <a className="primary-button" href={apiHref("/api/connections/canvas/start", { base_url: baseUrl })}>
            Sign in with Canvas
          </a>
          <button type="button" className="text-button" onClick={() => setUseToken(true)}>
            Use an access token instead
          </button>
        </div>
      )}
      {(!oauth || useToken) && host && (
        <form className="form" onSubmit={submit}>
          <ol className="steps">
            <li>
              In Canvas, open{" "}
              {tokenUrl ? (
                <a href={tokenUrl} target="_blank" rel="noreferrer">
                  Account → Settings
                </a>
              ) : (
                "Account → Settings"
              )}
              .
            </li>
            <li>
              Under <strong>Approved Integrations</strong>, choose <strong>+ New Access Token</strong>.
            </li>
            <li>Name it “Defog”, leave the expiry blank or pick a date, and generate it.</li>
            <li>Copy the token and paste it here. Canvas only shows it once.</li>
          </ol>
          <Field label="Access token" hint="Stored encrypted. Some schools turn these tokens off; if yours has, Canvas will refuse it.">
            <TextInput value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} minLength={10} required />
          </Field>
          <FormError message={error} />
          <button className="primary-button" disabled={busy}>
            {busy ? "Checking with Canvas…" : "Connect Canvas"}
          </button>
        </form>
      )}
    </div>
  );
}

// --- Google Calendar ---------------------------------------------------------------------------------

function GoogleCard({ conn, enabled, onChange }: { conn: Connection; enabled: boolean; onChange: () => Promise<void> }) {
  const [wantWrites, setWantWrites] = useState(false);
  const connectHref = (write: boolean) => apiHref("/api/connections/google-calendar/start", { write: String(write) });

  return (
    <section className="card">
      <header className="card-header">
        <div>
          <h2 className="card-title">Google Calendar</h2>
          <p className="card-sub">Your events and free time, from the calendars you pick</p>
        </div>
        <StatusBadge conn={conn} />
      </header>
      {!enabled && conn.status === "not_connected" && (
        <p className="card-meta">Google Calendar isn't set up on this server yet.</p>
      )}
      {enabled && conn.status === "not_connected" && (
        <>
          <label className="check">
            <input type="checkbox" checked={wantWrites} onChange={(e) => setWantWrites(e.target.checked)} />
            <span>Let Defog add events (deadlines and study blocks, only after you confirm each one)</span>
          </label>
          <div className="card-actions">
            <a className="primary-button" href={connectHref(wantWrites)}>
              Connect Google Calendar
            </a>
          </div>
        </>
      )}
      {conn.status === "needs_attention" && (
        <>
          <p className="banner banner-error">{conn.status_detail}</p>
          <div className="card-actions">
            <a className="primary-button" href={connectHref(Boolean(conn.allow_writes))}>
              Reconnect Google Calendar
            </a>
          </div>
        </>
      )}
      {conn.status !== "not_connected" && (
        <>
          <SyncLine conn={conn} />
          {conn.status !== "needs_attention" && conn.id && <CalendarSettings conn={conn} onChange={onChange} connectHref={connectHref} />}
          <ConnectedActions conn={conn} onChange={onChange} />
        </>
      )}
    </section>
  );
}

function CalendarSettings({
  conn,
  onChange,
  connectHref,
}: {
  conn: Connection;
  onChange: () => Promise<void>;
  connectHref: (write: boolean) => string;
}) {
  const [calendars, setCalendars] = useState<CalendarChoice[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function openPicker() {
    setOpen(true);
    setError(null);
    try {
      const list = await api.calendars(conn.id!);
      setCalendars(list);
      // The main calendar is stored as "primary", matching what Defog uses by default.
      setSelected(list.filter((c) => c.selected).map((c) => (c.primary ? "primary" : c.id)));
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function saveCalendars() {
    setSaving(true);
    setError(null);
    try {
      await api.updateConnection(conn.id!, { calendar_ids: selected });
      setOpen(false);
      await onChange();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

  async function toggleWrites(on: boolean) {
    setError(null);
    if (on && !conn.can_write) {
      // Needs the extra "add events" permission from Google.
      window.location.assign(connectHref(true));
      return;
    }
    try {
      await api.updateConnection(conn.id!, { allow_writes: on });
      await onChange();
    } catch (err) {
      setError(errorText(err));
    }
  }

  const count = conn.calendar_ids?.length ?? 1;
  return (
    <div className="calendar-settings">
      <label className="check">
        <input type="checkbox" checked={Boolean(conn.allow_writes)} onChange={(e) => void toggleWrites(e.target.checked)} />
        <span>
          Let Defog add events{" "}
          <span className="field-hint">(you confirm each one first{conn.can_write ? "" : "; Google will ask for permission"})</span>
        </span>
      </label>
      {!open ? (
        <p className="card-meta">
          Reading {count === 1 ? "1 calendar" : `${count} calendars`} ·{" "}
          <button type="button" className="text-button" onClick={() => void openPicker()}>
            Choose calendars
          </button>
        </p>
      ) : (
        <div className="calendar-picker">
          {!calendars && !error && <p className="card-meta">Loading your calendars…</p>}
          {calendars?.map((c) => (
            <label key={c.id} className="check">
              <input
                type="checkbox"
                checked={selected.includes(c.id) || (c.primary && selected.includes("primary"))}
                onChange={(e) => {
                  const ids = selected.filter((id) => id !== c.id && !(c.primary && id === "primary"));
                  setSelected(e.target.checked ? [...ids, c.primary ? "primary" : c.id] : ids);
                }}
              />
              <span>
                {c.name}
                {c.primary && <span className="field-hint"> (main)</span>}
              </span>
            </label>
          ))}
          {calendars && (
            <div className="card-actions">
              <button type="button" className="primary-button" onClick={() => void saveCalendars()} disabled={saving || selected.length === 0}>
                Save
              </button>
              <button type="button" className="text-button" onClick={() => setOpen(false)}>
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
      <FormError message={error} />
    </div>
  );
}

export function NoDataBanner() {
  return (
    <p className="banner">
      Connect Canvas and Google Calendar so Defog can answer from your own coursework and schedule.{" "}
      <Link href="/connections">Go to Connections</Link>
    </p>
  );
}
