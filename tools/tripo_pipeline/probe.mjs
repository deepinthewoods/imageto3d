import { firefox } from 'playwright';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dataHome = process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share');
const profile = path.join(dataHome, 'tripo-studio-playwright-firefox');
const context = await firefox.launchPersistentContext(profile, {
  headless: false,
  viewport: { width: 1440, height: 900 },
  acceptDownloads: true,
});
try {
  const cookies = JSON.parse(execFileSync('python3', [fileURLToPath(new URL('./import_firefox_cookies.py', import.meta.url))], { encoding: 'utf8' }));
  await context.addCookies(cookies);
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto('https://studio.tripo3d.ai/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(6000);
  console.log(JSON.stringify({ url: page.url(), title: await page.title(),
    loginVisible: await page.getByText(/Sign up\/Log in/i).count(),
    buttons: (await page.locator('button').allTextContents()).map(s => s.trim()).filter(Boolean).slice(0, 30) }));
  await page.screenshot({ path: path.join(profile, 'studio-probe.png') });
} finally {
  await context.close();
}
