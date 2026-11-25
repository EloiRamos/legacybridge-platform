// Asks the live-validation questions in the assistant drawer, as a signed-in person (evals/live_assistant.py).
// node live_drawer.mjs <origin> <out-dir> <user> <questions-json>   (password of the fictional evaluation persona in env)
import { createRequire } from "node:module";

const require = createRequire(new URL("../../frontend/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const [, , ORIGIN, OUT, USER, QUESTIONS] = process.argv;
const browser = await chromium.launch();
try {
  const page = await (await browser.newContext({ baseURL: ORIGIN, viewport: { width: 1440, height: 900 } })).newPage();
  await page.goto("/login");
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Password").fill(process.env.LBF_DRIVER_PASSWORD ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"));
  const shown = [];
  for (const [i, q] of JSON.parse(QUESTIONS).entries()) {
    // the second question is asked from another page: the conversation belongs to the session, not to the page
    if (i === 1) await page.locator("nav[aria-label=Main]").getByRole("link", { name: "Approvals" }).click();
    await page.getByRole("button", { name: "Open assistant" }).click();
    const d = page.getByRole("dialog", { name: "Assistant" });
    await d.getByLabel("Ask the assistant").fill(q);
    await d.getByRole("button", { name: "Ask", exact: true }).click(); // exactly one submission per question
    const card = d.getByRole("region", { name: `Answer to: ${q}` });
    await card.waitFor({ timeout: 150_000 });
    shown.push({ question: await card.locator(".bub").innerText(), text: await card.innerText() });
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${OUT}/turn${i + 1}.png` });
    await page.keyboard.press("Escape");
  }
  console.log(JSON.stringify(shown));
} finally {
  await browser.close();
}
