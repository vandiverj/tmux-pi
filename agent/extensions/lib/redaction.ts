/**
 * redaction — what sensitive.ts and safe.ts share, so both number placeholders in the same store.
 *
 * Kept in a subdirectory with no index.ts so pi does not load it as an extension.
 */

// Tools whose output is redacted before it leaves them: sensitive.ts lets them past the sensitive-read confirm.
export const SAFE_TOOLS = ["safe_read", "safe_grep"];

/** The redaction session of this pi process: a worker's task id, an explicit session, or the parent pid. */
export const redactionSession = (): string => process.env.TP_TASK_ID || process.env.TP_REDACTION_SESSION || `pi-${process.ppid}`;
