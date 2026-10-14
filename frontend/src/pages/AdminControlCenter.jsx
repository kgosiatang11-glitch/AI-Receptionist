import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Badge,
  Card,
  Dot,
  EmptyState,
  ErrorNotice,
  Loading,
  PageHead,
  SwitchRow,
} from "../components/ui.jsx";
import { useApi, useSession } from "../lib/session.jsx";

function StatCard({ label, value, description }) {
  return (
    <Card>
      <div style={{ padding: 18 }}>
        <div style={{ fontSize: 13, opacity: 0.7 }}>{label}</div>
        <div style={{ fontSize: 30, fontWeight: 700, marginTop: 6 }}>
          {value ?? "—"}
        </div>
        {description && (
          <div style={{ fontSize: 12, opacity: 0.65, marginTop: 5 }}>
            {description}
          </div>
        )}
      </div>
    </Card>
  );
}

function useAdminData(path) {
  const { call } = useSession();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setError("");

      try {
        const result = await call(path);
        if (!cancelled) setData(result);
      } catch (err) {
        if (!cancelled) {
          setError(err?.message || "Unable to load Control Center data.");
        }
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, [call, path, revision]);

  return { data, error, refresh: () => setRevision((value) => value + 1) };
}

/* -------------------------------------------------------------------------- */
/* Overview                                                                   */
/* -------------------------------------------------------------------------- */

export function AdminOverviewPage() {
  const { data, error } = useAdminData("/admin/overview");

  return (
    <>
      <PageHead
        title="Control Center"
        subtitle="Platform-wide overview of SmartDesk businesses, receptionists, channels and conversations."
      />

      <ErrorNotice message={error} />

      {!data && !error ? (
        <Loading />
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
            gap: 16,
          }}
        >
          <StatCard
            label="Total businesses"
            value={data?.total_tenants}
          />
          <StatCard
            label="Active businesses"
            value={data?.active_tenants}
          />
          <StatCard
            label="Inactive businesses"
            value={data?.inactive_tenants}
          />
          <StatCard
            label="Active AI receptionists"
            value={data?.active_receptionists}
          />
          <StatCard
            label="Connected channels"
            value={data?.connected_channels}
          />
          <StatCard
            label="Conversations"
            value={data?.total_conversations}
          />
        </div>
      )}

      <div style={{ marginTop: 20 }}>
        <Card title="Platform management">
          <div style={{ padding: 18 }}>
            <p style={{ marginTop: 0 }}>
              Manage businesses, users, AI receptionists and connected
              channels from the Control Center.
            </p>

            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 10,
              }}
            >
              <Link className="sd-button" to="/admin/tenants">
                Manage businesses
              </Link>

              <Link className="sd-button" to="/admin/users">
                Manage users
              </Link>

              <Link className="sd-button" to="/admin/receptionists">
                AI receptionists
              </Link>

              <Link className="sd-button" to="/admin/channels">
                Channels
              </Link>

              <Link className="sd-button" to="/admin/whatsapp-onboarding">
                WhatsApp onboarding
              </Link>
            </div>
          </div>
        </Card>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Users                                                                      */
/* -------------------------------------------------------------------------- */

