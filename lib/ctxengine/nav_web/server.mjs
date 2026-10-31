// JSON-over-stdio server for the TypeScript navigator (CX2). One JSON object per line in, one per line out.
// stdout carries protocol lines only; anything else goes to stderr.
// Usage: node server.mjs --root DIR --tsconfig FILE [--typescript DIR_OF_TYPESCRIPT_PACKAGE]
import path from "node:path";
import readline from "node:readline";
import { NavError, NavHost, loadTypescript } from "./host.mjs";

console.log = (...a) => console.error(...a);
const MAX_MESSAGE = 300;

function write(obj) {
    process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function failure(e) {
    if (e instanceof NavError) return { status: e.status, message: String(e.message).slice(0, MAX_MESSAGE), next: e.next };
    return { status: "error", message: String(e && e.message ? e.message : e).slice(0, MAX_MESSAGE), next: "report this; the helper restarts on the next call" };
}

function parseArgs(argv) {
    const out = {};
    for (let i = 0; i < argv.length; i += 2) out[argv[i].replace(/^--/, "")] = argv[i + 1];
    return out;
}

const a = parseArgs(process.argv.slice(2));
let host;
try {
    if (!a.root || !a.tsconfig) throw new NavError("bad_request", "--root and --tsconfig are required");
    const root = path.resolve(a.root);
    const tsconfig = path.resolve(root, a.tsconfig);
    const { ts, pkg } = loadTypescript(root, path.dirname(tsconfig), a.typescript);
    host = new NavHost(ts, { root, tsconfig });
    write({ event: "ready", status: "ok", typescript: ts.version, typescript_path: pkg, pid: process.pid });
} catch (e) {
    write({ event: "error", ...failure(e) });
    process.exit(2);
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
    if (!line.trim()) return;
    let req;
    try {
        req = JSON.parse(line);
    } catch {
        write({ id: null, status: "bad_request", message: "not JSON" });
        return;
    }
    const t0 = process.hrtime.bigint();
    let out;
    try {
        out = host.run(req);
    } catch (e) {
        out = failure(e);
    }
    out.server_ms = Number((process.hrtime.bigint() - t0) / 1000000n);
    write({ id: req.id ?? null, ...out });
});
rl.on("close", () => process.exit(0));
