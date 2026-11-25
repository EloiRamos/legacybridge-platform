import { existsSync, mkdirSync, statSync } from "node:fs";

import { chromium, type FullConfig } from "@playwright/test";

import { STATE_DIR, password, stateFile, USERS } from "./helpers";

// Signs each persona in once per run and stores the HttpOnly session cookie as Playwright storage state, so the
// suite stays far below the per-address login rate limit (10/min, SEC-05). The CSRF token is never stored: the
// console fetches a fresh one from GET /auth/session into memory on load.
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0]!.use.baseURL!;
  mkdirSync(STATE_DIR, { recursive: true });
  const browser = await chromium.launch();
  for (const username of USERS) {
    // approvals and resolutions need a recent authentication (step-up, SEC-06): reuse a state file only while it is
    // younger than 8 min, otherwise sign in again (7 personas stay below the 10/min per-address login limit)
    const file = stateFile(username);
    if (existsSync(file) && Date.now() - statSync(file).mtimeMs < 8 * 60 * 1000) continue;
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    await page.goto("/login");
    await page.getByLabel("Username").fill(username);
    await page.getByLabel("Password").fill(password(username));
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL((url) => !url.pathname.startsWith("/login"));
    await context.storageState({ path: stateFile(username) });
    await context.close();
  }
  await browser.close();
}
