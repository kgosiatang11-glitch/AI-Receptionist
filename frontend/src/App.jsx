import { Route, Routes } from "react-router-dom";
import { Sidebar } from "./components/ui.jsx";
import { useSession } from "./lib/session.jsx";
import { LoginPage, OverviewPage } from "./pages/Overview.jsx";
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

export default function App() {
  const { session, loading, error, tenants } = useSession();

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

  // Authenticated but not a member of any tenant. Showing an empty dashboard
  // would be misleading, so say so plainly.
  if (tenants.length === 0) {
    return (
      <div className="sd-login">
        <div className="sd-login-card sd-card">
          <h2>No business assigned</h2>
          <p style={{ color: "var(--sd-ink-soft)", fontSize: 13 }}>
            Your account is not yet linked to a business. Ask a SmartDesk
            administrator to grant you access.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="sd-shell">
      <Sidebar />
      <main className="sd-main">
        <div className="sd-content">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
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
