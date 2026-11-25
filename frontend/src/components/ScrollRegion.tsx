import { type ReactNode, useEffect, useRef, useState } from "react";

import { scrollSideways } from "../focus";

/** Horizontal scroll wrapper for a wide table (WCAG 2.1.1). While its content overflows it is a named, keyboard-focusable
 * region (arrow keys scroll it); while everything fits it is a plain box, so it adds no tab stop. */
export function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setOverflows(el.scrollWidth > el.clientWidth);
    const ro = new ResizeObserver(measure); // the wrapper (viewport) and its table (data) can both change width
    ro.observe(el);
    if (el.firstElementChild) ro.observe(el.firstElementChild);
    measure();
    return () => ro.disconnect();
  }, []);
  return (
    <>
      <div ref={ref} className="tblw" {...(overflows ? { tabIndex: 0, role: "region", "aria-label": `${label}, scrollable table`, onKeyDown: scrollSideways } : {})}>{children}</div>
      {overflows ? <span className="scrollcue" aria-hidden="true">Scroll sideways for more columns →</span> : null}
    </>
  );
}
