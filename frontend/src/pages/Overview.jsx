import { useState } from "react";
import { Link } from "react-router-dom";
import { customerLabel, formatDateTime, relativeTime, supabase, supabaseConfigured } from "../lib/api.js";
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

export function LoginPage() {
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
    if (authError) setError("Those sign-in details were not accepted.");
    setBusy(false);
  }

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
        <div
          className="sd-brand"
          style={{ justifyContent: "center", paddingBottom: 18 }}
        >
          <span className="sd-brand-mark">S</span>
          SmartDesk AI
        </div>
        <Card>
          <h2 style={{ marginBottom: 4 }}>Control Center</h2>
          <p
            style={{
              color: "var(--sd-ink-faint)",
              fontSize: 12.5,
              margin: "0 0 18px",
            }}
          >
            Sign in to manage your AI receptionist.
          </p>

          <ErrorNotice message={error} />

          <form onSubmit={submit}>
            <div className="sd-field">
              <label className="sd-label" htmlFor="email">
                Email
              </label>
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
              <label className="sd-label" htmlFor="password">
                Password
              </label>
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
        </Card>
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
