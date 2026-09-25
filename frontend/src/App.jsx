import { Route, Routes } from "react-router-dom";
import { Sidebar } from "./components/ui.jsx";
import { useSession } from "./lib/session.jsx";
import {
  LoginPage,
  NoBusinessPage,
  OverviewPage,
  ResetPasswordPage,
  VerifyEmailPage,
} from "./pages/Overview.jsx";
import {
  BookingsPage,
  ConversationsPage,
  CustomersPage,
  LeadsPage,
} from "./pages/Crm.jsx";
import {
  AccountPage,
  AnalyticsPage,
  AutomationsPage,
  BusinessPage,
  KnowledgeBasePage,
  ReceptionistPage,
  SettingsPage,
  VoicePage,
  WhatsAppPage,
} from "./pages/Config.jsx";
import { AdminTenantDetailPage, AdminTenantsPage } from "./pages/AdminTenants.jsx";

function RequirePlatformAdmin({ children }) {
  const { user } = useSession();
  if (!user?.is_platform_admin) {
    return (
      <div className="sd-card">
        <h2>SmartDesk administrator access required</h2>
        <p style={{ color: "var(--sd-ink-soft)", fontSize: 13 }}>
          This section is only available to SmartDesk platform administrators.
        </p>
      </div>
    );
  }
  return children;
}

export default function App() {
  const { session, loading, error, tenants, user, passwordRecovery } = useSession();

  // The user followed a "reset password" email link. This takes priority
  // over everything else -- they must set a new password before landing
  // anywhere else, authenticated or not.
  if (passwordRecovery) return <ResetPasswordPage />;

  if (loading) {
    return <div className="sd-login"><div className="sd-empty">Loading…</div></div>;
  }

  // No Supabase session: the only thing rendered is the login screen. No
  // dashboard route is reachable, and no API call is made.
  if (!session) return <LoginPage />;

  if (error) {
    return (
      <div className="sd-login">
        <div className="sd-login-card sd-card">
          <h2>Could not load your account</h2>
          <p style={{ color: "var(--sd-ink-soft)", fontSize: 13 }}>{error}</p>
        </div>
      </div>
    );
  }

  // Authenticated, but Supabase has not confirmed the email yet. The
  // backend never grants tenant access in this state (see
  // smartdesk/security/rbac.py::_is_email_confirmed), so /me always comes
  // back with an empty tenant list here regardless of any membership.
  if (user && user.email_verified === false) {
    return <VerifyEmailPage />;
  }

  // Authenticated and verified, but not a member of any tenant. Showing an
  // empty dashboard would be misleading, so say so plainly.
  if (tenants.length === 0) {
    return <NoBusinessPage />;
  }

  return (
    <div className="sd-shell">
      <Sidebar />
      <main className="sd-main">
        <div className="sd-content">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/admin/tenants" element={<RequirePlatformAdmin><AdminTenantsPage /></RequirePlatformAdmin>} />
            <Route path="/admin/tenants/:id" element={<RequirePlatformAdmin><AdminTenantDetailPage /></RequirePlatformAdmin>} />
            <Route path="/conversations" element={<ConversationsPage />} />
            <Route path="/customers" element={<CustomersPage />} />
            <Route path="/leads" element={<LeadsPage />} />
            <Route path="/bookings" element={<BookingsPage />} />
            <Route path="/knowledge" element={<KnowledgeBasePage />} />
            <Route path="/automations" element={<AutomationsPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/receptionist" element={<ReceptionistPage />} />
            <Route path="/whatsapp" element={<WhatsAppPage />} />
            <Route path="/voice" element={<VoicePage />} />
            <Route path="/business" element={<BusinessPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/account" element={<AccountPage />} />
            <Route path="*" element={<OverviewPage />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
