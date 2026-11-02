import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, ApiError, streamMessage, type ActionCard, type ChatMessage, type ChatSummary } from "./api";
import { NoDataBanner } from "./Connections";
import { errorText } from "./forms";
import { Link, navigate, type Route } from "./router";

const Markdown = lazy(() => import("./Markdown"));

const SUGGESTIONS = [
  "What's due this week?",
  "How am I doing in my classes?",
  "When am I free tomorrow afternoon?",
  "Any new announcements?",
  "Plan study time for my next exam",
];

interface LiveMessage extends ChatMessage {
  streaming?: boolean;
  status?: string | null;
}

export default function ChatPage({ route }: { route: Route }) {
  const chatId = route.path.startsWith("/c/") ? route.path.slice(3) : null;
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [messages, setMessages] = useState<LiveMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<{ text: string; retry: string | null } | null>(null);
  const [hasData, setHasData] = useState(true);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Set while we create a chat and navigate to it, so the load effect doesn't wipe the stream.
  const creatingRef = useRef<string | null>(null);

  const refreshChats = useCallback(() => {
    api.chats().then(setChats, () => undefined);
  }, []);

  useEffect(() => {
    refreshChats();
    api.connections().then(
      (p) => setHasData(p.connections.some((c) => c.status !== "not_connected")),
      () => undefined,
    );
  }, [refreshChats]);

  // Load the selected chat.
  useEffect(() => {
    if (chatId && creatingRef.current === chatId) {
      creatingRef.current = null;
      return;
    }
    abortRef.current?.abort();
    setPending(false);
    setError(null);
    setSidebarOpen(false);
    if (!chatId) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    api.chat(chatId).then(
      (detail) => !cancelled && setMessages(detail.messages),
      (err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) navigate("/", { replace: true });
        else setError({ text: errorText(err), retry: null });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [chatId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, error]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [draft]);

  const updateLast = (fn: (m: LiveMessage) => LiveMessage) =>
    setMessages((list) => (list.length ? [...list.slice(0, -1), fn(list[list.length - 1])] : list));

  async function send(text: string) {
    const content = text.trim();
    if (!content || pending) return;
    setPending(true);
    setError(null);
    setDraft("");
    setMessages((list) => [
      ...list,
      { role: "user", content },
      { role: "assistant", content: "", streaming: true, status: null, actions: [] },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      let id = chatId;
      if (!id) {
        const chat = await api.createChat();
        id = chat.id;
        creatingRef.current = id;
        setChats((list) => [chat, ...list]);
        navigate(`/c/${id}`);
      }
      await streamMessage(
        id,
        content,
        {
          onDelta: (t) => updateLast((m) => ({ ...m, content: m.content + t, status: null })),
          onTool: (label) => updateLast((m) => ({ ...m, status: label })),
          onAction: (card) => updateLast((m) => ({ ...m, actions: [...(m.actions ?? []), card] })),
          onDone: (saved) => {
            updateLast(() => ({ ...saved, streaming: false }));
            // The server's list is the source of truth (new title, newest first).
            refreshChats();
          },
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      // Nothing was saved: take the question back out so "Try again" sends it fresh.
      setMessages((list) => list.slice(0, -2));
      setError({ text: errorText(err), retry: content });
    } finally {
      if (!controller.signal.aborted) {
        setPending(false);
        inputRef.current?.focus();
      }
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void send(draft);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(draft);
    }
  }

  async function removeChat(id: string) {
    try {
      await api.deleteChat(id);
      setChats((list) => list.filter((c) => c.id !== id));
      if (id === chatId) navigate("/");
    } catch (err) {
      setError({ text: errorText(err), retry: null });
    }
  }

  function updateAction(card: ActionCard, followUp?: ChatMessage) {
    setMessages((list) => {
      const next = list.map((m) =>
        m.actions?.some((a) => a.id === card.id) ? { ...m, actions: m.actions.map((a) => (a.id === card.id ? card : a)) } : m,
      );
      return followUp ? [...next, followUp] : next;
    });
  }

  const empty = messages.length === 0;

  return (
    <div className={`chat-layout${sidebarOpen ? " sidebar-open" : ""}`}>
      <aside className="sidebar" aria-label="Your chats">
        <button type="button" className="primary-button new-chat" onClick={() => navigate("/")}>
          New chat
        </button>
        <nav className="chat-list">
          {chats.map((c) => (
            <div key={c.id} className={`chat-item${c.id === chatId ? " active" : ""}`}>
              <Link href={`/c/${c.id}`} className="chat-link">
                {c.title}
              </Link>
              <button type="button" className="chat-delete" aria-label={`Delete “${c.title}”`} onClick={() => void removeChat(c.id)}>
                ×
              </button>
            </div>
          ))}
          {chats.length === 0 && <p className="sidebar-empty">Your chats will appear here.</p>}
        </nav>
      </aside>
      {sidebarOpen && <button type="button" className="sidebar-scrim" aria-label="Close chats" onClick={() => setSidebarOpen(false)} />}

      <div className="chat-main">
        <div className="chat-topbar">
          <button type="button" className="ghost-button" onClick={() => setSidebarOpen((o) => !o)} aria-expanded={sidebarOpen}>
            Chats
          </button>
          <button type="button" className="ghost-button" onClick={() => navigate("/")}>
            New chat
          </button>
        </div>
        <main className="thread">
          <div className="thread-inner" role="log" aria-live="polite">
            {empty ? (
              <section className="welcome">
                <h1 className="wordmark wordmark--hero">defog</h1>
                <p className="lede">Ask about your deadlines, grades, announcements and schedule, or anything you're studying.</p>
                {!hasData && <NoDataBanner />}
                <div className="suggestions">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} type="button" className="suggestion" onClick={() => void send(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </section>
            ) : (
              messages.map((m, i) =>
                m.role === "user" ? (
                  <p key={m.id ?? i} className="msg msg-user">
                    {m.content}
                  </p>
                ) : (
                  <AssistantMessage key={m.id ?? i} message={m} chatId={chatId} onAction={updateAction} />
                ),
              )
            )}
            {error && (
              <div className="error" role="alert">
                <p>{error.text}</p>
                {error.retry && (
                  <button type="button" className="ghost-button" onClick={() => void send(error.retry!)}>
                    Try again
                  </button>
                )}
              </div>
            )}
            <div ref={endRef} />
          </div>
        </main>

        <footer className="composer">
          <form className="composer-form" onSubmit={onSubmit}>
            <label htmlFor="message" className="visually-hidden">
              Message Defog
            </label>
            <textarea
              id="message"
              ref={inputRef}
              rows={1}
              value={draft}
              maxLength={8000}
              placeholder="Ask about your classes, deadlines or anything you're studying"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
              autoFocus
            />
            <button type="submit" className="send-button" disabled={pending || !draft.trim()}>
              Send
            </button>
          </form>
          <p className="disclaimer">Defog can make mistakes. Check important dates and grades in Canvas.</p>
        </footer>
      </div>
    </div>
  );
}

function AssistantMessage({
  message,
  chatId,
  onAction,
}: {
  message: LiveMessage;
  chatId: string | null;
  onAction: (card: ActionCard, followUp?: ChatMessage) => void;
}) {
  const waiting = message.streaming && !message.content;
  return (
    <div className="msg msg-assistant">
      {waiting && (
        <p className="thinking" aria-label="Defog is thinking">
          {message.status ?? "Thinking"}
        </p>
      )}
      {message.content && (
        <Suspense fallback={<p className="plain">{message.content}</p>}>
          <Markdown>{message.content}</Markdown>
        </Suspense>
      )}
      {message.streaming && message.content && message.status && <p className="tool-status">{message.status}…</p>}
      {message.actions?.map((a) => <ActionCardView key={a.id} card={a} chatId={chatId} onChange={onAction} />)}
      {!message.streaming && message.sources && message.sources.length > 0 && (
        <p className="sources">From {message.sources.join(" · ")}</p>
      )}
    </div>
  );
}

function ActionCardView({
  card,
  chatId,
  onChange,
}: {
  card: ActionCard;
  chatId: string | null;
  onChange: (card: ActionCard, followUp?: ChatMessage) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(kind: "confirm" | "cancel") {
    if (!chatId) return;
    setBusy(true);
    setError(null);
    try {
      if (kind === "confirm") {
        const res = await api.confirmAction(chatId, card.id);
        onChange(res.action, res.message);
      } else {
        onChange((await api.cancelAction(chatId, card.id)).action);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`action-card action-${card.status}`}>
      <p className="action-summary">{card.summary}</p>
      {card.status === "pending" && (
        <div className="card-actions">
          <button type="button" className="primary-button" onClick={() => void act("confirm")} disabled={busy || !chatId}>
            {busy ? "Adding…" : "Confirm"}
          </button>
          <button type="button" className="text-button" onClick={() => void act("cancel")} disabled={busy || !chatId}>
            Cancel
          </button>
        </div>
      )}
      {card.status === "done" && (
        <p className="action-state">
          Added to Google Calendar.
          {card.result.url && (
            <>
              {" "}
              <a href={card.result.url} target="_blank" rel="noreferrer">
                Open
              </a>
            </>
          )}
        </p>
      )}
      {card.status === "running" && <p className="action-state">Adding…</p>}
      {card.status === "cancelled" && <p className="action-state">Cancelled.</p>}
      {card.status === "expired" && <p className="action-state">This suggestion expired. Ask again to add it.</p>}
      {error && (
        <p className="form-error" role="alert">
          {error} {/Connections/i.test(error) && <Link href="/connections">Open Connections</Link>}
        </p>
      )}
    </div>
  );
}
