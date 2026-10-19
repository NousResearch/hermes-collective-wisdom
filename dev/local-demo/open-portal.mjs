// Opens the local Portal in a separate, throwaway Chromium window per user,
// already signed in (cookies from scripts/mint-account.ts).
//
//   node dev/local-demo/open-portal.mjs <nas-app-dir> <app-url> <privy-mock-url> <path> <name>=<account.json>...
//
// Uses the Playwright that the Portal repo already depends on and the
// Chrome for Testing it already downloaded; nothing is installed. Every request
// that is not to 127.0.0.1 is blocked, except the Portal's sign-in widget
// (auth.privy.io), which is answered by the Portal's own local Privy mock,
// exactly as the Portal's browser tests do. Stays open until killed.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";

const [nasDir, appUrl, privyUrl, path, ...users] = process.argv.slice(2);
const require = createRequire(join(nasDir, "package.json"));
const { chromium } = require("@playwright/test");

function chromeForTesting() {
  if (process.env.CW_CHROME) return process.env.CW_CHROME;
  const cache = join(homedir(), "Library/Caches/ms-playwright");
  if (!existsSync(cache)) return undefined;
  const builds = readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const build of builds) {
    for (const arch of ["chrome-mac-arm64", "chrome-mac"]) {
      const exe = join(cache, build, arch,
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");
      if (existsSync(exe)) return exe;
    }
  }
  return undefined;
}

const PRIVY = "https://auth.privy.io";
const mockPrivy = privyUrl.replace(/\/$/, "");
// CW_CHECK="<text>": headless self-test. Load the page, exit 0 if the text
// shows up within 3 minutes, else exit 1. Used by --smoke; opens no window.
const CHECK = process.env.CW_CHECK;
const browser = await chromium.launch({
  headless: Boolean(CHECK),
  executablePath: chromeForTesting(),
  chromiumSandbox: false,
  args: ["--no-sandbox", "--no-first-run", "--disable-background-networking",
    "--disable-component-update", "--disable-sync", "--metrics-recording-only"],
});

for (const spec of users) {
  const [name, file] = spec.split("=");
  const account = JSON.parse(readFileSync(file, "utf8"));
  const context = await browser.newContext({ viewport: null });
  await context.addCookies(account.cookies);
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.hostname === "127.0.0.1" || url.hostname === "localhost") return route.continue();
    if (url.origin === PRIVY) {
      const response = await fetch(request.url().replace(PRIVY, mockPrivy), {
        method: request.method(), headers: request.headers(), body: request.postData(),
      });
      return route.fulfill({
        status: response.status,
        headers: Object.fromEntries(response.headers.entries()),
        body: await response.text(),
      });
    }
    return route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  await page.goto(appUrl + path, { timeout: 180_000 }).catch(() => {});
  if (CHECK) {
    const found = await page.getByText(CHECK).first()
      .waitFor({ timeout: 180_000 }).then(() => true, () => false);
    process.stdout.write(`${name}: ${found ? "found" : "missing"} ${JSON.stringify(CHECK)} at ${page.url()}\n`);
    await browser.close();
    process.exit(found ? 0 : 1);
  }
  process.stdout.write(`opened ${name}: ${appUrl}${path}\n`);
}
browser.on("disconnected", () => process.exit(0));
await new Promise(() => {});
