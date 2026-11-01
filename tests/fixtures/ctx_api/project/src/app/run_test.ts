import { LIMIT, run } from '@/app/run.ts';
import { host } from '@host';

export const checked = [run({ name: host }), LIMIT];
