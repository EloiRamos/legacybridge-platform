import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Forbidden, Loading, NotFound } from "./components/States";
import { Admin } from "./pages/Admin";
import { Approvals } from "./pages/Approvals";
import { Assistant } from "./pages/Assistant";
import { Audit } from "./pages/Audit";
import { CustomerCard } from "./pages/CustomerCard";
import { Customers } from "./pages/Customers";
import { Dashboard } from "./pages/Dashboard";
import { Inventory } from "./pages/Inventory";
import { Login, Reauth } from "./pages/Login";
import { NewOperation } from "./pages/NewOperation";
import { OperationDetail } from "./pages/OperationDetail";
import { Operations } from "./pages/Operations";
import { Reconciliation } from "./pages/Reconciliation";
import { SessionProvider, useSession } from "./session";

function RequireSession({ children }: { children: ReactNode }) {
  const { session, loading } = useSession();
  const location = useLocation();
  if (loading) return <Loading what="session" />;
  if (!session) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />;
  return <>{children}</>;
}

/** Hides a page the role cannot use. This mirrors, and never replaces, the server's permission check. */
function Need({ any, children }: { any: string[]; children: ReactNode }) {
  const { can } = useSession();
  if (!any.some((p) => can(p))) {
    // the sysadmin has no operation.read: its start page is Administration
    if (any.includes("operation.read") && can("admin.users")) return <Navigate to="/admin" replace />;
    return <><h1 className="vh">Not available for your role</h1><Forbidden /></>;
  }
  return <>{children}</>;
}

export function App() {
  return (
    <BrowserRouter>
      <SessionProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<RequireSession><Layout /></RequireSession>}>
            <Route path="/reauth" element={<Reauth />} />
            <Route path="/" element={<Need any={["operation.read"]}><Dashboard /></Need>} />
            <Route path="/customers" element={<Need any={["customer.read"]}><Customers /></Need>} />
            <Route path="/customers/:backend/:nativeId" element={<Need any={["customer.read"]}><CustomerCard /></Need>} />
            <Route path="/inventory" element={<Need any={["inventory.read"]}><Inventory /></Need>} />
            <Route path="/operations" element={<Need any={["operation.read"]}><Operations /></Need>} />
            <Route path="/operations/new" element={<Need any={["draft.create"]}><NewOperation /></Need>} />
            <Route path="/operations/:id" element={<OperationDetail />} />
            <Route path="/approvals" element={<Need any={["approval.grant.standard", "approval.grant.any"]}><Approvals /></Need>} />
            <Route path="/reconciliation" element={<Need any={["operation.read"]}><Reconciliation /></Need>} />
            <Route path="/audit" element={<Need any={["audit.read"]}><Audit /></Need>} />
            <Route path="/assistant" element={<Need any={["assistant.use"]}><Assistant /></Need>} />
            <Route path="/admin" element={<Admin />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </SessionProvider>
    </BrowserRouter>
  );
}
