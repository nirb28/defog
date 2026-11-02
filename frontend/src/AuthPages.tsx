import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, apiHref, setCsrfToken, type User } from "./api";
import { detectTimeZone, errorText, Field, FormError, SchoolInput, TextInput, TimeZoneSelect, YearSelect } from "./forms";
import { Link, navigate, type Route } from "./router";

type SignedIn = (user: User) => void;

const GOOGLE_ERRORS: Record<string, string> = {
  google_expired: "Google sign-in took too long. Try again.",
  google_cancelled: "Google sign-in was cancelled.",
  google_failed: "Google sign-in didn't work. Try again, or use your email and password.",
  google_other_account: "That email is already linked to a different Google account.",
};

export function AuthLayout({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="auth">
      <div className="auth-card">
        <Link href="/" className="wordmark auth-wordmark">
          defog
        </Link>
        <h1 className="auth-title">{title}</h1>
        {children}
      </div>
      {footer && <div className="auth-footer">{footer}</div>}
    </div>
  );
}

function GoogleButton({ label }: { label: string }) {
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    api.options().then((o) => setEnabled(o.google_enabled), () => setEnabled(false));
  }, []);
  if (!enabled) return null;
  return (
    <>
      <a className="google-button" href={apiHref("/api/auth/google/start")}>
        <GoogleMark />
        {label}
      </a>
      <div className="divider">
        <span>or</span>
      </div>
    </>
  );
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.7 1.2 9.2 3.6l6.9-6.9C35.9 2.4 30.4 0 24 0 14.6 0 6.6 5.4 2.7 13.3l8 6.2C12.6 13.6 17.8 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.4 5.7c4.3-4 6.9-9.9 6.9-17z" />
      <path fill="#FBBC05" d="M10.7 28.5c-.5-1.4-.8-2.9-.8-4.5s.3-3.1.8-4.5l-8-6.2C1 16.6 0 20.2 0 24s1 7.4 2.7 10.7l8-6.2z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.4-5.7c-2.1 1.4-4.8 2.2-8.5 2.2-6.2 0-11.4-4.1-13.3-9.8l-8 6.2C6.6 42.6 14.6 48 24 48z" />
    </svg>
  );
}

// --- Login ---------------------------------------------------------------------------------------

export function LoginPage({ route, onSignedIn }: { route: Route; onSignedIn: SignedIn }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(GOOGLE_ERRORS[route.params.get("error") ?? ""] ?? null);
  const notice = route.params.get("notice");

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(email, password);
      setCsrfToken(res.csrf_token);
      onSignedIn(res.user);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Log in"
      footer={
        <>
          New to Defog? <Link href="/signup">Create an account</Link>
        </>
      }
    >
      {notice === "verified" && <p className="form-notice">Email confirmed. Log in to continue.</p>}
      {notice === "reset" && <p className="form-notice">Password changed. Log in with your new password.</p>}
      <GoogleButton label="Continue with Google" />
      <form className="form" onSubmit={submit}>
        <Field label="Email">
          <TextInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
        </Field>
        <Field label="Password">
          <TextInput
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </Field>
        <FormError message={error} />
        <button className="primary-button" disabled={busy}>
          {busy ? "Logging in…" : "Log in"}
        </button>
        <Link href="/forgot-password" className="quiet-link">
          Forgot your password?
        </Link>
      </form>
    </AuthLayout>
  );
}

// --- Sign up -------------------------------------------------------------------------------------

