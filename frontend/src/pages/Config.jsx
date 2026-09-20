import { useEffect, useState } from "react";
import { formatDateTime } from "../lib/api.js";
import { useApi, useSession } from "../lib/session.jsx";
import {
  Badge,
  Card,
  Dot,
  EmptyState,
  ErrorNotice,
  Loading,
  PageHead,
  PendingNotice,
  SwitchRow,
} from "../components/ui.jsx";

/* ------------------------------------------------------ knowledge base */

export function KnowledgeBasePage() {
  const { call, can } = useSession();
  const { data, loading, error, refresh } = useApi("/knowledge");
  const [active, setActive] = useState(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  useEffect(() => {
    if (data?.sections?.length && !active) {
      setActive(data.sections[0].section);
      setDraft(data.sections[0].body || "");
    }
  }, [data, active]);

  const current = data?.sections.find((s) => s.section === active);

  function select(section) {
    setActive(section.section);
    setDraft(section.body || "");
    setSaveError(null);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      await call(`/knowledge/${active}`, {
        method: "PUT",
        body: { body: draft, is_published: Boolean(draft.trim()) },
      });
      refresh();
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <Loading />;

  return (
    <>
      <PageHead
        title="Knowledge Base"
        subtitle="The verified facts your receptionist is allowed to state. Anything not written here, it will say it does not know."
      />
      <ErrorNotice message={error} />

      <div className="sd-grid" style={{ gridTemplateColumns: "240px 1fr" }}>
        <Card>
          {data?.sections.map((section) => (
            <div
              key={section.section}
              className={`sd-nav-item ${section.section === active ? "is-active" : ""}`}
              style={{ cursor: "pointer", justifyContent: "space-between" }}
              onClick={() => select(section)}
            >
              <span>{section.title}</span>
              <Dot state={section.is_published ? "ok" : "off"} />
            </div>
          ))}
        </Card>

        <Card
          title={current?.title}
          action={
            <button className="sd-btn" onClick={save} disabled={saving || !can("manager")}>
              {saving ? "Saving…" : "Save section"}
            </button>
          }
        >
          <ErrorNotice message={saveError} />
          {!can("manager") && (
            <PendingNotice>
              Your role can read the knowledge base but not edit it.
            </PendingNotice>
          )}
          <textarea
            className="sd-textarea"
            style={{ minHeight: 280 }}
            value={draft}
            disabled={!can("manager")}
            placeholder="Leave blank if this is not yet known. An empty section stays unpublished and is never shown to the AI."
            onChange={(e) => setDraft(e.target.value)}
          />
          <p className="sd-hint">
            {current?.is_published
              ? `Published · last updated ${formatDateTime(current.updated_at)}`
              : "Unpublished — this section is not given to the receptionist."}
          </p>
        </Card>
      </div>
    </>
  );
}

/* ---------------------------------------------------- AI receptionist */

export function ReceptionistPage() {
  const { call, can, user } = useSession();
  const { data, loading, error, refresh } = useApi("/receptionist");
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  useEffect(() => { if (data) setForm(data); }, [data]);

  if (loading || !form) return <Loading />;

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  async function save() {
    setSaving(true);
    setSaveError(null);
    const { id, sales_mode_enabled, ...payload } = form;
    try {
      await call("/receptionist", { method: "PATCH", body: payload });
      refresh();
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHead
        title="AI Receptionist"
        subtitle="O'Brien's intelligence is shared across the platform; this configuration belongs to this business alone."
        action={
          <button className="sd-btn" onClick={save} disabled={saving || !can("manager")}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        }
      />
      <ErrorNotice message={error || saveError} />

      <div className="sd-grid sd-grid-2">
        <Card
          title={form.display_name || "O'Brien"}
          action={
            <Badge tone={form.is_active ? "ok" : ""}>
              <Dot state={form.is_active ? "ok" : "off"} />
              {form.is_active ? "Active" : "Paused"}
            </Badge>
          }
        >
          <div className="sd-field">
            <label className="sd-label">Assistant name</label>
            <input className="sd-input" value={form.display_name || ""}
              disabled={!can("manager")}
              onChange={(e) => set("display_name", e.target.value)} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Greeting (WhatsApp)</label>
            <textarea className="sd-textarea" value={form.greeting || ""}
              disabled={!can("manager")}
              onChange={(e) => set("greeting", e.target.value)} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Greeting (Voice)</label>
            <textarea className="sd-textarea" value={form.voice_greeting || ""}
              disabled={!can("manager")}
              onChange={(e) => set("voice_greeting", e.target.value)} />
            <p className="sd-hint">Spoken aloud when a call is answered. Keep it short.</p>
          </div>
        </Card>

        <Card title="Behaviour">
          <div className="sd-field">
            <label className="sd-label">Personality</label>
            <textarea className="sd-textarea" value={form.personality || ""}
              disabled={!can("manager")}
              placeholder="e.g. Warm, concise, and professional. Use first names."
              onChange={(e) => set("personality", e.target.value)} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Business instructions</label>
            <textarea className="sd-textarea" value={form.business_instructions || ""}
              disabled={!can("manager")}
              placeholder="e.g. Always mention that courts must be booked at least two hours ahead."
              onChange={(e) => set("business_instructions", e.target.value)} />
          </div>

          <SwitchRow label="Human handoff"
            description="Connect the customer to a person when they ask."
            checked={form.handoff_enabled} disabled={!can("manager")}
            onChange={(v) => set("handoff_enabled", v)} />
          <SwitchRow label="Lead capture"
            description="Record enquiries worth following up."
            checked={form.lead_capture_enabled} disabled={!can("manager")}
            onChange={(v) => set("lead_capture_enabled", v)} />
          <SwitchRow label="Booking assistance"
            description="Help customers arrange an appointment."
            checked={form.booking_assistance_enabled} disabled={!can("manager")}
            onChange={(v) => set("booking_assistance_enabled", v)} />

          <div className="sd-field" style={{ marginTop: 14 }}>
            <label className="sd-label">Booking mode</label>
            <select className="sd-select" value={form.booking_mode || "calendar"}
              disabled={!can("manager")}
              onChange={(e) => set("booking_mode", e.target.value)}>
              <option value="calendar">SmartDesk Calendar</option>
              <option value="external">External Booking System</option>
            </select>
          </div>

          {form.booking_mode === "external" ? (
            <div className="sd-field">
              <label className="sd-label">Booking URL</label>
              <input className="sd-input" value={form.external_booking_url || ""}
                disabled={!can("manager")} placeholder="https://…"
                onChange={(e) => set("external_booking_url", e.target.value)} />
              <p className="sd-hint">
                O'Brien sends customers this link instead of booking directly.
              </p>
            </div>
          ) : (
            <div className="sd-field">
              <label className="sd-label">Default booking duration (minutes)</label>
              <input className="sd-input" type="number" min="15" step="15"
                value={form.default_booking_duration_minutes || 60}
                disabled={!can("manager")}
                onChange={(e) => set("default_booking_duration_minutes", Number(e.target.value))} />
              <p className="sd-hint">
                Used when a customer doesn't specify a duration. Timezone and
                calendar connection are configured on the Business and
                Bookings pages.
              </p>
            </div>
          )}

          {user?.is_platform_admin && (
            <div style={{ marginTop: 14 }}>
              <PendingNotice>
                Sales mode is {form.sales_mode_enabled ? "ON" : "OFF"} for this
                tenant. It should only ever be on for SmartDesk AI's own tenant;
                enabling it elsewhere makes that business pitch SmartDesk to its
                own customers.
              </PendingNotice>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

/* ------------------------------------------------------------ channels */

function ChannelPage({ kind, title, subtitle }) {
  const { data, loading, error } = useApi(`/channels/${kind}`, [kind]);
  const overview = useApi("/overview");

  if (loading) return <Loading />;
  const channels = data?.items ?? [];
  const metrics = overview.data?.metrics;

  return (
    <>
      <PageHead title={title} subtitle={subtitle} />
      <ErrorNotice message={error} />

      {channels.length === 0 ? (
        <Card>
          <EmptyState
            title={`No ${title} number connected`}
            description="A SmartDesk administrator registers the number that routes inbound traffic to this business."
          />
        </Card>
      ) : (
        channels.map((channel) => (
          <Card
            key={channel.id}
            className=""
            title={title}
            action={
              <Badge tone={channel.is_active ? "ok" : ""}>
                <Dot state={channel.is_active ? "ok" : "off"} />
                {channel.is_active ? "Connected" : "Inactive"}
              </Badge>
            }
          >
            <div className="sd-status-row"><span>Number</span><span>{channel.address}</span></div>
            <div className="sd-status-row"><span>Status</span><span>{channel.is_active ? "Active" : "Inactive"}</span></div>
            <div className="sd-status-row">
              <span>{kind === "voice" ? "Calls this month" : "Conversations"}</span>
              <span>{channel.conversations}</span>
            </div>
            {kind === "voice" && (
              <div className="sd-status-row">
                <span>Calls today</span>
                <span>{metrics ? metrics.conversations_today : "—"}</span>
              </div>
            )}
            <div className="sd-status-row">
              <span>Last inbound</span><span>{formatDateTime(channel.last_inbound_at)}</span>
            </div>
            <div className="sd-status-row">
              <span>Provider credentials</span>
              <span>
                <Dot state={channel.provider_configured ? "ok" : "warn"} />
                {channel.provider_configured ? "Configured on server" : "Not configured"}
              </span>
            </div>
            <div className="sd-status-row">
              <span>Webhook signature validation</span>
              <span>
                <Dot state={channel.signature_validation ? "ok" : "warn"} />
                {channel.signature_validation ? "Enforced" : "Disabled"}
              </span>
            </div>
            <div className="sd-status-row">
              <span>Human handoff</span><span>Enabled</span>
            </div>
            <p className="sd-hint">
              Credentials are held on the server and are never sent to this
              dashboard — only whether they are present.
            </p>
          </Card>
        ))
      )}
    </>
  );
}

export const WhatsAppPage = () => (
  <ChannelPage kind="whatsapp" title="WhatsApp"
    subtitle="The WhatsApp number customers of this business message." />
);

export const VoicePage = () => (
  <ChannelPage kind="voice" title="Voice"
    subtitle="The phone number your AI receptionist answers." />
);

/* --------------------------------------------------------- automations */

export function AutomationsPage() {
  const { data, loading, error } = useApi("/automations");
  if (loading) return <Loading />;

  return (
    <>
      <PageHead title="Automations" subtitle="Scheduled and triggered messages." />
      <ErrorNotice message={error} />
      <PendingNotice>
        These automations are modelled and stored, but message delivery is not
        wired up yet. They are shown as Pending and cannot be switched on until
        the delivery layer lands.
      </PendingNotice>
      <Card>
        <table className="sd-table">
          <thead><tr><th>Automation</th><th>Status</th><th>Last run</th></tr></thead>
          <tbody>
            {data?.items.map((item) => (
              <tr key={item.kind}>
                <td>{item.name}</td>
                <td>
                  <Badge tone={item.status === "enabled" ? "ok" : ""}>
                    {item.is_implemented ? item.status : "Pending"}
                  </Badge>
                </td>
                <td>{formatDateTime(item.last_run_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}

/* ----------------------------------------------------------- analytics */

function Bars({ series, label }) {
  const values = series?.map((point) => point.value) ?? [];
  const max = Math.max(1, ...values);
  if (values.length === 0) {
    return <EmptyState title={`No ${label.toLowerCase()} in this period`} />;
  }
  return (
    <>
      <div className="sd-bar">
        {series.map((point) => (
          <div
            key={point.date}
            className="sd-bar-col"
            style={{ height: `${(point.value / max) * 100}%` }}
            title={`${point.date}: ${point.value}`}
          />
        ))}
      </div>
      <p className="sd-hint">
        {values.reduce((a, b) => a + b, 0)} total · peak {max}
      </p>
    </>
  );
}

export function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const { data, loading, error } = useApi(`/analytics?days=${days}`, [days]);

  if (loading) return <Loading />;

  return (
    <>
      <PageHead title="Analytics" subtitle="Computed from this tenant's own records only." />
      <div className="sd-toolbar">
        {[7, 30, 90].map((value) => (
          <button key={value} className={`sd-chip ${days === value ? "is-active" : ""}`} onClick={() => setDays(value)}>
            {value} days
          </button>
        ))}
      </div>
      <ErrorNotice message={error} />

      {!data?.has_data && (
        <PendingNotice>
          There is not enough activity yet to report on. Figures appear here as
          real conversations accumulate — nothing on this page is simulated.
        </PendingNotice>
      )}

      <div className="sd-grid sd-grid-2">
        <Card title="Conversations"><Bars series={data?.conversations} label="Conversations" /></Card>
        <Card title="Calls"><Bars series={data?.calls} label="Calls" /></Card>
        <Card title="Leads"><Bars series={data?.leads} label="Leads" /></Card>
        <Card title="Bookings"><Bars series={data?.bookings} label="Bookings" /></Card>
        <Card title="Message volume"><Bars series={data?.messages} label="Messages" /></Card>
        <Card title="AI vs human">
          <div className="sd-status-row"><span>Handled by O'Brien</span><span>{data?.ai_vs_human.ai ?? 0}</span></div>
          <div className="sd-status-row"><span>Handled by staff</span><span>{data?.ai_vs_human.human ?? 0}</span></div>
        </Card>
      </div>
    </>
  );
}

/* --------------------------------------------- business, settings, account */

export function BusinessPage() {
  const { call, can } = useSession();
  const { data, loading, error, refresh } = useApi("/business");
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  useEffect(() => { if (data) setForm(data.tenant); }, [data]);
  if (loading || !form) return <Loading />;

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));

  async function save() {
    setSaving(true); setSaveError(null);
    try {
      await call("/business", {
        method: "PATCH",
        body: {
          name: form.name,
          timezone: form.timezone,
          escalation_whatsapp: form.escalation_whatsapp,
          escalation_email: form.escalation_email,
        },
      });
      refresh();
    } catch (err) { setSaveError(err.message); }
    finally { setSaving(false); }
  }

  return (
    <>
      <PageHead title="Business" subtitle="Details and team for this business."
        action={<button className="sd-btn" onClick={save} disabled={saving || !can("owner")}>
          {saving ? "Saving…" : "Save changes"}</button>} />
      <ErrorNotice message={error || saveError} />

      <div className="sd-grid sd-grid-2">
        <Card title="Details">
          <div className="sd-field">
            <label className="sd-label">Business name</label>
            <input className="sd-input" value={form.name} disabled={!can("owner")}
              onChange={(e) => set("name", e.target.value)} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Timezone</label>
            <input className="sd-input" value={form.timezone} disabled={!can("owner")}
              onChange={(e) => set("timezone", e.target.value)} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Escalation WhatsApp number</label>
            <input className="sd-input" value={form.escalation_whatsapp || ""}
              disabled={!can("owner")} placeholder="+267…"
              onChange={(e) => set("escalation_whatsapp", e.target.value)} />
            <p className="sd-hint">
              Where handoff alerts for this business are sent. Alerts never go to
              another business's staff.
            </p>
          </div>
          <div className="sd-field">
            <label className="sd-label">Escalation email</label>
            <input className="sd-input" value={form.escalation_email || ""}
              disabled={!can("owner")}
              onChange={(e) => set("escalation_email", e.target.value)} />
          </div>
        </Card>

        <Card title="Team">
          {data.members.length === 0 ? (
            <EmptyState title="No members yet"
              description="Users are granted access with the flask grant command." />
          ) : (
            <table className="sd-table">
              <thead><tr><th>Email</th><th>Role</th></tr></thead>
              <tbody>
                {data.members.map((member) => (
                  <tr key={member.membership_id}>
                    <td>{member.email}</td>
                    <td><Badge tone="accent">{member.role}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="sd-hint">
            Inviting team members from the dashboard arrives in a later phase.
          </p>
        </Card>
      </div>
    </>
  );
}

export function SettingsPage() {
  const { data, loading } = useApi("/business");
  const [calendarNotice, setCalendarNotice] = useState(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const status = params.get("calendar");
    if (!status) return;
    const messages = {
      connected: { tone: "ok", text: "Google Calendar connected successfully." },
      denied: { tone: "warn", text: "Google Calendar connection was cancelled." },
      expired: { tone: "warn", text: "That connection link expired — please try again from the Bookings page." },
      error: { tone: "alert", text: "Could not connect Google Calendar. Please try again." },
    };
    setCalendarNotice(messages[status] || null);
    window.history.replaceState({}, "", window.location.pathname);
  }, []);

  if (loading) return <Loading />;
  const tenant = data?.tenant;

  return (
    <>
      <PageHead title="Settings" subtitle="Platform configuration for this business." />
      {calendarNotice && (
        <div className={`sd-badge is-${calendarNotice.tone}`} style={{ marginBottom: 16, display: "inline-flex" }}>
          {calendarNotice.text}
        </div>
      )}
      <div className="sd-grid sd-grid-2">
        <Card title="Tenant">
          <div className="sd-status-row"><span>Tenant ID</span>
            <span style={{ fontSize: 11.5, color: "var(--sd-ink-faint)" }}>{tenant?.id}</span></div>
          <div className="sd-status-row"><span>Slug</span><span>{tenant?.slug}</span></div>
          <div className="sd-status-row"><span>Type</span><span style={{ textTransform: "capitalize" }}>{tenant?.business_type}</span></div>
          <div className="sd-status-row"><span>Status</span>
            <Badge tone={tenant?.status === "active" ? "ok" : "warn"}>{tenant?.status}</Badge></div>
          <div className="sd-status-row"><span>Data classification</span>
            <Badge tone={tenant?.is_test_data ? "warn" : "ok"}>
              {tenant?.is_test_data ? "Test data" : "Production"}</Badge></div>
        </Card>
        <Card title="Limits">
          <div className="sd-status-row"><span>Monthly conversation limit</span>
            <span>{tenant?.monthly_conversation_limit}</span></div>
          <p className="sd-hint">
            Limits are counted per business, so one tenant's volume never
            affects another's.
          </p>
        </Card>
      </div>
    </>
  );
}

export function AccountPage() {
  const { user, tenants, signOut } = useSession();
  return (
    <>
      <PageHead title="Account" subtitle="Your SmartDesk AI sign-in."
        action={<button className="sd-btn is-ghost" onClick={signOut}>Sign out</button>} />
      <div className="sd-grid sd-grid-2">
        <Card title="Profile">
          <div className="sd-status-row"><span>Email</span><span>{user?.email}</span></div>
          <div className="sd-status-row"><span>Name</span><span>{user?.full_name || "—"}</span></div>
          <div className="sd-status-row"><span>Platform administrator</span>
            <Badge tone={user?.is_platform_admin ? "accent" : ""}>
              {user?.is_platform_admin ? "Yes" : "No"}</Badge></div>
          <p className="sd-hint">
            Passwords are managed by Supabase. SmartDesk never stores or sees them.
          </p>
        </Card>
        <Card title="Your access">
          <table className="sd-table">
            <thead><tr><th>Business</th><th>Role</th></tr></thead>
            <tbody>
              {tenants.map((tenant) => (
                <tr key={tenant.id}>
                  <td>{tenant.name}</td>
                  <td><Badge tone="accent">{tenant.role}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>
    </>
  );
}
