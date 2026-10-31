// Drives NavHost directly to show stale detection: a file that changes while an answer is computed gives stale_index once.
// Usage: node stale_driver.mjs ROOT TSCONFIG TYPESCRIPT_PACKAGE FILE   (prints two answers as JSON)
import fs from "node:fs";
import { NavHost, loadTypescript } from "../../../lib/ctxengine/nav_web/host.mjs";

const [root, tsconfig, pkg, file] = process.argv.slice(2);
const { ts } = loadTypescript(root, root, pkg);
let armed = true;
const host = new NavHost(ts, {
    root,
    tsconfig,
    hooks: {
        beforeVerify: (op, changed) => {
            if (!armed) return;
            armed = false;
            fs.appendFileSync(changed, "\n// edited while the answer was computed\n");
        },
    },
});
const answer = () => {
    try {
        return host.run({ op: "exports", file });
    } catch (e) {
        return { status: e.status ?? "error", message: e.message };
    }
};
process.stdout.write(`${JSON.stringify([answer(), answer()])}\n`);
