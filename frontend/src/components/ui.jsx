import { useEffect, useRef, useState } from "react";
import { NavLink } from "react-router-dom";
import { useSession } from "../lib/session.jsx";

export function Card({ title, action, children, className = "" }) {
  return (
    <section className={`sd-card ${className}`}>
      {(title || action) && (
        <header className="sd-card-head">
          {title ? <h2>{title}</h2> : <span />}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Dot({ state = "off" }) {
  return <span className={`sd-dot is-${state}`} aria-hidden="true" />;
}

export function Badge({ children, tone = "" }) {
  return <span className={`sd-badge ${tone ? `is-${tone}` : ""}`}>{children}</span>;
}

export function EmptyState({ title, description }) {
  return (
    <div className="sd-empty">
      <strong>{title}</strong>
      {description && <span>{description}</span>}
    </div>
  );
}

/* Shown wherever a capability is modelled but not yet wired up, so the UI
 * never implies functionality that does not exist. */
export function PendingNotice({ children }) {
  return <p className="sd-pending">{children}</p>;
}

export function Loading({ label = "Loading…" }) {
  return <div className="sd-empty">{label}</div>;
}

export function ErrorNotice({ message }) {
  if (!message) return null;
  return <div className="sd-error">{message}</div>;
}

export function Toggle({ checked, onChange, disabled }) {
  return (
    <button
      type="button"
      className={`sd-toggle ${checked ? "is-on" : ""}`}
      aria-pressed={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

export function SwitchRow({ label, description, checked, onChange, disabled }) {
  return (
    <div className="sd-switch">
      <div className="sd-switch-text">
        <strong>{label}</strong>
        {description && <span>{description}</span>}
      </div>
      <Toggle checked={checked} onChange={onChange} disabled={disabled} />
    </div>
  );
}

export function PageHead({ title, subtitle, action }) {
  return (
    <header className="sd-page-head">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 16,
        }}
      >
        <div>
          <h1>{title}</h1>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </div>
    </header>
  );
}

function TenantSwitcher() {
  const { activeTenant, tenants, canSwitchTenants, selectTenant } = useSession();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const onClick = (event) => {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  if (!activeTenant) return null;

  const roleLabel =
    activeTenant.role === "platform_admin"
      ? "SmartDesk admin"
      : activeTenant.role.charAt(0).toUpperCase() + activeTenant.role.slice(1);

  return (
    <div className="sd-tenant" ref={ref}>
      <button
        type="button"
        className="sd-tenant-button"
        disabled={!canSwitchTenants}
        onClick={() => canSwitchTenants && setOpen((value) => !value)}
      >
        <span>
          <span className="sd-tenant-name">{activeTenant.name}</span>
          <br />
          <span className="sd-tenant-role">{roleLabel}</span>
        </span>
        {canSwitchTenants && <span aria-hidden="true">▾</span>}
      </button>

      {open && (
        <div className="sd-tenant-menu" role="menu">
          {tenants.map((tenant) => (
            <button
              key={tenant.id}
              type="button"
              className={tenant.id === activeTenant.id ? "is-active" : ""}
              onClick={() => {
                selectTenant(tenant.id);
                setOpen(false);
              }}
            >
              {tenant.name}
              {tenant.is_test_data && " · test"}
              {tenant.status === "development" && " · dev"}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const NAV_GROUPS = [
  {
    label: "SmartDesk AI",
    items: [
      { to: "/", label: "Overview", icon: "◈", end: true },
      { to: "/conversations", label: "Conversations", icon: "◉" },
      { to: "/customers", label: "Customers", icon: "◍" },
      { to: "/leads", label: "Leads", icon: "◇" },
      { to: "/bookings", label: "Bookings", icon: "▤" },
      { to: "/knowledge", label: "Knowledge Base", icon: "▣" },
      { to: "/automations", label: "Automations", icon: "⚙" },
      { to: "/analytics", label: "Analytics", icon: "▦" },
    ],
  },
  {
    items: [
      { to: "/receptionist", label: "AI Receptionist", icon: "◎" },
      { to: "/whatsapp", label: "WhatsApp", icon: "✆" },
      { to: "/voice", label: "Voice", icon: "☎" },
    ],
  },
  {
    items: [
      { to: "/business", label: "Business", icon: "▢" },
      { to: "/settings", label: "Settings", icon: "⚒" },
    ],
  },
  { items: [{ to: "/account", label: "Account", icon: "◐" }] },
];

export function Sidebar() {
  return (
    <aside className="sd-sidebar">
      <div className="sd-brand">
        <span className="sd-brand-mark">S</span>
        SmartDesk AI
      </div>

      <TenantSwitcher />

      <nav className="sd-nav">
        {NAV_GROUPS.map((group, index) => (
          <div key={index}>
            {group.label ? (
              <div className="sd-nav-section">{group.label}</div>
            ) : (
              <div className="sd-nav-divider" />
            )}
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `sd-nav-item ${isActive ? "is-active" : ""}`
                }
              >
                <span className="sd-nav-icon">{item.icon}</span>
                {item.label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}
