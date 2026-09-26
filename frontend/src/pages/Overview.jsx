import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { apiRequest, customerLabel, formatDateTime, relativeTime, supabase, supabaseConfigured } from "../lib/api.js";
import { useApi, useSession } from "../lib/session.jsx";
import {
  Badge,
  Card,
  Dot,
  EmptyState,
  ErrorNotice,
  Loading,
  PageHead,
} from "../components/ui.jsx";

/* ------------------------------------------------------------------ login */

const REDIRECT_URL = typeof window !== "undefined" ? window.location.origin : undefined;

function LinkButton({ children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: "none",
        border: 0,
        padding: 0,
        color: "var(--sd-accent)",
        fontSize: 12.5,
        fontWeight: 600,
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

function GoogleButton({ busy, onClick }) {
  return (
    <button
      type="button"
      className="sd-btn is-ghost"
      style={{ width: "100%", justifyContent: "center", marginBottom: 14 }}
      disabled={busy}
      onClick={onClick}
    >
      Continue with Google
    </button>
  );
}

function Divider() {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        margin: "4px 0 16px",
        color: "var(--sd-ink-faint)",
        fontSize: 11.5,
      }}
    >
      <span style={{ flex: 1, height: 1, background: "var(--sd-border)" }} />
      or
      <span style={{ flex: 1, height: 1, background: "var(--sd-border)" }} />
    </div>
  );
}

/* Sign in with email/password, plus a Google option. */
function SignInForm({ onSwitchToSignup, onSwitchToForgot }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { error: authError } = await supabase.auth.signInWithPassword({
      email,
      password,
    });
    // Deliberately generic: the message must not reveal whether the email
    // exists on the platform.
    if (authError) {
      console.error("Supabase login error:", authError);
      setError(authError.message);
    }
    setBusy(false);
  }

  async function withGoogle() {
    setBusy(true);
    setError(null);
    const { error: authError } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: REDIRECT_URL },
    });
    if (authError) {
      console.error("Supabase Google sign-in error:", authError);
      setError(authError.message);
      setBusy(false);
    }
    // On success the browser is redirected to Google, so there is nothing
    // further to do here.
  }

  return (
    <Card>
      <h2 style={{ marginBottom: 4 }}>Control Center</h2>
      <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
        Sign in to manage your AI receptionist.
      </p>

      <ErrorNotice message={error} />

      <GoogleButton busy={busy} onClick={withGoogle} />
      <Divider />

      <form onSubmit={submit}>
        <div className="sd-field">
          <label className="sd-label" htmlFor="email">Email</label>
          <input
            id="email"
            className="sd-input"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="sd-field">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <label className="sd-label" htmlFor="password" style={{ marginBottom: 0 }}>Password</label>
            <LinkButton onClick={onSwitchToForgot}>Forgot password?</LinkButton>
          </div>
          <input
            id="password"
            className="sd-input"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button
          className="sd-btn"
          style={{ width: "100%", justifyContent: "center" }}
          disabled={busy}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <p style={{ textAlign: "center", fontSize: 12.5, color: "var(--sd-ink-faint)", marginTop: 16 }}>
        New to SmartDesk?{" "}
        <LinkButton onClick={onSwitchToSignup}>Create an account</LinkButton>
      </p>
    </Card>
  );
}

/* Customer self-signup. Creates only a SmartDesk login -- never a tenant,
 * and never asks for business information here. Once the email is
 * confirmed, the customer sets up their own business themselves from the
 * dashboard (see NoBusinessPage / POST /signup/business); a platform admin
 * can alternatively create it for them via smartdesk/api/admin_api.py. */
