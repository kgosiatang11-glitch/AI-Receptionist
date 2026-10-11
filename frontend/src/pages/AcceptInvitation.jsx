import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { apiRequest } from "../lib/api.js";
import { useSession } from "../lib/session.jsx";
import { Card, ErrorNotice } from "../components/ui.jsx";

export const ACCEPT_INVITATION_PATH = "/accept-invitation";

/* The invitation code is a one-time bearer secret, so this page is deliberately
 * careful with it:
 *
 *  - It is typed or pasted into the form and sent only in the POST body of
 *    /invitations/accept. It is never put in a URL, query string, fragment or
 *    header, so it cannot appear in an access log, the browser history or a
 *    Referer header. (The page does not read it from the URL either.)
 *  - It lives in component state only -- never localStorage/sessionStorage -- and
 *    is cleared as soon as it has been used.
 *  - Nothing here logs it: there is no console output, and every failure message
 *    is a fixed string -- neither the request nor the server's error text is
 *    echoed back.
 *
 * This page is rendered by App.jsx BEFORE its "no business yet" gate, because
 * someone accepting their first invitation has no business by definition. The
 * app-level gates still apply first: no session -> login page, unconfirmed
 * email -> verify-email page, so reaching this component means signed in. */

function failureMessage(err) {
  switch (err?.status) {
    case 403:
      return "Your email address has not been confirmed yet. Confirm it using the link we emailed you, then try again.";
    case 404:
      // The server deliberately does not say WHY (unknown, expired, already used,
      // revoked, or issued to a different address), so neither do we.
      return "This invitation is invalid or has expired. Check that you copied the whole code and that you are signed in with the email address that was invited, or ask the business owner for a new invitation.";
    case 401:
      return "Your session has expired. Please sign in again.";
    default:
      return "We could not accept the invitation right now. Please try again.";
  }
}

export function AcceptInvitationPage() {
  const { session, tenants, signOut, selectTenant, reload } = useSession();
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [accepted, setAccepted] = useState(null);

  async function submit(event) {
    event.preventDefault();
    const invitationCode = code.trim();
    if (!invitationCode || busy) return;
    setBusy(true);
    setError(null);
    try {
      // apiRequest, not the session's tenant-scoped call(): the caller has no
      // membership in the invited business yet, so no X-Tenant-Id is sent.
      const result = await apiRequest("/invitations/accept", {
        method: "POST",
        body: { invitation_code: invitationCode },
      });
      setAccepted({ tenant: result.tenant, role: result.role });
      setCode("");
    } catch (err) {
      setError(failureMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function openDashboard() {
    setBusy(true);
    selectTenant(accepted.tenant.id);
    // Refresh /me so the new membership is in the tenant list, then land on the
    // dashboard of the business that was just joined.
    await reload();
    navigate("/", { replace: true });
  }

  return (
    <div className="sd-login">
      <div className="sd-login-card">
        <div className="sd-brand" style={{ justifyContent: "center", paddingBottom: 18 }}>
          <span className="sd-brand-mark">S</span>
          SmartDesk AI
        </div>

        {accepted ? (
          <Card>
            <h2 style={{ marginBottom: 4 }}>You're in</h2>
            <p style={{ color: "var(--sd-ink-soft)", fontSize: 13, margin: "0 0 18px" }}>
              You now have access to <strong>{accepted.tenant?.name || "the business"}</strong>
              {accepted.role ? <> as <strong>{accepted.role}</strong></> : null}.
            </p>
            <button
              className="sd-btn"
              style={{ width: "100%", justifyContent: "center" }}
              disabled={busy}
              onClick={openDashboard}
            >
              {busy ? "Opening…" : "Open dashboard"}
            </button>
          </Card>
        ) : (
          <Card>
            <h2 style={{ marginBottom: 4 }}>Accept an invitation</h2>
            <p style={{ color: "var(--sd-ink-faint)", fontSize: 12.5, margin: "0 0 18px" }}>
              Enter the invitation code the business owner gave you. It works only
              once, only for the email address it was issued to
              {session?.user?.email ? <> (you are signed in as <strong>{session.user.email}</strong>)</> : null}
              , and only until it expires.
            </p>

            <ErrorNotice message={error} />

            <form onSubmit={submit} autoComplete="off">
              <div className="sd-field">
                <label className="sd-label" htmlFor="invitation-code">Invitation code</label>
                <input
                  id="invitation-code"
                  name="invitation_code"
                  className="sd-input"
                  type="text"
                  autoComplete="off"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  data-1p-ignore="true"
                  data-lpignore="true"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  disabled={busy}
                />
              </div>
              <button
                className="sd-btn"
                style={{ width: "100%", justifyContent: "center" }}
                disabled={busy || !code.trim()}
              >
                {busy ? "Joining…" : "Accept invitation"}
              </button>
            </form>
          </Card>
        )}

        {!accepted && (
          <p style={{ textAlign: "center", fontSize: 12.5, color: "var(--sd-ink-faint)", marginTop: 16 }}>
            {tenants.length > 0 && (
              <>
                <Link to="/" style={{ color: "var(--sd-accent)", fontWeight: 600 }}>
                  Back to dashboard
                </Link>
                {" · "}
              </>
            )}
            <button
              type="button"
              onClick={signOut}
              style={{
                background: "none", border: 0, padding: 0,
                color: "var(--sd-accent)", fontSize: 12.5, fontWeight: 600, cursor: "pointer",
              }}
            >
              Sign out
            </button>
          </p>
        )}
      </div>
    </div>
  );
}
