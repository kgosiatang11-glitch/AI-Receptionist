import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { formatDate, formatDateTime } from "../lib/api.js";
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

const PLANS = ["basic", "professional", "enterprise"];
const STATUSES = ["active", "development", "suspended"];

/* --------------------------------------------------------------- overview */

function PlatformStats() {
  const { data, loading, error } = useApi("/admin/overview");
  if (loading) return null;
  if (error) return <ErrorNotice message={error} />;
  if (!data) return null;

  const cards = [
    ["Total tenants", data.total_tenants],
    ["Active tenants", data.active_tenants],
    ["Inactive tenants", data.inactive_tenants],
    ["Active receptionists", data.active_receptionists],
    ["Connected channels", data.connected_channels],
    ["Total conversations", data.total_conversations],
  ];

  return (
    <div className="sd-grid sd-grid-metrics" style={{ marginBottom: 16 }}>
      {cards.map(([label, value]) => (
        <Card key={label}>
          <div className="sd-metric-label">{label}</div>
          <div className="sd-metric-value">{value}</div>
        </Card>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------- create form */

function CreateTenantForm({ onCreated, onCancel }) {
  const { call } = useSession();
  const [form, setForm] = useState({
    business_name: "", business_category: "", owner_name: "",
    owner_email: "", phone: "", plan: "basic", status: "active",
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const set = (key) => (e) => setForm((prev) => ({ ...prev, [key]: e.target.value }));

  async function submit(event) {
    event.preventDefault();
    if (!form.business_name.trim()) {
      setError("Business name is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const tenant = await call("/admin/tenants", { method: "POST", body: form });
      onCreated(tenant);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Create Tenant">
      <form onSubmit={submit}>
        <div className="sd-grid sd-grid-2">
          <div className="sd-field">
            <label className="sd-label">Business name</label>
            <input className="sd-input" value={form.business_name}
              onChange={set("business_name")} disabled={submitting} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Business category</label>
            <input className="sd-input" placeholder="e.g. sports, beauty"
              value={form.business_category} onChange={set("business_category")}
              disabled={submitting} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Owner name</label>
            <input className="sd-input" value={form.owner_name}
              onChange={set("owner_name")} disabled={submitting} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Owner email</label>
            <input className="sd-input" type="email" value={form.owner_email}
              onChange={set("owner_email")} disabled={submitting} />
            <p className="sd-hint">
              The owner links their own account after signing up — nothing is
              created or emailed automatically.
            </p>
          </div>
          <div className="sd-field">
            <label className="sd-label">Phone</label>
            <input className="sd-input" value={form.phone}
              onChange={set("phone")} disabled={submitting} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Plan</label>
            <select className="sd-select" value={form.plan}
              onChange={set("plan")} disabled={submitting}>
              {PLANS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="sd-field">
            <label className="sd-label">Status</label>
            <select className="sd-select" value={form.status}
              onChange={set("status")} disabled={submitting}>
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>
        <ErrorNotice message={error} />
        <div style={{ display: "flex", gap: 8 }}>
          <button className="sd-btn" disabled={submitting}>
            {submitting ? "Creating…" : "Create Tenant"}
          </button>
          <button type="button" className="sd-btn is-ghost" onClick={onCancel} disabled={submitting}>
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );
}

/* --------------------------------------------------------------- list page */

export function AdminTenantsPage() {
  const navigate = useNavigate();
  const { selectTenant } = useSession();
  const { data, loading, error, refresh } = useApi("/admin/tenants");
  const [showCreate, setShowCreate] = useState(false);

  function openDashboard(tenant) {
    selectTenant(tenant.id);
    navigate("/");
  }

  return (
    <>
      <PageHead title="Tenants" subtitle="Every business on the SmartDesk AI platform."
        action={
          !showCreate && (
            <button className="sd-btn" onClick={() => setShowCreate(true)}>
              + Create Tenant
            </button>
          )
        } />

      <PlatformStats />

      {showCreate && (
        <div style={{ marginBottom: 16 }}>
          <CreateTenantForm
            onCreated={() => { setShowCreate(false); refresh(); }}
            onCancel={() => setShowCreate(false)}
          />
        </div>
      )}

      <ErrorNotice message={error} />
      <Card>
        {loading && <Loading />}
        {!loading && (data?.items?.length ?? 0) === 0 && (
          <EmptyState title="No tenants yet" description="Create the first one above." />
        )}
        {!loading && (data?.items?.length ?? 0) > 0 && (
          <table className="sd-table">
            <thead>
              <tr>
                <th>Business</th><th>Owner</th><th>Receptionist</th>
                <th>Channel</th><th>Status</th><th>Created</th><th></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((tenant) => (
                <tr key={tenant.id} className="is-clickable"
                  onClick={() => navigate(`/admin/tenants/${tenant.id}`)}>
                  <td>{tenant.name}</td>
                  <td>
                    {tenant.owner_account.linked
                      ? tenant.owner_account.email
                      : tenant.owner_email
                        ? <Badge tone="warn">Not linked</Badge>
                        : "—"}
                  </td>
                  <td>
                    <Badge tone={tenant.receptionist.is_active ? "ok" : ""}>
                      <Dot state={tenant.receptionist.is_active ? "ok" : "off"} />
                      {tenant.receptionist.is_active ? "Active" : "Setup required"}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={tenant.channels.whatsapp ? "ok" : ""}>
                      {tenant.channels.whatsapp ? "WhatsApp connected" : "Not connected"}
                    </Badge>
                  </td>
                  <td>
                    <Badge tone={tenant.status === "active" ? "ok" : tenant.status === "suspended" ? "alert" : "warn"}>
                      {tenant.status}
                    </Badge>
                  </td>
                  <td>{formatDate(tenant.created_at)}</td>
                  <td>
                    <button className="sd-btn is-ghost"
                      onClick={(e) => { e.stopPropagation(); openDashboard(tenant); }}>
                      Open Dashboard
                    </button>
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

/* ------------------------------------------------------------- detail page */

export function AdminTenantDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { call, selectTenant } = useSession();
  const { data: tenant, loading, error, refresh } = useApi(`/admin/tenants/${id}`, [id]);
  const [form, setForm] = useState(null);
  const [linkEmail, setLinkEmail] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [linkError, setLinkError] = useState(null);
  const [linking, setLinking] = useState(false);

  useEffect(() => { if (tenant) setForm(tenant); }, [tenant]);

  if (loading || !form) return <Loading />;
  if (error) return <ErrorNotice message={error} />;

  const set = (key) => (e) => setForm((prev) => ({ ...prev, [key]: e.target.value }));

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      await call(`/admin/tenants/${id}`, {
        method: "PATCH",
        body: {
          business_name: form.name,
          business_category: form.business_type,
          plan: form.plan,
          owner_name: form.owner_name,
          owner_email: form.owner_email,
          phone: form.owner_phone,
        },
      });
      refresh();
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function toggleStatus() {
    const nextStatus = form.status === "active" ? "suspended" : "active";
    setSaving(true);
    try {
      await call(`/admin/tenants/${id}`, { method: "PATCH", body: { status: nextStatus } });
      refresh();
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function linkOwner(event) {
    event.preventDefault();
    setLinking(true);
    setLinkError(null);
    try {
      await call(`/admin/tenants/${id}/owner`, { method: "POST", body: { email: linkEmail } });
      setLinkEmail("");
      refresh();
    } catch (err) {
      setLinkError(err.message);
    } finally {
      setLinking(false);
    }
  }

  function openDashboard() {
    selectTenant(tenant.id);
    navigate("/");
  }

  return (
    <>
      <PageHead title={tenant.name} subtitle={`/${tenant.slug}`}
        action={
          <div style={{ display: "flex", gap: 8 }}>
            <button className="sd-btn is-ghost" onClick={() => navigate("/admin/tenants")}>
              Back to Tenants
            </button>
            <button className="sd-btn" onClick={openDashboard}>Open Dashboard</button>
          </div>
        } />

      <div className="sd-grid sd-grid-2">
        <Card title="Business"
          action={<button className="sd-btn is-ghost" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>}>
          <ErrorNotice message={saveError} />
          <div className="sd-field">
            <label className="sd-label">Business name</label>
            <input className="sd-input" value={form.name} onChange={set("name")} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Category</label>
            <input className="sd-input" value={form.business_type || ""} onChange={set("business_type")} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Plan</label>
            <select className="sd-select" value={form.plan} onChange={set("plan")}>
              {PLANS.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="sd-status-row">
            <span>Status</span>
            <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Badge tone={tenant.status === "active" ? "ok" : "alert"}>{tenant.status}</Badge>
              <button className="sd-btn is-ghost" onClick={toggleStatus} disabled={saving}>
                {tenant.status === "active" ? "Deactivate" : "Activate"}
              </button>
            </span>
          </div>
        </Card>

        <Card title="Receptionist & Channel">
          <div className="sd-status-row">
            <span>Receptionist</span>
            <span>
              <Dot state={tenant.receptionist.is_active ? "ok" : "off"} />
              {tenant.receptionist.is_active ? "Active" : "Setup required"}
            </span>
          </div>
          <div className="sd-status-row">
            <span>WhatsApp</span>
            <span>
              <Dot state={tenant.channels.whatsapp ? "ok" : "off"} />
              {tenant.channels.whatsapp ? "Connected" : "Not connected"}
            </span>
          </div>
          <div className="sd-status-row">
            <span>Voice</span>
            <span>
              <Dot state={tenant.channels.voice ? "ok" : "off"} />
              {tenant.channels.voice ? "Connected" : "Not connected"}
            </span>
          </div>
        </Card>

        <Card title="Owner">
          <div className="sd-field">
            <label className="sd-label">Owner name</label>
            <input className="sd-input" value={form.owner_name || ""} onChange={set("owner_name")} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Owner email</label>
            <input className="sd-input" value={form.owner_email || ""} onChange={set("owner_email")} />
          </div>
          <div className="sd-field">
            <label className="sd-label">Phone</label>
            <input className="sd-input" value={form.owner_phone || ""} onChange={set("phone")} />
          </div>
          <div className="sd-status-row">
            <span>Account</span>
            <Badge tone={tenant.owner_account.linked ? "ok" : "warn"}>
              {tenant.owner_account.linked ? `Linked — ${tenant.owner_account.email}` : "Not linked"}
            </Badge>
          </div>
        </Card>

        <Card title="Link Owner">
          <p className="sd-hint" style={{ marginBottom: 12 }}>
            The owner must sign up via the login page first, using this exact
            email. Once they have an account, linking gives them owner access
            to only this tenant.
          </p>
          <form onSubmit={linkOwner}>
            <div className="sd-field">
              <label className="sd-label">Owner's email</label>
              <input className="sd-input" type="email" value={linkEmail}
                onChange={(e) => setLinkEmail(e.target.value)} disabled={linking} />
            </div>
            <ErrorNotice message={linkError} />
            <button className="sd-btn" disabled={linking || !linkEmail.trim()}>
              {linking ? "Linking…" : "Link Owner"}
            </button>
          </form>
        </Card>
      </div>
    </>
  );
}