function SignUpForm({ onSwitchToSignIn, onSignedUp }) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error: authError } = await supabase.auth.signUp({
      email,
      password,
      options: {
        data: { full_name: fullName },
        emailRedirectTo: REDIRECT_URL,
      },
    });
    if (authError) {
      console.error("Supabase signup error:", authError);
      setError(authError.message);
      setBusy(false);
      return;
    }
    // If email confirmation is required, Supabase returns a user with no
    // active session yet -- that's the "check your email" case. If a
    // session did come back (confirmation disabled project-side), the
    // backend still won't grant tenant access until Supabase reports the
    // email as confirmed, so it's safe to just let the session take over.
    if (!data.session) {
      onSignedUp();
    }
    setBusy(false);
  }

  async function withGoogle() {
    setBusy(true);
    setError(null);
    const { error: authError } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: REDIRECT_URL },
    });
    if (authError) {
      console.error("Supabase Google sign-in error:", authError);
      setError(authError.message);
      setBusy(false);
    }
  }

  return (
    <Card>
      <h2 style={{ marginBottom: 4 }}>Create your account</h2>
      <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
        Set up your SmartDesk login. Once you confirm your email, you can
        set up your own business and get straight to your dashboard --
        no waiting required.
      </p>

      <ErrorNotice message={error} />

      <GoogleButton busy={busy} onClick={withGoogle} />
      <Divider />

      <form onSubmit={submit}>
        <div className="sd-field">
          <label className="sd-label" htmlFor="full-name">Full name</label>
          <input
            id="full-name"
            className="sd-input"
            type="text"
            autoComplete="name"
            required
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
          />
        </div>
        <div className="sd-field">
          <label className="sd-label" htmlFor="signup-email">Email</label>
          <input
            id="signup-email"
            className="sd-input"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="sd-field">
          <label className="sd-label" htmlFor="signup-password">Password</label>
          <input
            id="signup-password"
            className="sd-input"
            type="password"
            autoComplete="new-password"
            minLength={8}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button
          className="sd-btn"
          style={{ width: "100%", justifyContent: "center" }}
          disabled={busy}
        >
          {busy ? "Creating account…" : "Create Account"}
        </button>
      </form>

      <p style={{ textAlign: "center", fontSize: 12.5, color: "var(--sd-ink-faint)", marginTop: 16 }}>
        Already have an account? <LinkButton onClick={onSwitchToSignIn}>Sign in</LinkButton>
      </p>
    </Card>
  );
}

function CheckEmailNotice({ onBackToSignIn }) {
  return (
    <Card>
      <h2 style={{ marginBottom: 4 }}>Check your email</h2>
      <p style={{ color: "var(--sd-ink-soft)", fontSize: 13, margin: "0 0 16px" }}>
        We've sent you a confirmation link. Please verify your email before
        signing in -- your SmartDesk account won't have access to a business
        until it's confirmed.
      </p>
      <button
        className="sd-btn is-ghost"
        style={{ width: "100%", justifyContent: "center" }}
        onClick={onBackToSignIn}
      >
        Back to sign in
      </button>
    </Card>
  );
}

function ForgotPasswordForm({ onBackToSignIn }) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const { error: authError } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: REDIRECT_URL,
    });
    // Generic on success and on failure alike: must not reveal whether the
    // email exists on the platform.
    if (authError) console.error("Supabase reset-password error:", authError);
    setSent(true);
    setBusy(false);
  }

  if (sent) {
    return (
      <Card>
        <h2 style={{ marginBottom: 4 }}>Check your email</h2>
        <p style={{ color: "var(--sd-ink-soft)", fontSize: 13, margin: "0 0 16px" }}>
          If an account exists for {email}, a password reset link is on its
          way.
        </p>
        <button
          className="sd-btn is-ghost"
          style={{ width: "100%", justifyContent: "center" }}
          onClick={onBackToSignIn}
        >
          Back to sign in
        </button>
      </Card>
    );
  }

  return (
    <Card>
      <h2 style={{ marginBottom: 4 }}>Reset your password</h2>
      <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
        Enter your account email and we'll send you a reset link.
      </p>

      <ErrorNotice message={error} />

      <form onSubmit={submit}>
        <div className="sd-field">
          <label className="sd-label" htmlFor="forgot-email">Email</label>
          <input
            id="forgot-email"
            className="sd-input"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <button
          className="sd-btn"
          style={{ width: "100%", justifyContent: "center" }}
          disabled={busy}
        >
          {busy ? "Sending…" : "Send reset link"}
        </button>
      </form>

      <p style={{ textAlign: "center", fontSize: 12.5, color: "var(--sd-ink-faint)", marginTop: 16 }}>
        <LinkButton onClick={onBackToSignIn}>Back to sign in</LinkButton>
      </p>
    </Card>
  );
}

