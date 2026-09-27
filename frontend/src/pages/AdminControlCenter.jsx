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
import { useApi } from "../lib/session.jsx";

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
  const api = useApi();
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setError("");

      try {
        const result = await api(path);
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
  }, [api, path]);

  return { data, error };
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