export function SignupPage({ onSignedIn }: { onSignedIn: SignedIn }) {
  const [form, setForm] = useState({
    name: "",
    email: "",
    password: "",
    school: "",
    year: "",
    major: "",
    time_zone: detectTimeZone(),
    accept_terms: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (key: keyof typeof form) => (value: string | boolean) => setForm((f) => ({ ...f, [key]: value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.register({
        ...form,
        year: form.year || null,
        major: form.major.trim() || null,
      });
      setCsrfToken(res.csrf_token);
      onSignedIn(res.user);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthLayout
      title="Create your account"
      footer={
        <>
          Already have an account? <Link href="/login">Log in</Link>
        </>
      }
    >
      <GoogleButton label="Sign up with Google" />
      <form className="form" onSubmit={submit}>
        <Field label="Full name">
          <TextInput value={form.name} onChange={(e) => set("name")(e.target.value)} maxLength={100} autoComplete="name" required />
        </Field>
        <Field label="Email" hint="We'll send a link to confirm it.">
          <TextInput type="email" value={form.email} onChange={(e) => set("email")(e.target.value)} autoComplete="email" required />
        </Field>
        <Field label="Password" hint="At least 8 characters. Passwords found in known data breaches aren't allowed.">
          <TextInput
            type="password"
            value={form.password}
            onChange={(e) => set("password")(e.target.value)}
            minLength={8}
            maxLength={256}
            autoComplete="new-password"
            required
          />
        </Field>
        <Field label="School or university">
          <SchoolInput value={form.school} onChange={set("school")} />
        </Field>
        <div className="field-row">
          <Field label="Year of study (optional)">
            <YearSelect value={form.year} onChange={set("year")} />
          </Field>
          <Field label="Major (optional)">
            <TextInput value={form.major} onChange={(e) => set("major")(e.target.value)} maxLength={200} />
          </Field>
        </div>
        <Field label="Time zone" hint="Detected from your browser. Deadlines and free time use it.">
          <TimeZoneSelect value={form.time_zone} onChange={set("time_zone")} />
        </Field>
        <label className="check">
          <input type="checkbox" checked={form.accept_terms} onChange={(e) => set("accept_terms")(e.target.checked)} required />
          <span>
            I agree to the <a href="/terms" target="_blank">terms</a> and <a href="/privacy" target="_blank">privacy policy</a>.
          </span>
        </label>
        <FormError message={error} />
        <button className="primary-button" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>
      </form>
    </AuthLayout>
  );
}

// --- Email verification ----------------------------------------------------------------------------

// React may mount a page twice (always in development); send each link to the server once.
const verifications = new Map<string, Promise<User>>();
function verifyOnce(token: string): Promise<User> {
  let pending = verifications.get(token);
  if (!pending) {
    pending = api.verifyEmail(token);
    verifications.set(token, pending);
  }
  return pending;
}

export function VerifyEmailPage({ route, user, onUser }: { route: Route; user: User | null; onUser: SignedIn }) {
  const [error, setError] = useState<string | null>(null);
  const token = route.params.get("token") ?? "";

  useEffect(() => {
    let cancelled = false;
    verifyOnce(token).then(
      (verified) => {
        if (cancelled) return;
        if (user && user.id === verified.id) {
          onUser(verified);
          navigate("/connections?onboarding=1", { replace: true });
        } else {
          navigate("/login?notice=verified", { replace: true });
        }
      },
      (err) => !cancelled && setError(errorText(err)),
    );
    return () => {
      cancelled = true;
    };
    // Runs once per token; the callbacks it uses don't change what it does.
  }, [token]);

  return (
    <AuthLayout title={error ? "That link didn't work" : "Confirming your email…"}>
      <FormError message={error} />
      {error && (
        <Link href="/" className="primary-button">
          Back to Defog
        </Link>
      )}
    </AuthLayout>
  );
}

export function CheckEmailPage({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function resend() {
    setError(null);
    try {
      await api.resendVerification();
      setSent(true);
    } catch (err) {
      setError(errorText(err));
    }
  }
  return (
    <AuthLayout
      title="Check your email"
      footer={
        <button type="button" className="text-button" onClick={onLogout}>
          Log out
        </button>
      }
    >
      <p className="auth-text">
        We sent a confirmation link to <strong>{user.email}</strong>. Open it to start using Defog.
      </p>
      {sent && <p className="form-notice">A new link is on its way.</p>}
      <FormError message={error} />
      <button type="button" className="ghost-button" onClick={resend} disabled={sent}>
        Send the link again
      </button>
    </AuthLayout>
  );
}

// --- Password reset --------------------------------------------------------------------------------

export function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.forgotPassword(email);
      setDone(true);
    } catch (err) {
      setError(errorText(err));
    }
  }
  return (
    <AuthLayout title="Reset your password" footer={<Link href="/login">Back to log in</Link>}>
      {done ? (
        <p className="auth-text">If an account exists for {email}, we've emailed a link to reset the password. It works for 1 hour.</p>
      ) : (
        <form className="form" onSubmit={submit}>
          <Field label="Email">
            <TextInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
          </Field>
          <FormError message={error} />
          <button className="primary-button">Email me a reset link</button>
        </form>
      )}
    </AuthLayout>
  );
}

export function ResetPasswordPage({ route }: { route: Route }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.resetPassword(route.params.get("token") ?? "", password);
      setCsrfToken(null);
      // A reset signs out every device, so go to the login page.
      window.location.assign("/login?notice=reset");
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <AuthLayout title="Choose a new password">
      <form className="form" onSubmit={submit}>
        <Field label="New password" hint="At least 8 characters. This signs you out on every device.">
          <TextInput
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            maxLength={256}
            autoComplete="new-password"
            required
          />
        </Field>
        <FormError message={error} />
        <button className="primary-button" disabled={busy}>
          Save password
        </button>
      </form>
    </AuthLayout>
  );
}

// --- Google account linking -------------------------------------------------------------------------

export function LinkGooglePage({ route, onSignedIn }: { route: Route; onSignedIn: SignedIn }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const email = route.params.get("email") ?? "your account";
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const res = await api.linkGoogle(route.params.get("token") ?? "", password);
      setCsrfToken(res.csrf_token);
      navigate("/", { replace: true });
      onSignedIn(res.user);
    } catch (err) {
      setError(errorText(err));
    }
  }
  return (
    <AuthLayout title="Link your Google account" footer={<Link href="/login">Cancel</Link>}>
      <p className="auth-text">
        There's already a Defog account for <strong>{email}</strong>. Enter its password once to link Google sign-in. After
        that, you can log in either way.
      </p>
      <form className="form" onSubmit={submit}>
        <Field label="Defog password">
          <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </Field>
        <FormError message={error} />
        <button className="primary-button">Link and log in</button>
      </form>
    </AuthLayout>
  );
}

