import { type FormEvent, type ReactNode, useEffect, useRef } from "react";

import { opener } from "../focus";
import { Icon } from "./Icon";

/** Side sheet (bottom sheet on phones) on a native modal <dialog>: focus is trapped, Escape closes, focus returns
 * to the opener — or to the page heading when the opener is gone or disabled. Used for every confirmation that changes
 * state. ``locked``: a request is in flight, so nothing may dismiss the sheet and suggest it was cancelled. A child marked
 * ``data-autofocus`` receives the initial focus. */
export function Sheet({ title, eyebrow, onClose, onSubmit, children, footer, busy, locked }: {
  title: string; eyebrow?: ReactNode; onClose: () => void; onSubmit: () => void; children: ReactNode; footer: ReactNode; busy?: boolean; locked?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    const from = opener();
    if (d && !d.open) d.showModal();
    d?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    return () => {
      d?.close();
      from?.focus?.();
      if (!from || from === document.body || document.activeElement !== from) document.querySelector<HTMLElement>("main h1[tabindex]")?.focus();
    };
  }, []);
  useEffect(() => {
    if (locked) ref.current?.querySelector<HTMLElement>("#sheet-title")?.focus(); // the pressed button is now disabled
  }, [locked]);
  const submit = (e: FormEvent) => { e.preventDefault(); onSubmit(); };
  return (
    <dialog ref={ref} className="sheet" aria-labelledby="sheet-title" onCancel={(e) => { e.preventDefault(); if (!locked) onClose(); }}
      onKeyDown={(e) => { if (locked && e.key === "Escape") e.preventDefault(); }}
      onClose={() => { if (locked && ref.current && !ref.current.open) ref.current.showModal(); /* a browser may close on a repeated Escape */ }}>
      <form onSubmit={submit} aria-busy={busy}>
        <div className="sheet-h">
          <div>
            {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
            <h2 id="sheet-title" tabIndex={-1}>{title}</h2>
          </div>
          <button type="button" className="btn x" onClick={onClose} disabled={locked} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="sheet-b">{children}</div>
        <div className="sheet-f">{footer}</div>
      </form>
    </dialog>
  );
}
