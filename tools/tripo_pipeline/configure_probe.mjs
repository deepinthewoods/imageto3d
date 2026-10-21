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
  await page.getByRole('button', { name: 'Reject All' }).click({ timeout: 3000 }).catch(() => {});
  await page.locator('div[class*="i-tripo:multi-view"]').click();
  await page.getByText('H3.1 – Best Quality').click();
  await page.getByText('H3.0 – Fast & Balanced').click();
  await page.getByText('Geometry & Texture').first().click();
  await page.waitForTimeout(600);
  const panel = page.getByRole('dialog', { name: 'Geometry & Texture' });
  const pbrToggle = panel.getByText('PBR', { exact: true }).locator('xpath=..').getByRole('switch');
  if (await pbrToggle.getAttribute('aria-checked') === 'true') await pbrToggle.click();
  await panel.getByText('2K', { exact: true }).click();
  await panel.getByText('Quad', { exact: true }).click();
  await page.waitForTimeout(600);
  console.log(JSON.stringify({ model: (await page.locator('body').innerText()).match(/H3\.[01] – [^\n]+/)?.[0],
    generate: (await page.locator('body').innerText()).match(/Generate\n\d+/)?.[0],
    pbr: await pbrToggle.getAttribute('aria-checked'),
    texture: await panel.getByText('2K', { exact: true }).evaluate(el => el.closest('[data-state]')?.getAttribute('data-state')),
    quad: await panel.getByText('Quad', { exact: true }).evaluate(el => el.closest('[data-state]')?.getAttribute('data-state')),
    inputs: await panel.locator('input').evaluateAll(nodes => nodes.map(n => ({ type: n.type, value: n.value, min: n.min, max: n.max, outer: n.outerHTML.slice(0, 200) }))),
    fileInputs: await page.locator('input[type=file]').evaluateAll(nodes => nodes.map(n => ({ outer: n.outerHTML.slice(0, 300), parent: n.parentElement?.outerHTML.slice(0, 400) }))) }));
  await page.screenshot({ path: path.join(profile, 'configured-multiview.png') });
  const moduleDir = path.resolve('../../multiview_ready/arcane_plate_mail_v2--c7f70712717e/human_male/body_armor');
  const imageInputs = page.locator('input[type=file][accept^="image/"]');
  if (await imageInputs.count() !== 4) throw new Error('Expected four multiview image inputs');
  for (const name of ['front.png', 'front_three_quarter.png', 'back_three_quarter.png', 'back.png']) {
    await imageInputs.first().setInputFiles(path.join(moduleDir, name));
    await page.waitForTimeout(600);
  }
  await page.waitForTimeout(3000);
  console.log('AFTER_UPLOAD', JSON.stringify({ text: (await page.locator('body').innerText()).slice(0, 520), fileInputs: await imageInputs.count() }));
  await page.screenshot({ path: path.join(profile, 'uploaded-body-quad.png') });
} finally {
  await context.close();
}