// --- Finish profile (after Google sign-up) ------------------------------------------------------------

export function CompleteProfilePage({ user, onUser }: { user: User; onUser: SignedIn }) {
  const [school, setSchool] = useState(user.school ?? "");
  const [year, setYear] = useState<string>(user.year ?? "");
  const [major, setMajor] = useState(user.major ?? "");
  const [timeZone, setTimeZone] = useState(user.time_zone ?? detectTimeZone());
  const [accept, setAccept] = useState(Boolean(user.consent_at));
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const updated = await api.updateMe({
        school,
        year: year || null,
        major: major.trim() || null,
        time_zone: timeZone,
        accept_terms: true,
      });
      onUser(updated);
      navigate("/connections?onboarding=1", { replace: true });
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <AuthLayout title={`Welcome, ${user.name.split(" ")[0]}`}>
      <p className="auth-text">A few details and you're in.</p>
      <form className="form" onSubmit={submit}>
        <Field label="School or university">
          <SchoolInput value={school} onChange={setSchool} />
        </Field>
        <div className="field-row">
          <Field label="Year of study (optional)">
            <YearSelect value={year} onChange={setYear} />
          </Field>
          <Field label="Major (optional)">
            <TextInput value={major} onChange={(e) => setMajor(e.target.value)} maxLength={200} />
          </Field>
        </div>
        <Field label="Time zone">
          <TimeZoneSelect value={timeZone} onChange={setTimeZone} />
        </Field>
        <label className="check">
          <input type="checkbox" checked={accept} onChange={(e) => setAccept(e.target.checked)} required />
          <span>
            I agree to the <a href="/terms" target="_blank">terms</a> and <a href="/privacy" target="_blank">privacy policy</a>.
          </span>
        </label>
        <FormError message={error} />
        <button className="primary-button">Continue</button>
      </form>
    </AuthLayout>
  );
}
