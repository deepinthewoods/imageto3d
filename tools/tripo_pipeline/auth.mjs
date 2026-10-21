// One-time interactive Studio sign-in in a Firefox profile outside the repository.
import { firefox } from 'playwright';
import os from 'node:os';
import path from 'node:path';

const dataHome = process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share');
const profile = path.join(dataHome, 'tripo-studio-playwright-firefox');
const context = await firefox.launchPersistentContext(profile, {
  headless: false,
  viewport: { width: 1440, height: 900 },
  acceptDownloads: true,
});
const page = context.pages()[0] ?? await context.newPage();
await page.goto('https://studio.tripo3d.ai/', { waitUntil: 'domcontentloaded' });
console.log(`Tripo Studio is open in Playwright Firefox. Profile: ${profile}`);
console.log('Sign in in that browser window, then press Enter here to save and close the session.');
process.stdin.resume();
await new Promise(resolve => process.stdin.once('data', resolve));
await context.close();
