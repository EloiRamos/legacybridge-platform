/** Keyboard focus that a browser does not handle by itself (found in WebKit). */

/** The control that opened a dialog, for returning focus to it. Safari does not focus a button that is clicked, so the
 * focused element alone is not enough there: a click of the last moment names its control. */
let last: { control: HTMLElement; at: number } | null = null;

document.addEventListener("click", (e) => {
  const control = e.target instanceof Element ? e.target.closest<HTMLElement>("button, a[href], input, select, textarea, [tabindex]") : null;
  last = control && control.tabIndex >= 0 ? { control, at: performance.now() } : null;
}, true);

export function opener(): HTMLElement | null {
  if (last && performance.now() - last.at < 1000 && last.control.isConnected) return last.control;
  return document.activeElement as HTMLElement | null;
}

// Moving the focus with Tab scrolls the new control into view, but WebKit does so without its scroll margin, which keeps
// it clear of the fixed bars on a phone; an explicit request honours it.
document.addEventListener("focusin", (e) => {
  const el = e.target;
  if (el instanceof HTMLElement && el.closest("main.main") && el.matches(":focus-visible")) el.scrollIntoView({ block: "nearest", inline: "nearest" });
});

/** Arrow keys on a focused region that scrolls sideways: the same step in every browser. */
export function scrollSideways(e: { key: string; target: EventTarget; currentTarget: HTMLElement; preventDefault: () => void }) {
  if (e.target !== e.currentTarget || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
  e.preventDefault();
  e.currentTarget.scrollLeft += e.key === "ArrowRight" ? 40 : -40;
}
