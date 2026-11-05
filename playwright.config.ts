import { defineConfig, devices } from '@playwright/test';

// The harness runs the `smoke` and `a11y` projects as the `browser` and `a11y` checks.
const port = Number(process.env.E2E_PORT ?? 3100);

export default defineConfig({
  testDir: 'e2e',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${port}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'smoke', testMatch: 'harness/smoke.spec.ts', use: { ...devices['Desktop Chrome'] } },
    { name: 'a11y', testMatch: 'harness/a11y.spec.ts', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: {
    command: `pnpm build && pnpm start --port ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: false,
    timeout: 300_000,
  },
});
