import { firefox } from 'playwright';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

const profile = path.join(process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share'),
  'tripo-studio-playwright-firefox');
const context = await firefox.launchPersistentContext(profile, {
  headless: false, viewport: { width: 1440, height: 900 }, acceptDownloads: true,
});
try {
  const cookies = JSON.parse(execFileSync('python3', [fileURLToPath(new URL('./import_firefox_cookies.py', import.meta.url))], { encoding: 'utf8' }));
  await context.addCookies(cookies);
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto('https://studio.tripo3d.ai/workspace/generate', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  const reject = page.getByRole('button', { name: 'Reject All' });
  await reject.click({ timeout: 8000 }).catch(() => {});
  const inputSpot = await page.evaluate(() => document.elementsFromPoint(166,189).slice(0,3).map(el => el.outerHTML.slice(0,450)));
  console.log('MULTIVIEW_ICON', JSON.stringify(inputSpot));
  await page.mouse.click(166,189);
  await page.waitForTimeout(500);
  await page.getByText('Geometry & Texture').click();
  await page.waitForTimeout(500);
  console.log('PANEL', JSON.stringify((await page.locator('body').innerText()).slice(0, 900)));
  const spots = await page.evaluate(() => Object.fromEntries([[374,630],[585,705]].map(([x,y]) =>
    [`${x},${y}`, document.elementsFromPoint(x,y).slice(0,4).map(el => el.outerHTML.slice(0,350))])));
  console.log('SETTINGS', JSON.stringify(spots));
  await page.screenshot({ path: path.join(profile, 'multiview-panel.png') });
} finally {
  await context.close();
}
