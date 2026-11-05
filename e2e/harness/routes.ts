import { readdirSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const appDir = join(process.cwd(), 'src', 'app');

// Every static App Router page, so a new page is checked without editing this file.
// Dynamic segments and the login callback need data or an identity provider, so they are skipped.
export function staticRoutes(dir = appDir): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      return entry.name.startsWith('[') || entry.name.startsWith('(.') ? [] : staticRoutes(path);
    }
    if (entry.name !== 'page.tsx') {
      return [];
    }
    const route = `/${relative(appDir, dir).split(sep).filter(Boolean).join('/')}`;
    return route.startsWith('/login') ? [] : [route];
  });
}
