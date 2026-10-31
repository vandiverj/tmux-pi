import { run } from '@/app/run.ts';
import type { Config } from './config.ts';
import { slug } from '@util';
import { host } from '@host';
import data from './data.json' with { type: 'json' };
import * as path from 'node:path';

// import { commentedOut } from './commented_out.ts';
export { run } from './app/run.ts';
export * from '@shared/types.ts';

const note = "import './in_string.ts'";
const lazy = () => import('./lazy.ts');
const config: Config = { name: slug('Main') };

export function main(): string {
    return [run(config), host, JSON.stringify(data), path.sep, note, String(lazy)].join(' ');
}
