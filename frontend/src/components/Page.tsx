import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { useSession } from "../session";

/** Page header: eyebrow, serif title, optional lede, actions and section tabs. */
export function PageHead({ eyebrow, title, lede, actions, tabs }: { eyebrow: ReactNode; title: ReactNode; lede?: ReactNode; actions?: ReactNode; tabs?: ReactNode }) {
  return (
    <div>
      <div className="ph">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h1 className="d" tabIndex={-1}>{title}</h1>
          {lede ? <p className="lede">{lede}</p> : null}
        </div>
        {actions ? <div className="acts">{actions}</div> : null}
      </div>
      {tabs}
    </div>
  );
}

/** Secondary navigation inside Orders: All orders · Reconciliation · Customers · Inventory. */
export function OrdersTabs({ attention }: { attention?: number }) {
  const { can } = useSession();
  const { pathname } = useLocation();
  const tabs: [string, string, string, ReactNode?][] = [
    ["/operations", "All orders", "operation.read"],
    ["/reconciliation", "Reconciliation", "operation.read", attention ? <span className="ct">{attention}<span className="vh"> unresolved</span></span> : null],
    ["/customers", "Customers", "customer.read"],
    ["/inventory", "Inventory", "inventory.read"],
  ];
  const active = pathname.startsWith("/customers") ? "/customers" : pathname === "/operations/new" ? "" : pathname;
  return (
    <nav className="stabs" aria-label="Orders sections">
      {tabs.filter((t) => can(t[2])).map(([to, label, , extra]) => (
        <Link key={to} to={to} aria-current={active === to ? "page" : undefined}>{label}{extra}</Link>
      ))}
    </nav>
  );
}

export function Card({ children, className = "", ...rest }: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLElement>) {
  return <section className={`card card-p ${className}`} {...rest}>{children}</section>;
}
