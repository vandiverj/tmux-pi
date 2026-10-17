/**
 * state-write — write tmux-pi task and session state without following a planted symlink.
 *
 * Every helper takes a trusted `root` (the session state dir; it may itself be a link) and a path
 * under it. Each directory below `root` down to the target is checked with lstat and refused if it
 * is a symlink. A link at the target file is replaced (whole-file writes, via rename) or refused
 * (appends, via O_NOFOLLOW); it is never written through. Writes into a task dir go through
 * writeTaskFile, which takes the state dir above the task dir (layout state/tasks/<id>) as the
 * root, so a link at tasks/ or at the task dir itself is refused as well.
 * This is the TypeScript twin of lib/work.py atomic_write.
 *
 * Kept in a subdirectory with no index.ts so pi does not load it as an extension.
 */

import { randomUUID } from "node:crypto";
import { closeSync, constants, fchmodSync, lstatSync, mkdirSync, openSync, renameSync, unlinkSync, writeSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

// The directory names from `root` down to `dir`; refuses a `dir` that is not under `root`.
function below(root: string, dir: string): string[] {
	const rel = relative(resolve(root), resolve(dir));
	if (rel === "") return [];
	if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) throw new Error(`${dir} is outside ${root}`);
	return rel.split(sep);
}

/** Throw if any directory under `root`, down to and including `dir`, is a symlink. A missing one is fine. */
export function refuseLinks(root: string, dir: string): void {
	let at = resolve(root);
	for (const name of below(root, dir)) {
		at = join(at, name);
		let info;
		try { info = lstatSync(at); } catch (error: any) {
			if (error?.code === "ENOENT") return;
			throw error;
		}
		if (info.isSymbolicLink()) throw new Error(`refusing to write through a symlink at ${at}`);
	}
}

/** mkdir -p `dir` below `root`, refusing a symlink at any level. `root` itself is created if absent and trusted. */
export function ensureDir(root: string, dir: string): void {
	mkdirSync(root, { recursive: true });
	let at = resolve(root);
	for (const name of below(root, dir)) {
		at = join(at, name);
		try { mkdirSync(at); } catch (error: any) {
			if (error?.code !== "EEXIST") throw error;
		}
		const info = lstatSync(at);
		if (info.isSymbolicLink()) throw new Error(`refusing to write through a symlink at ${at}`);
		if (!info.isDirectory()) throw new Error(`${at} is not a directory`);
	}
}

function writeAll(fd: number, data: string | Uint8Array): void {
	const bytes = typeof data === "string" ? Buffer.from(data) : data;
	for (let done = 0; done < bytes.length;) done += writeSync(fd, bytes, done);
}

// The permission bits of an existing regular file at `file`; undefined for a missing file, a link or a non-file.
function existingMode(file: string): number | undefined {
	try {
		const info = lstatSync(file);
		return info.isFile() ? info.mode & 0o7777 : undefined;
	} catch (error: any) {
		if (error?.code === "ENOENT") return undefined;
		throw error;
	}
}

/**
 * Replace `file` with `data`: a fresh temp file in the same directory (exclusive create, no
 * follow), then rename onto the target. A symlink at `file` is replaced, never written through.
 * An explicit `mode` always wins and is applied at create time under the umask, like the
 * writeFileSync(temp, { mode }) + rename it replaces; pass 0o666 for a fresh default-mode file.
 * With no `mode`, as for an in-place writeFileSync, an existing regular file keeps its permission
 * bits (a 0600 file stays 0600), and a new file or a replaced link gets 0o666 under the umask.
 */
export function writeFileAtomic(root: string, file: string, data: string | Uint8Array, mode?: number): void {
	refuseLinks(root, dirname(file));
	const keep = mode === undefined ? existingMode(file) : undefined;
	const temp = `${file}.${randomUUID()}.tmp`;
	try {
		const fd = openSync(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, keep ?? mode ?? 0o666);
		try {
			if (keep !== undefined) fchmodSync(fd, keep); // the umask may have cleared bits at create time
			writeAll(fd, data);
		} finally { closeSync(fd); }
		renameSync(temp, file);
	} catch (error) {
		try { unlinkSync(temp); } catch { /* the write may have failed before the temp file existed */ }
		throw error;
	}
}

/** The state dir that holds a task dir (layout state/tasks/<id>): the trusted root for every write into that task dir. */
export function taskStateRoot(taskDir: string): string {
	return dirname(dirname(resolve(taskDir)));
}

/** writeFileAtomic for `name` in a task dir, checking every directory from the state dir down: tasks/, then the task dir. */
export function writeTaskFile(taskDir: string, name: string, data: string | Uint8Array, mode?: number): void {
	writeFileAtomic(taskStateRoot(taskDir), join(taskDir, name), data, mode);
}

/** Append to `file`; a symlink at `file` makes the open fail instead of being followed. */
export function appendFileSafe(root: string, file: string, data: string, mode = 0o666): void {
	refuseLinks(root, dirname(file));
	const fd = openSync(file, constants.O_APPEND | constants.O_CREAT | constants.O_WRONLY | constants.O_NOFOLLOW, mode);
	try { writeAll(fd, data); } finally { closeSync(fd); }
}