export function LoginPage() {
  const [mode, setMode] = useState("signin"); // signin | signup | signup-sent | forgot

  if (!supabaseConfigured) {
    return (
      <div className="sd-login">
        <Card className="sd-login-card" title="Configuration required">
          <p style={{ color: "var(--sd-ink-soft)", fontSize: 13 }}>
            Set <code>VITE_SUPABASE_URL</code> and{" "}
            <code>VITE_SUPABASE_ANON_KEY</code> in <code>frontend/.env</code>,
            then restart the dev server.
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="sd-login">
      <div className="sd-login-card">
        <div className="sd-brand" style={{ justifyContent: "center", paddingBottom: 18 }}>
          <span className="sd-brand-mark">S</span>
          SmartDesk AI
        </div>

        {mode === "signin" && (
          <SignInForm
            onSwitchToSignup={() => setMode("signup")}
            onSwitchToForgot={() => setMode("forgot")}
          />
        )}
        {mode === "signup" && (
          <SignUpForm
            onSwitchToSignIn={() => setMode("signin")}
            onSignedUp={() => setMode("signup-sent")}
          />
        )}
        {mode === "signup-sent" && (
          <CheckEmailNotice onBackToSignIn={() => setMode("signin")} />
        )}
        {mode === "forgot" && (
          <ForgotPasswordForm onBackToSignIn={() => setMode("signin")} />
        )}
      </div>
    </div>
  );
}

/* --------------------------------------------------------- recovery flow */

/* Shown instead of the rest of the app while Supabase reports a password
 * recovery session (the user clicked the emailed reset link). The user
 * must set a new password before continuing -- they are never silently
 * left signed in with no way to do that. */
export function ResetPasswordPage() {
  const { clearPasswordRecovery } = useSession();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(event) {
    event.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    const { error: authError } = await supabase.auth.updateUser({ password });
    if (authError) {
      console.error("Supabase password update error:", authError);
      setError(authError.message);
      setBusy(false);
      return;
    }
    setDone(true);
    setBusy(false);
  }

  return (
    <div className="sd-login">
      <div className="sd-login-card">
        <div className="sd-brand" style={{ justifyContent: "center", paddingBottom: 18 }}>
          <span className="sd-brand-mark">S</span>
          SmartDesk AI
        </div>
        <Card>
          <h2 style={{ marginBottom: 4 }}>Set a new password</h2>
          {done ? (
            <>
              <p style={{ color: "var(--sd-ink-soft)", fontSize: 13, marginBottom: 16 }}>
                Your password has been updated. You're signed in with your new
                password.
              </p>
              <button
                className="sd-btn"
                style={{ width: "100%", justifyContent: "center" }}
                onClick={clearPasswordRecovery}
              >
                Continue
              </button>
            </>
          ) : (
            <>
              <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
                Choose a new password for your account.
              </p>
              <ErrorNotice message={error} />
              <form onSubmit={submit}>
                <div className="sd-field">
                  <label className="sd-label" htmlFor="new-password">New password</label>
                  <input
                    id="new-password"
                    className="sd-input"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </div>
                <div className="sd-field">
                  <label className="sd-label" htmlFor="confirm-password">Confirm password</label>
                  <input
                    id="confirm-password"
                    className="sd-input"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    required
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                  />
                </div>
                <button
                  className="sd-btn"
                  style={{ width: "100%", justifyContent: "center" }}
                  disabled={busy}
                >
                  {busy ? "Saving…" : "Set new password"}
                </button>
              </form>
            </>
          )}
        </Card>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------- verification /
   no-business states */

/* Authenticated, but Supabase has not confirmed the email yet. Distinct
 * from NoBusinessPage: nothing has gone wrong here, the account just is not
 * usable until verification completes. */
export function VerifyEmailPage() {
  const { signOut, session } = useSession();
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function resend() {
    setBusy(true);
    const email = session?.user?.email;
    if (email) {
      await supabase.auth.resend({ type: "signup", email });
    }
    setSent(true);
    setBusy(false);
  }

  return (
    <div className="sd-login">
      <div className="sd-login-card sd-card">
        <h2>Verify your email</h2>
        <p style={{ color: "var(--sd-ink-soft)", fontSize: 13 }}>
          Your SmartDesk account is active, but your email address hasn't
          been confirmed yet. Please check your inbox for the confirmation
          link before continuing.
        </p>
        <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
          <button className="sd-btn is-ghost" disabled={busy} onClick={resend}>
            {sent ? "Email sent" : busy ? "Sending…" : "Resend email"}
          </button>
          <button className="sd-btn is-ghost" onClick={signOut}>
            Sign Out
          </button>
        </div>
      </div>
    </div>
  );
}

/* Authenticated, verified, but not linked to any tenant yet. This is the
 * self-service "Create Your Business" step -- the ONLY way a customer
 * tenant gets created. It calls POST /api/v1/signup/business directly
 * (there is no tenant yet, so useSession().call is not used -- that helper
 * always attaches whatever tenant is currently selected, which is exactly
 * what must NOT happen here). On success it reloads /me and lands the
 * owner straight on their new dashboard -- never through any admin flow. */
export function NoBusinessPage() {
  const { signOut, reload } = useSession();
  const navigate = useNavigate();
  const [form, setForm] = useState({
    business_name: "",
    business_email: "",
    phone: "",
    location: "",
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const set = (key) => (e) =>
    setForm((prev) => ({ ...prev, [key]: e.target.value }));

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await apiRequest("/signup/business", { method: "POST", body: form });
      // Refresh /me so the new membership shows up, then go straight to
      // the tenant dashboard -- no admin step, no extra confirmation page.
      await reload();
      navigate("/", { replace: true });
    } catch (err) {
      if (err.status === 409) {
        setError(
          "This account is already linked to a business. Try refreshing the page."
        );
      } else if (err.status === 401) {
        setError("Your session has expired. Please sign in again.");
      } else {
        // 400 (validation) and network/server errors: the backend's own
        // message is specific and safe to show as-is (e.g. which field is
        // missing); anything else falls back to a generic message.
        setError(err.message || "Something went wrong. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sd-login">
      <div className="sd-login-card">
        <div className="sd-brand" style={{ justifyContent: "center", paddingBottom: 18 }}>
          <span className="sd-brand-mark">S</span>
          SmartDesk AI
        </div>
        <Card>
          <h2 style={{ marginBottom: 4 }}>Create your business</h2>
          <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
            Your SmartDesk account is ready. Tell us about your business and
            your dashboard is set up immediately -- no waiting on approval.
          </p>

          <ErrorNotice message={error} />

          <form onSubmit={submit}>
            <div className="sd-field">
              <label className="sd-label" htmlFor="business-name">Business name</label>
              <input
                id="business-name"
                className="sd-input"
                type="text"
                required
                value={form.business_name}
                onChange={set("business_name")}
                disabled={busy}
              />
            </div>
            <div className="sd-field">
              <label className="sd-label" htmlFor="business-email">Business email</label>
              <input
                id="business-email"
                className="sd-input"
                type="email"
                required
                value={form.business_email}
                onChange={set("business_email")}
                disabled={busy}
              />
            </div>
            <div className="sd-field">
              <label className="sd-label" htmlFor="business-phone">Business phone</label>
              <input
                id="business-phone"
                className="sd-input"
                type="tel"
                required
                value={form.phone}
                onChange={set("phone")}
                disabled={busy}
              />
            </div>
            <div className="sd-field">
              <label className="sd-label" htmlFor="business-location">Business location</label>
              <input
                id="business-location"
                className="sd-input"
                type="text"
                required
                placeholder="e.g. Plot 123, Francistown, Botswana"
                value={form.location}
                onChange={set("location")}
                disabled={busy}
              />
            </div>
            <button
              className="sd-btn"
              style={{ width: "100%", justifyContent: "center" }}
              disabled={busy}
            >
              {busy ? "Creating your business…" : "Create my business"}
            </button>
          </form>
        </Card>

        <p style={{ textAlign: "center", fontSize: 12.5, color: "var(--sd-ink-faint)", marginTop: 16 }}>
          <LinkButton onClick={signOut}>Sign out</LinkButton>
        </p>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- overview */

function greeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function Metric({ label, value }) {
  const empty = value === null || value === undefined;
  return (
    <Card>
      <div className="sd-metric-label">{label}</div>
      <div className={`sd-metric-value ${empty ? "is-empty" : ""}`}>
        {empty ? "No data yet" : value}
      </div>
    </Card>
  );
}

export function OverviewPage() {
  const { user, activeTenant } = useSession();
  const { data, loading, error } = useApi("/overview");

  if (loading) return <Loading />;
  if (error) return <ErrorNotice message={error} />;
  if (!data) return null;

  const { metrics, receptionist, channels } = data;
  const firstName = (user?.full_name || user?.email || "").split(/[@ ]/)[0];

  return (
    <>
      <PageHead
        title={`${greeting()}${firstName ? `, ${firstName}` : ""}`}
        subtitle="Here's what's happening with your AI receptionist."
      />

      {activeTenant?.is_test_data && (
        <p className="sd-pending" style={{ marginBottom: 16 }}>
          This is a development tenant. All records here are marked as test data
          and are excluded from production reporting.
        </p>
      )}

      <div className="sd-grid sd-grid-metrics" style={{ marginBottom: 16 }}>
        <Metric label="Conversations today" value={metrics.conversations_today} />
        <Metric label="Conversations this month" value={metrics.conversations_month} />
        <Metric label="Leads captured" value={metrics.leads_captured} />
        <Metric label="Bookings" value={metrics.bookings} />
        <Metric label="Missed / handed off" value={metrics.handed_off} />
        <Metric
          label="AI response rate"
          value={
            metrics.ai_response_rate === null
              ? null
              : `${metrics.ai_response_rate}%`
          }
        />
      </div>

      <div className="sd-grid sd-grid-2" style={{ marginBottom: 16 }}>
        <Card
          title="Recent Conversations"
          action={<Link to="/conversations" className="sd-badge is-accent">View all</Link>}
        >
          {data.recent_conversations.length === 0 ? (
            <EmptyState
              title="No conversations yet"
              description="They will appear here as customers message your receptionist."
            />
          ) : (
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Channel</th>
                  <th>Latest message</th>
                  <th>Time</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_conversations.map((row) => (
                  <tr key={row.id}>
                    <td>{customerLabel(row.customer)}</td>
                    <td style={{ textTransform: "capitalize" }}>{row.channel_kind}</td>
                    <td
                      style={{
                        maxWidth: 220,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                        color: "var(--sd-ink-soft)",
                      }}
                    >
                      {row.last_message_preview || "—"}
                    </td>
                    <td>{relativeTime(row.last_message_at)}</td>
                    <td>
                      <Badge tone={row.status === "needs_human" ? "warn" : ""}>
                        {row.status === "needs_human" ? "Needs human" : row.status}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="AI Receptionist Status">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 14,
            }}
          >
            <strong style={{ fontSize: 16 }}>{receptionist.name}</strong>
            <Badge tone={receptionist.online ? "ok" : ""}>
              <Dot state={receptionist.online ? "ok" : "off"} />
              {receptionist.online ? "Online" : "Paused"}
            </Badge>
          </div>
          <div className="sd-status-row">
            <span>WhatsApp</span>
            <span>
              <Dot state={channels.whatsapp ? "ok" : "off"} />
              {channels.whatsapp ? "Connected" : "Not configured"}
            </span>
          </div>
          <div className="sd-status-row">
            <span>Voice</span>
            <span>
              <Dot state={channels.voice ? "ok" : "off"} />
              {channels.voice ? "Connected" : "Not configured"}
            </span>
          </div>
          <div className="sd-status-row">
            <span>AI Engine</span>
            <span>
              <Dot state={data.ai_engine_operational ? "ok" : "off"} />
              {data.ai_engine_operational ? "Operational" : "Unavailable"}
            </span>
          </div>
        </Card>
      </div>

      <div className="sd-grid sd-grid-2">
        <Card title="Recent Leads">
          {data.recent_leads.length === 0 ? (
            <EmptyState title="No leads captured yet" />
          ) : (
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Source</th>
                  <th>Status</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_leads.map((lead) => (
                  <tr key={lead.id}>
                    <td>{customerLabel(lead.customer)}</td>
                    <td style={{ textTransform: "capitalize" }}>{lead.source}</td>
                    <td>
                      <Badge tone={lead.status === "converted" ? "ok" : "accent"}>
                        {lead.status}
                      </Badge>
                    </td>
                    <td>{relativeTime(lead.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Upcoming Bookings">
          {data.upcoming_bookings.length === 0 ? (
            <EmptyState title="No upcoming bookings" />
          ) : (
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Service</th>
                  <th>Date / time</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.upcoming_bookings.map((booking) => (
                  <tr key={booking.id}>
                    <td>{customerLabel(booking.customer)}</td>
                    <td>{booking.service || "—"}</td>
                    <td>{formatDateTime(booking.starts_at)}</td>
                    <td>
                      <Badge tone={booking.status === "confirmed" ? "ok" : ""}>
                        {booking.status}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  );
}
