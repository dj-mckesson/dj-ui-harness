import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { staticRoutes } from './routes';

for (const route of staticRoutes()) {
  test(`${route} has no serious accessibility violations`, async ({ page }) => {
    await page.goto(route, { waitUntil: 'networkidle' });
    // WCAG 1.4.3 exempts text that is part of a logo. "Pricing Lab" in the header is the wordmark.
    const wordmark = page.getByRole('banner').getByText('Lab', { exact: true });
    for (const element of await wordmark.all()) {
      await element.evaluate((node) => node.setAttribute('data-brand-wordmark', ''));
    }

    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
      .exclude('[data-brand-wordmark]')
      .analyze();
    const serious = results.violations
      .filter((violation) => violation.impact === 'serious' || violation.impact === 'critical')
      .map((violation) => `${violation.id} (${violation.nodes.length}): ${violation.help}`);

    expect(serious, `axe violations on ${route}`).toEqual([]);
  });
}