export function AdminUsersPage() {
  const { data, error } = useAdminData("/admin/users");
  const api = useApi();

  const [savingId, setSavingId] = useState(null);
  const [localError, setLocalError] = useState("");

  async function updateUser(user, changes) {
    setSavingId(user.id);
    setLocalError("");

    try {
      await api(`/admin/users/${user.id}`, {
        method: "PATCH",
        body: changes,
      });

      window.location.reload();
    } catch (err) {
      setLocalError(err?.message || "Unable to update user.");
      setSavingId(null);
    }
  }

  const users = data?.items || [];

  return (
    <>
      <PageHead
        title="Users"
        subtitle="View SmartDesk accounts, platform administrators and business memberships."
      />

      <ErrorNotice message={error || localError} />

      {!data && !error ? (
        <Loading />
      ) : users.length === 0 ? (
        <EmptyState
          title="No users found"
          description="No SmartDesk accounts are currently available."
        />
      ) : (
        <Card>
          <div className="sd-table-wrap">
            <table className="sd-table">
              <thead>
                <tr>
                  <th>User</th>
                  <th>Businesses</th>
                  <th>Platform Admin</th>
                  <th>Account</th>
                </tr>
              </thead>

              <tbody>
                {users.map((user) => (
                  <tr key={user.id}>
                    <td>
                      <strong>{user.full_name || "Unnamed user"}</strong>
                      <br />
                      <span style={{ opacity: 0.7 }}>{user.email}</span>
                    </td>

                    <td>
                      {user.memberships?.length ? (
                        <div>
                          {user.memberships.map((membership) => (
                            <div key={membership.tenant_id}>
                              {membership.tenant_name} · {membership.role}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span style={{ opacity: 0.6 }}>No business</span>
                      )}
                    </td>

                    <td>
                      <SwitchRow
                        label=""
                        checked={Boolean(user.is_platform_admin)}
                        disabled={savingId === user.id}
                        onChange={(value) =>
                          updateUser(user, {
                            is_platform_admin: value,
                          })
                        }
                      />
                    </td>

                    <td>
                      <button
                        type="button"
                        className="sd-button"
                        disabled={savingId === user.id}
                        onClick={() =>
                          updateUser(user, {
                            is_active: !user.is_active,
                          })
                        }
                      >
                        {user.is_active ? "Deactivate" : "Activate"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Receptionists                                                              */
/* -------------------------------------------------------------------------- */

export function AdminReceptionistsPage() {
  const { data, error } = useAdminData("/admin/receptionists");
  const rows = data?.items || [];

  return (
    <>
      <PageHead
        title="AI Receptionists"
        subtitle="View the receptionist status across every SmartDesk business."
      />

      <ErrorNotice message={error} />

      {!data && !error ? (
        <Loading />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No AI receptionists"
          description="No receptionist profiles have been created yet."
        />
      ) : (
        <Card>
          <div className="sd-table-wrap">
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Business</th>
                  <th>Receptionist</th>
                  <th>Status</th>
                </tr>
              </thead>

              <tbody>
                {rows.map((row) => (
                  <tr key={row.tenant_id}>
                    <td>
                      <strong>{row.tenant_name}</strong>
                    </td>

                    <td>{row.display_name || "Unnamed receptionist"}</td>

                    <td>
                      <Badge tone={row.is_active ? "success" : "muted"}>
                        <Dot state={row.is_active ? "on" : "off"} />
                        {row.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Channels                                                                   */
/* -------------------------------------------------------------------------- */

export function AdminChannelsPage() {
  const { data, error } = useAdminData("/admin/channels");
  const rows = data?.items || [];

  return (
    <>
      <PageHead
        title="Channels"
        subtitle="Monitor connected WhatsApp and voice channels across the platform."
      />

      <ErrorNotice message={error} />

      {!data && !error ? (
        <Loading />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No channels connected"
          description="Connected receptionist channels will appear here."
        />
      ) : (
        <Card>
          <div className="sd-table-wrap">
            <table className="sd-table">
              <thead>
                <tr>
                  <th>Business</th>
                  <th>Channel</th>
                  <th>Address</th>
                  <th>Status</th>
                </tr>
              </thead>

              <tbody>
                {rows.map((row, index) => (
                  <tr
                    key={`${row.tenant_id}-${row.kind}-${row.address || index}`}
                  >
                    <td>
                      <strong>{row.tenant_name}</strong>
                    </td>

                    <td>{row.kind}</td>

                    <td>{row.address || "—"}</td>

                    <td>
                      <Badge tone={row.is_active ? "success" : "muted"}>
                        <Dot state={row.is_active ? "on" : "off"} />
                        {row.is_active ? "Active" : "Inactive"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Analytics                                                                  */
/* -------------------------------------------------------------------------- */

export function AdminAnalyticsPage() {
  const { data, error } = useAdminData("/admin/overview");

  return (
    <>
      <PageHead
        title="Analytics"
        subtitle="Platform-level usage metrics currently available from SmartDesk."
      />

      <ErrorNotice message={error} />

      {!data && !error ? (
        <Loading />
      ) : (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
              gap: 16,
            }}
          >
            <StatCard
              label="Businesses"
              value={data?.total_tenants}
              description="All provisioned businesses"
            />

            <StatCard
              label="Active businesses"
              value={data?.active_tenants}
              description="Businesses currently active"
            />

            <StatCard
              label="AI receptionists"
              value={data?.active_receptionists}
              description="Active receptionist profiles"
            />

            <StatCard
              label="Connected channels"
              value={data?.connected_channels}
              description="Active platform channels"
            />

            <StatCard
              label="Conversations"
              value={data?.total_conversations}
              description="Total conversations recorded"
            />
          </div>

          <div style={{ marginTop: 20 }}>
            <Card title="Analytics roadmap">
              <div style={{ padding: 18 }}>
                <p style={{ marginTop: 0 }}>
                  Detailed usage analytics can be added as conversation,
                  message, booking and plan-usage metrics are exposed by the
                  platform.
                </p>
              </div>
            </Card>
          </div>
        </>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* System Settings                                                            */
/* -------------------------------------------------------------------------- */

export function AdminSystemSettingsPage() {
  return (
    <>
      <PageHead
        title="System Settings"
        subtitle="Platform-level settings and operational controls."
      />

      <Card title="Platform status">
        <div style={{ padding: 18 }}>
          <SwitchRow
            label="SmartDesk platform"
            description="The Control Center is connected to the application backend."
            checked={true}
            disabled={true}
            onChange={() => {}}
          />
        </div>
      </Card>

      <div style={{ marginTop: 20 }}>
        <Card title="Coming platform controls">
          <div style={{ padding: 18 }}>
            <p style={{ marginTop: 0 }}>
              Billing configuration, platform-wide limits, integrations,
              maintenance controls and other global settings can be added
              here as their backend services are implemented.
            </p>
          </div>
        </Card>
      </div>
    </>
  );
}

function WhatsAppOnboardingRow({ row, busy, onAction }) {
  const [form, setForm] = useState(() => ({
    twilio_sender_sid: row.twilio_sender_sid || "",
    twilio_configuration_recorded: Boolean(row.twilio_configuration_recorded),
    registration_status: row.registration_status,
    phone_verification_status: row.phone_verification_status,
    meta_verification_status: row.meta_verification_status,
    outstanding_requirements: (row.outstanding_requirements || []).join("\n"),
    provisioning_error: row.provisioning_error || "",
    check_source: row.provider_check_source || "administrator_confirmation",
  }));
  const set = (field, value) => setForm((current) => ({ ...current, [field]: value }));
  const save = () => onAction(row, "update", {
    twilio_sender_sid: form.twilio_sender_sid || null,
    twilio_configuration_recorded: form.twilio_configuration_recorded,
    registration_status: form.registration_status,
    phone_verification_status: form.phone_verification_status,
    meta_verification_status: form.meta_verification_status,
    outstanding_requirements: form.outstanding_requirements.split("\n").map((item) => item.trim()).filter(Boolean),
    provisioning_error: form.provisioning_error,
  });
  const disabled = busy === row.id;
  return <tr>
    <td><strong>{row.business_name}</strong><br /><span style={{ opacity: .7 }}>{row.owner?.email || "No linked owner"}</span></td>
    <td>{row.requested_number}<br /><Badge tone={row.status === "active" ? "ok" : row.status === "failed" ? "alert" : "warn"}>{row.status.replaceAll("_", " ")}</Badge></td>
    <td style={{ minWidth: 250 }}>
      <label className="sd-label">Twilio sender SID</label><input className="sd-input" value={form.twilio_sender_sid} placeholder="PN…" disabled={disabled} onChange={(e) => set("twilio_sender_sid", e.target.value)} />
      <label className="sd-check" style={{ marginTop: 8 }}><input type="checkbox" checked={form.twilio_configuration_recorded} disabled={disabled} onChange={(e) => set("twilio_configuration_recorded", e.target.checked)} /> Configuration recorded</label>
      <label className="sd-label">Sender registration</label><select className="sd-select" value={form.registration_status} disabled={disabled} onChange={(e) => set("registration_status", e.target.value)}>{["unverified", "pending", "registered", "failed"].map((value) => <option key={value}>{value}</option>)}</select>
    </td>
    <td style={{ minWidth: 220 }}>
      <label className="sd-label">Phone verification</label><select className="sd-select" value={form.phone_verification_status} disabled={disabled} onChange={(e) => set("phone_verification_status", e.target.value)}>{["not_applicable", "pending", "confirmed", "failed", "unverified"].map((value) => <option key={value}>{value}</option>)}</select>
      <label className="sd-label">Meta verification</label><select className="sd-select" value={form.meta_verification_status} disabled={disabled} onChange={(e) => set("meta_verification_status", e.target.value)}>{["not_applicable", "pending", "confirmed", "failed", "unverified"].map((value) => <option key={value}>{value}</option>)}</select>
      <label className="sd-label">Outstanding requirements (one per line)</label><textarea className="sd-input" rows="3" value={form.outstanding_requirements} disabled={disabled} onChange={(e) => set("outstanding_requirements", e.target.value)} />
      <label className="sd-label">Provisioning note / error</label><input className="sd-input" value={form.provisioning_error} disabled={disabled} onChange={(e) => set("provisioning_error", e.target.value)} />
      <button className="sd-button" disabled={disabled} onClick={save}>Save record</button>
    </td>
    <td style={{ minWidth: 220 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "review")}>Review</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "provision")}>Provision</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "ready_for_testing")}>Ready for testing</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "record_check", { passed: true, source: form.check_source })}>Record passed check</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "record_check", { passed: false, source: form.check_source, error: form.provisioning_error })}>Record failed check</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "activate")}>Activate</button>
        <button className="sd-button" disabled={disabled} onClick={() => onAction(row, "suspend")}>Suspend</button>
      </div>
      <label className="sd-label" style={{ marginTop: 10 }}>Check source</label><select className="sd-select" value={form.check_source} disabled={disabled} onChange={(e) => set("check_source", e.target.value)}><option value="administrator_confirmation">Administrator attestation</option><option value="provider_console">Provider console</option><option value="test_webhook">Test webhook</option></select>
      <p className="sd-hint">Provider checks are administrator attestations. SmartDesk does not contact Twilio or Meta automatically.</p>
      {row.last_provider_check_at && <small>Last check: {row.last_provider_check_passed ? "passed" : "failed"} ({row.provider_check_source})</small>}
    </td>
  </tr>;
}

export function AdminWhatsAppOnboardingPage() {
  const { data, error, refresh } = useAdminData("/admin/whatsapp-onboardings");
  const { call } = useSession();
  const [busy, setBusy] = useState(null);
  const [localError, setLocalError] = useState("");
  async function act(row, action, fields = {}) {
    setBusy(row.id);
    setLocalError("");

    try {
      await call(`/admin/whatsapp-onboardings/${row.id}`, {
        method: "PATCH",
        body: { action, ...fields },
      });

      refresh();
    } catch (err) {
      setLocalError(err.message);
    } finally {
      setBusy(null);
    }
  }
  const rows = data?.items || [];
  return <><PageHead title="WhatsApp onboarding" subtitle="Record manual provisioning evidence, verification status, and administrator-attested provider checks." />
    <ErrorNotice message={error || localError} />
    {!data && !error ? <Loading /> : rows.length === 0 ? <EmptyState title="No WhatsApp onboarding requests" /> : <Card><div className="sd-table-wrap"><table className="sd-table"><thead><tr><th>Business</th><th>Number / status</th><th>Twilio configuration</th><th>Verification & requirements</th><th>Actions</th></tr></thead><tbody>{rows.map((row) => <WhatsAppOnboardingRow key={row.id} row={row} busy={busy} onAction={act} />)}</tbody></table></div></Card>}
  </>;
}
