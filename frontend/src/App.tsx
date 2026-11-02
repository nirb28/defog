import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, setCsrfToken, type User } from "./api";
import {
  AuthLayout,
  CheckEmailPage,
  CompleteProfilePage,
  ForgotPasswordPage,
  LinkGooglePage,
  LoginPage,
  ResetPasswordPage,
  SignupPage,
  VerifyEmailPage,
} from "./AuthPages";
import { Link, navigate, useRoute, type Route } from "./router";

const ChatPage = lazy(() => import("./Chat"));
const ConnectionsPage = lazy(() => import("./Connections"));
const SettingsPage = lazy(() => import("./Settings"));

// Pages anyone can open. Signed-in students are sent on from the first four.
const PUBLIC = new Set(["/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/link-google", "/privacy", "/terms"]);
const GUEST_ONLY = new Set(["/login", "/signup", "/forgot-password", "/link-google"]);

export default function App() {
  const route = useRoute();
  const [user, setUser] = useState<User | null | undefined>(undefined); // undefined = still loading
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadMe = useCallback(async () => {
    try {
      const me = await api.me();
      setCsrfToken(me.csrf_token);
      setUser(me.user);
      setLoadError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setCsrfToken(null);
        setUser(null);
      } else {
        setLoadError(err instanceof Error ? err.message : "Can't reach Defog.");
      }
    }
  }, []);

  useEffect(() => {
    void loadMe();
  }, [loadMe]);

  const signedIn = useCallback((u: User) => {
    setUser(u);
    if (!u.email_verified) navigate("/", { replace: true });
    else if (!u.profile_complete) navigate("/complete-profile", { replace: true });
    else if (GUEST_ONLY.has(window.location.pathname)) navigate("/", { replace: true });
  }, []);

  const signedOut = useCallback(() => {
    setCsrfToken(null);
    setUser(null);
    navigate("/login", { replace: true });
  }, []);

  if (user === undefined) {
    return loadError ? (
      <AuthLayout title="Can't reach Defog">
        <p className="auth-text">{loadError}</p>
        <button type="button" className="primary-button" onClick={() => void loadMe()}>
          Try again
        </button>
      </AuthLayout>
    ) : (
      <div className="loading" aria-label="Loading">
        <span className="wordmark thinking">defog</span>
      </div>
    );
  }

  if (route.path === "/privacy" || route.path === "/terms") return <PolicyPage kind={route.path.slice(1)} />;

  // --- Signed out ---
  if (user === null) {
    switch (route.path) {
      case "/signup":
        return <SignupPage onSignedIn={signedIn} />;
      case "/forgot-password":
        return <ForgotPasswordPage />;
      case "/reset-password":
        return <ResetPasswordPage route={route} />;
      case "/verify-email":
        return <VerifyEmailPage route={route} user={null} onUser={signedIn} />;
      case "/link-google":
        return <LinkGooglePage route={route} onSignedIn={signedIn} />;
      default:
        if (route.path !== "/login") return <Redirect to="/login" />;
        return <LoginPage route={route} onSignedIn={signedIn} />;
    }
  }

  // --- Signed in, but not finished setting up ---
  if (route.path === "/verify-email") return <VerifyEmailPage route={route} user={user} onUser={setUser} />;
  if (route.path === "/reset-password") return <ResetPasswordPage route={route} />;
  if (!user.email_verified) return <CheckEmailPage user={user} onLogout={() => void api.logout().finally(signedOut)} />;
  if (!user.profile_complete) return <CompleteProfilePage user={user} onUser={setUser} />;
  if (GUEST_ONLY.has(route.path) || route.path === "/complete-profile") return <Redirect to="/" />;

  return (
    <Shell route={route}>
      <Suspense fallback={null}>{page(route, user, setUser, signedOut)}</Suspense>
    </Shell>
  );
}

function page(route: Route, user: User, setUser: (u: User) => void, signedOut: () => void) {
  if (route.path === "/connections") return <ConnectionsPage route={route} />;
  if (route.path === "/settings") return <SettingsPage user={user} onUser={setUser} onSignedOut={signedOut} />;
  if (route.path === "/" || route.path.startsWith("/c/")) return <ChatPage route={route} />;
  if (PUBLIC.has(route.path)) return null;
  return (
    <div className="page">
      <div className="page-inner">
        <h1 className="page-title">Page not found</h1>
        <Link href="/">Back to chat</Link>
      </div>
    </div>
  );
}

/** Navigate after rendering (navigating during render would update state mid-render). */
function Redirect({ to }: { to: string }) {
  useEffect(() => navigate(to, { replace: true }), [to]);
  return null;
}

function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const tab = route.path === "/connections" ? "connections" : route.path === "/settings" ? "settings" : "chat";
  return (
    <div className="shell">
      <header className="header">
        <Link href="/" className="wordmark">
          defog
        </Link>
        <nav className="nav">
          <Link href="/" className={`nav-link${tab === "chat" ? " active" : ""}`}>
            Chat
          </Link>
          <Link href="/connections" className={`nav-link${tab === "connections" ? " active" : ""}`}>
            Connections
          </Link>
          <Link href="/settings" className={`nav-link${tab === "settings" ? " active" : ""}`}>
            Account
          </Link>
        </nav>
      </header>
      <div className="shell-body">{children}</div>
    </div>
  );
}

function PolicyPage({ kind }: { kind: string }) {
  return (
    <AuthLayout title={kind === "privacy" ? "Privacy" : "Terms"} footer={<Link href="/">Back to Defog</Link>}>
      {kind === "privacy" ? (
        <div className="auth-text policy">
          <p>Defog collects only what its features need: your account details, and the Canvas and Google Calendar data you choose to connect.</p>
          <ul>
            <li>Your grades and coursework are treated as education records and are only ever shown to you.</li>
            <li>Your data is never used to train AI models.</li>
            <li>Canvas and Google tokens are stored encrypted and never leave our servers.</li>
            <li>Disconnecting a source deletes its synced data. Deleting your account deletes everything.</li>
            <li>You can download all of your data from the Account page at any time.</li>
          </ul>
          <p>The full privacy policy will be published here before public launch.</p>
        </div>
      ) : (
        <p className="auth-text">The full terms of service will be published here before public launch.</p>
      )}
    </AuthLayout>
  );
}
