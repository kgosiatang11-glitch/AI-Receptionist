import { useEffect, useState } from "react";
import { customerLabel, formatDate, formatDateTime, relativeTime } from "../lib/api.js";
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
} from "../components/ui.jsx";

/* --------------------------------------------------------------- inbox */

const FILTERS = [
  ["all", "All"],
  ["active", "Active"],
  ["unread", "Unread"],
  ["leads", "Leads"],
  ["booking", "Booking"],
  ["needs_human", "Needs Human"],
  ["closed", "Closed"],
];

const BUBBLE_CLASS = {
  customer: "is-customer",
  assistant: "is-assistant",
  human_agent: "is-human",
};

const ROLE_LABEL = {
  customer: "Customer",
  assistant: "O'Brien",
  human_agent: "Staff",
};

export function ConversationsPage() {
  const { call, can, activeTenantId } = useSession();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const [thread, setThread] = useState(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [draftReply, setDraftReply] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(null);

  // Debounce so typing does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const listPath = `/conversations?filter=${filter}${
    query ? `&q=${encodeURIComponent(query)}` : ""
  }`;
  const { data, loading, error, refresh } = useApi(listPath, [filter, query]);

  // Changing tenant must clear the open thread, never carry it across.
  useEffect(() => {
    setSelectedId(null);
    setThread(null);
  }, [activeTenantId]);

  useEffect(() => {
    if (!selectedId) return;
    setThreadLoading(true);
    setDraftReply("");
    setSendError(null);
    call(`/conversations/${selectedId}`)
      .then(setThread)
      .catch(() => setThread(null))
      .finally(() => setThreadLoading(false));
  }, [selectedId, call]);

  async function toggleTakeover() {
    if (!thread) return;
    const next = !thread.conversation.human_takeover;
    await call(`/conversations/${thread.conversation.id}/takeover`, {
      method: "POST",
      body: { enabled: next },
    });
    const updated = await call(`/conversations/${thread.conversation.id}`);
    setThread(updated);
    refresh();
  }

  async function sendReply(event) {
    event.preventDefault();
    if (!draftReply.trim() || !thread) return;
    setSending(true);
    setSendError(null);
    try {
      await call(`/conversations/${thread.conversation.id}/reply`, {
        method: "POST",
        body: { body: draftReply.trim() },
      });
      setDraftReply("");
      const updated = await call(`/conversations/${thread.conversation.id}`);
      setThread(updated);
      refresh();
    } catch (err) {
      setSendError(err.message);
    } finally {
      setSending(false);
    }
  }

  const items = data?.items ?? [];

  return (
    <>
      <PageHead title="Conversations" subtitle="Every customer conversation across WhatsApp and Voice." />

      <div className="sd-toolbar">
        {FILTERS.map(([key, label]) => (
          <button
            key={key}
            className={`sd-chip ${filter === key ? "is-active" : ""}`}
            onClick={() => setFilter(key)}
          >
            {label}
          </button>
        ))}
        <input
          className="sd-input sd-search"
          placeholder="Search conversations…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <ErrorNotice message={error} />

      <div className="sd-inbox">
        <div className="sd-inbox-list">
          <div className="sd-inbox-scroll">
            {loading && <Loading />}
            {!loading && items.length === 0 && (
              <EmptyState title="No conversations" description="Nothing matches this filter." />
            )}
            {items.map((row) => (
              <div
                key={row.id}
                className={`sd-inbox-item ${row.id === selectedId ? "is-active" : ""}`}
                onClick={() => setSelectedId(row.id)}
              >
                <div className="sd-inbox-item-top">
                  <span className="sd-inbox-name">
                    {customerLabel(row.customer)}
                    {row.is_unread && <span className="sd-inbox-unread" />}
                  </span>
                  <span className="sd-inbox-time">{relativeTime(row.last_message_at)}</span>
                </div>
                <div className="sd-inbox-preview">{row.last_message_preview || "No messages yet"}</div>
                <div style={{ marginTop: 6, display: "flex", gap: 5 }}>
                  <Badge>{row.channel_kind}</Badge>
                  {row.status === "needs_human" && <Badge tone="warn">Needs human</Badge>}
                  {row.human_takeover && <Badge tone="accent">Human</Badge>}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="sd-inbox-thread">
          {!selectedId && (
            <EmptyState title="Select a conversation" description="Choose one from the list to read the full thread." />
          )}
          {selectedId && threadLoading && <Loading />}
          {thread && !threadLoading && (
            <>
              <div className="sd-thread-head">
                <div>
                  <strong>{customerLabel(thread.conversation.customer)}</strong>
                  <div style={{ fontSize: 12, color: "var(--sd-ink-faint)" }}>
                    {thread.conversation.channel_kind} · {thread.conversation.session_key}
                  </div>
                </div>
                <button
                  className={`sd-btn ${thread.conversation.human_takeover ? "" : "is-ghost"}`}
                  onClick={toggleTakeover}
                  disabled={!can("agent")}
                  title={can("agent") ? "" : "Your role cannot take over conversations"}
                >
                  {thread.conversation.human_takeover ? "Release to O'Brien" : "Take over"}
                </button>
              </div>

              <div className="sd-thread-body">
                {thread.messages.length === 0 && (
                  <EmptyState title="No messages in this conversation yet" />
                )}
                {thread.messages.map((message) =>
                  message.role === "event" ? (
                    <div key={message.id} className="sd-event">
                      {message.body}
                    </div>
                  ) : (
                    <div key={message.id} className={`sd-bubble ${BUBBLE_CLASS[message.role]}`}>
                      {message.body}
                      <div className="sd-bubble-meta">
                        {ROLE_LABEL[message.role]} · {formatDateTime(message.created_at)}
                      </div>
                    </div>
                  )
                )}
              </div>

              {thread.conversation.human_takeover ? (
                thread.conversation.channel_kind === "whatsapp" ? (
                  <form
                    onSubmit={sendReply}
                    style={{
                      display: "flex", gap: 8, padding: 12,
                      borderTop: "1px solid var(--sd-border)",
                    }}
                  >
                    <input
                      className="sd-input"
                      placeholder="Reply as staff…"
                      value={draftReply}
                      disabled={!can("agent") || sending}
                      onChange={(e) => setDraftReply(e.target.value)}
                    />
                    <button className="sd-btn" disabled={!can("agent") || sending || !draftReply.trim()}>
                      {sending ? "Sending…" : "Send"}
                    </button>
                  </form>
                ) : (
                  <div style={{ padding: 12, borderTop: "1px solid var(--sd-border)" }}>
                    <PendingNotice>
                      This is a Voice conversation — there is no way to send an
                      outbound message into an ended call. Takeover here only
                      stops O'Brien from replying to any further messages.
                    </PendingNotice>
                  </div>
                )
              ) : null}
              {sendError && (
                <div style={{ padding: "0 12px 12px" }}>
                  <ErrorNotice message={sendError} />
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

/* ----------------------------------------------------------- customers */

export function CustomersPage() {
  const { call } = useSession();
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const { data, loading, error } = useApi(
    `/customers${query ? `?q=${encodeURIComponent(query)}` : ""}`,
    [query]
  );

  if (detail) {
    return (
      <>
        <PageHead
          title={customerLabel(detail.customer)}
          subtitle={detail.customer.phone || detail.customer.email || ""}
          action={
            <button className="sd-btn is-ghost" onClick={() => setDetail(null)}>
              Back to customers
            </button>
          }
        />
        <div className="sd-grid sd-grid-2">
          <Card title="Profile">
            <div className="sd-status-row"><span>Phone</span><span>{detail.customer.phone || "—"}</span></div>
            <div className="sd-status-row"><span>Email</span><span>{detail.customer.email || "—"}</span></div>
            <div className="sd-status-row"><span>First contact</span><span>{formatDate(detail.customer.first_contact_at)}</span></div>
            <div className="sd-status-row"><span>Last contact</span><span>{formatDate(detail.customer.last_contact_at)}</span></div>
          </Card>
          <Card title="Conversations">
            {detail.conversations.length === 0 ? (
              <EmptyState title="No conversations" />
            ) : (
              detail.conversations.map((row) => (
                <div key={row.id} className="sd-status-row">
                  <span>{row.last_message_preview || row.channel_kind}</span>
                  <span>{relativeTime(row.last_message_at)}</span>
                </div>
              ))
            )}
          </Card>
          <Card title="Leads">
            {detail.leads.length === 0 ? <EmptyState title="No leads" /> :
              detail.leads.map((lead) => (
                <div key={lead.id} className="sd-status-row">
                  <span>{lead.interest || "—"}</span>
                  <Badge tone="accent">{lead.status}</Badge>
                </div>
              ))}
          </Card>
          <Card title="Bookings">
            {detail.bookings.length === 0 ? <EmptyState title="No bookings" /> :
              detail.bookings.map((booking) => (
                <div key={booking.id} className="sd-status-row">
                  <span>{booking.service || "—"}</span>
                  <span>{formatDateTime(booking.starts_at)}</span>
                </div>
              ))}
          </Card>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHead title="Customers" subtitle="Everyone who has contacted this business." />
      <div className="sd-toolbar">
        <input
          className="sd-input sd-search"
          placeholder="Search by name, phone or email…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <ErrorNotice message={error} />
      <Card>
        {loading && <Loading />}
        {!loading && (data?.items?.length ?? 0) === 0 && (
          <EmptyState title="No customers yet" description="Customers are created automatically from inbound conversations." />
        )}
        {!loading && (data?.items?.length ?? 0) > 0 && (
          <table className="sd-table">
            <thead>
              <tr>
                <th>Name</th><th>Phone</th><th>Email</th>
                <th>Conversations</th><th>First contact</th><th>Last contact</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((customer) => (
                <tr
                  key={customer.id}
                  className="is-clickable"
                  onClick={() => call(`/customers/${customer.id}`).then(setDetail)}
                >
                  <td>{customer.full_name || "—"}</td>
                  <td>{customer.phone || "—"}</td>
                  <td>{customer.email || "—"}</td>
                  <td>{customer.conversation_count}</td>
                  <td>{formatDate(customer.first_contact_at)}</td>
                  <td>{formatDate(customer.last_contact_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

/* --------------------------------------------------------------- leads */

const LEAD_STATUSES = ["new", "contacted", "qualified", "converted", "lost"];

export function LeadsPage() {
  const { call, can } = useSession();
  const [status, setStatus] = useState("");
  const { data, loading, error, refresh } = useApi(
    `/leads${status ? `?status=${status}` : ""}`,
    [status]
  );

  async function updateStatus(lead, nextStatus) {
    await call(`/leads/${lead.id}`, { method: "PATCH", body: { status: nextStatus } });
    refresh();
  }

  return (
    <>
      <PageHead title="Leads" subtitle="Enquiries worth following up." />
      <div className="sd-toolbar">
        <button className={`sd-chip ${status === "" ? "is-active" : ""}`} onClick={() => setStatus("")}>All</button>
        {LEAD_STATUSES.map((value) => (
          <button key={value} className={`sd-chip ${status === value ? "is-active" : ""}`} onClick={() => setStatus(value)}>
            {value.charAt(0).toUpperCase() + value.slice(1)}
          </button>
        ))}
      </div>
      <ErrorNotice message={error} />
      <PendingNotice>
        Automatic lead creation from conversations is modelled but not yet
        active — O'Brien will start writing leads here in a later phase.
      </PendingNotice>
      <Card className="" >
        {loading && <Loading />}
        {!loading && (data?.items?.length ?? 0) === 0 && (
          <EmptyState title="No leads yet" />
        )}
        {!loading && (data?.items?.length ?? 0) > 0 && (
          <table className="sd-table">
            <thead>
              <tr><th>Customer</th><th>Source</th><th>Interest</th><th>Status</th><th>Assigned</th><th>Date</th></tr>
            </thead>
            <tbody>
              {data.items.map((lead) => (
                <tr key={lead.id}>
                  <td>{customerLabel(lead.customer)}</td>
                  <td style={{ textTransform: "capitalize" }}>{lead.source}</td>
                  <td>{lead.interest || "—"}</td>
                  <td>
                    <select
                      className="sd-select"
                      style={{ width: 132 }}
                      value={lead.status}
                      disabled={!can("agent")}
                      onChange={(e) => updateStatus(lead, e.target.value)}
                    >
                      {LEAD_STATUSES.map((value) => (
                        <option key={value} value={value}>{value}</option>
                      ))}
                    </select>
                  </td>
                  <td>{lead.assigned_user?.email || "Unassigned"}</td>
                  <td>{formatDate(lead.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

/* ------------------------------------------------------------ bookings */

const SCOPES = [["upcoming", "Upcoming"], ["past", "Past"], ["cancelled", "Cancelled"]];

function CalendarConnectionCard() {
  const { call, can } = useSession();
  const { data, loading, error, refresh } = useApi("/calendar/status");
  const [connecting, setConnecting] = useState(false);
  const [actionError, setActionError] = useState(null);

  async function connect() {
    setConnecting(true);
    setActionError(null);
    try {
      const result = await call("/calendar/connect");
      window.location.href = result.authorize_url;
    } catch (err) {
      setActionError(err.message);
      setConnecting(false);
    }
  }

  async function disconnect() {
    setActionError(null);
    try {
      await call("/calendar/disconnect", { method: "POST" });
      refresh();
    } catch (err) {
      setActionError(err.message);
    }
  }

  if (loading) return null;

  const connection = data?.connection;
  const isConnected = connection?.status === "connected";

  return (
    <Card
      title="Google Calendar"
      action={
        <Badge tone={isConnected ? "ok" : ""}>
          <Dot state={isConnected ? "ok" : "off"} />
          {isConnected ? "Connected" : "Not connected"}
        </Badge>
      }
    >
      <ErrorNotice message={error || actionError} />
      {isConnected ? (
        <>
          <div className="sd-status-row"><span>Account</span><span>{connection.connected_email || "—"}</span></div>
          <div className="sd-status-row"><span>Calendar</span><span>{connection.calendar_id}</span></div>
          <div className="sd-status-row"><span>Connected</span><span>{formatDateTime(connection.connected_at)}</span></div>
          {connection.last_sync_error && (
            <p className="sd-pending">Last sync issue: {connection.last_sync_error}</p>
          )}
          <button className="sd-btn is-ghost" style={{ marginTop: 10 }}
            onClick={disconnect} disabled={!can("owner")}>
            Disconnect
          </button>
        </>
      ) : (
        <>
          <p className="sd-hint" style={{ marginBottom: 12 }}>
            Connect this business's own Google Calendar so bookings made
            here — by O'Brien or by staff — create real calendar events.
            Calendar is the source of truth once connected.
          </p>
          <button className="sd-btn" onClick={connect}
            disabled={connecting || !can("owner") || !data?.configured_on_server}>
            {connecting ? "Redirecting…" : "Connect Google Calendar"}
          </button>
          {!data?.configured_on_server && (
            <p className="sd-hint">Google Calendar is not yet configured on the server.</p>
          )}
        </>
      )}
    </Card>
  );
}

export function BookingsPage() {
  const [scope, setScope] = useState("upcoming");
  const { data, loading, error } = useApi(`/bookings?scope=${scope}`, [scope]);

  return (
    <>
      <PageHead title="Bookings" subtitle="Appointments and reservations for this business." />

      <div className="sd-grid sd-grid-2" style={{ marginBottom: 16 }}>
        <CalendarConnectionCard />
      </div>

      <div className="sd-toolbar">
        {SCOPES.map(([key, label]) => (
          <button key={key} className={`sd-chip ${scope === key ? "is-active" : ""}`} onClick={() => setScope(key)}>
            {label}
          </button>
        ))}
      </div>
      <ErrorNotice message={error} />
      <PendingNotice>
        Bookings made in an external system are recorded here as references
        only. SmartDesk does not have API access to those systems, so their
        availability cannot be read or written from this dashboard yet.
      </PendingNotice>
      <Card>
        {loading && <Loading />}
        {!loading && (data?.items?.length ?? 0) === 0 && (
          <EmptyState title={`No ${scope} bookings`} />
        )}
        {!loading && (data?.items?.length ?? 0) > 0 && (
          <table className="sd-table">
            <thead>
              <tr><th>Customer</th><th>Service</th><th>Date / time</th><th>Status</th><th>Source</th><th>Reference</th></tr>
            </thead>
            <tbody>
              {data.items.map((booking) => (
                <tr key={booking.id}>
                  <td>{customerLabel(booking.customer)}</td>
                  <td>{booking.service || "—"}</td>
                  <td>{formatDateTime(booking.starts_at)}</td>
                  <td>
                    <Badge tone={booking.status === "confirmed" ? "ok" : booking.status === "cancelled" ? "alert" : ""}>
                      {booking.status}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={booking.source === "ai" ? "accent" : ""}>
                      {booking.source === "ai" ? "AI" : booking.source === "staff" ? "Staff" : "External"}
                    </Badge>
                  </td>
                  <td style={{ color: "var(--sd-ink-faint)", fontSize: 12 }}>
                    {booking.external_system
                      ? `${booking.external_system}${booking.external_reference ? ` · ${booking.external_reference}` : ""}`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
