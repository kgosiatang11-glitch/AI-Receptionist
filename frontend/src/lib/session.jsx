import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { apiRequest, supabase, supabaseConfigured } from "./api.js";

const SessionContext = createContext(null);
const TENANT_STORAGE_KEY = "smartdesk.activeTenantId";

export function SessionProvider({ children }) {
  const [session, setSession] = useState(null);
  const [profile, setProfile] = useState(null);
  const [activeTenantId, setActiveTenantId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!supabaseConfigured) {
      setLoading(false);
      return;
    }
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session ?? null);
      if (!data.session) setLoading(false);
    });
    const { data: listener } = supabase.auth.onAuthStateChange((_event, next) => {
      setSession(next);
      if (!next) {
        setProfile(null);
        setActiveTenantId(null);
        setLoading(false);
      }
    });
    return () => listener.subscription.unsubscribe();
  }, []);

  /* The tenant list comes from /me, which returns only the tenants this user
   * is actually entitled to. A business user never receives another tenant's
   * name, let alone its data. */
  const loadProfile = useCallback(async () => {
    if (!session) return;
    setLoading(true);
    try {
      const data = await apiRequest("/me");
      setProfile(data);
      setError(null);
      const stored = window.localStorage.getItem(TENANT_STORAGE_KEY);
      const valid = data.tenants.some((t) => t.id === stored);
      setActiveTenantId(valid ? stored : data.tenants[0]?.id ?? null);
    } catch (err) {
      setError(err.message);
      setProfile(null);
    } finally {
      setLoading(false);
    }
  }, [session]);

  useEffect(() => {
    if (session) loadProfile();
  }, [session, loadProfile]);

  const selectTenant = useCallback((tenantId) => {
    window.localStorage.setItem(TENANT_STORAGE_KEY, tenantId);
    setActiveTenantId(tenantId);
  }, []);

  const signOut = useCallback(async () => {
    window.localStorage.removeItem(TENANT_STORAGE_KEY);
    await supabase?.auth.signOut();
  }, []);

  const activeTenant = useMemo(
    () => profile?.tenants.find((t) => t.id === activeTenantId) ?? null,
    [profile, activeTenantId]
  );

  /* Role gating in the UI is a convenience only; the API enforces the same
   * rules independently. */
  const can = useCallback(
    (minimum) => {
      const order = ["viewer", "agent", "manager", "owner"];
      const role = activeTenant?.role;
      if (!role) return false;
      if (role === "platform_admin") return true;
      return order.indexOf(role) >= order.indexOf(minimum);
    },
    [activeTenant]
  );

  const call = useCallback(
    (path, options = {}) =>
      apiRequest(path, { ...options, tenantId: options.tenantId ?? activeTenantId }),
    [activeTenantId]
  );

  const value = {
    session,
    user: profile?.user ?? null,
    tenants: profile?.tenants ?? [],
    canSwitchTenants: profile?.can_switch_tenants ?? false,
    activeTenant,
    activeTenantId,
    selectTenant,
    signOut,
    loading,
    error,
    can,
    call,
    reload: loadProfile,
  };

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession() {
  const context = useContext(SessionContext);
  if (!context) throw new Error("useSession must be used inside SessionProvider");
  return context;
}

/* Small data-fetching hook used by every page. */
export function useApi(path, deps = [], options = {}) {
  const { call, activeTenantId } = useSession();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const skip = options.skip || !path;

  const refresh = useCallback(() => {
    if (skip || !activeTenantId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    call(path)
      .then((result) => {
        setData(result);
        setError(null);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, activeTenantId, skip, ...deps]);

  useEffect(refresh, [refresh]);
  return { data, loading, error, refresh };
}
