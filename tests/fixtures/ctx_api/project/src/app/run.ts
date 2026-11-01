import type { Config } from '../config.ts';
import { slug } from '@/util/index.ts';

/** Run the app for one config. */
export function run(config: Config): string {
    return slug(config.name);
}

export const LIMIT = 5;
