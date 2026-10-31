// A stand-in for lib/ctxengine/nav_web/server.mjs that misbehaves on purpose. Its behaviour is the text of `<root>/mode`:
//   hang         ready, then never answers
//   hang_after_first   answers the first request, then never answers
//   slow_first  ready; the first request is held until a second arrives, then both are answered (a build that outlasts one call)
//   die_once     ready; the first request kills it (a marker file records that), later launches answer
//   die_always   ready; every request kills it
//   stale_once   answers the first request with stale_index, then ok
//   stale_always answers stale_index every time
//   env          answers with the names of the environment variables it was started with
import fs from "node:fs";
import readline from "node:readline";

const root = process.argv[process.argv.indexOf("--root") + 1];
const mode = fs.readFileSync(`${root}/mode`, "utf8").trim();
const marker = `${root}/died`;
const write = (o) => process.stdout.write(`${JSON.stringify(o)}\n`);

write({ event: "ready", status: "ok", typescript: "fake", pid: process.pid });
let seen = 0;
let held = null;
readline.createInterface({ input: process.stdin }).on("line", (line) => {
    const req = JSON.parse(line);
    seen += 1;
    if (mode === "hang" || (mode === "hang_after_first" && seen > 1)) return;
    if (mode === "slow_first" && seen === 1) {
        held = req; // the first request is answered only when a second one arrives
        return;
    }
    if (held) {
        write({ id: held.id, status: "ok", late: true });
        held = null;
    }
    if (mode === "die_always") process.exit(1);
    if (mode === "die_once" && !fs.existsSync(marker)) {
        fs.writeFileSync(marker, "1");
        process.exit(1);
    }
    if (mode === "stale_always" || (mode === "stale_once" && seen === 1)) {
        write({ id: req.id, status: "stale_index", message: "changed", next: "repeat" });
        return;
    }
    if (mode === "env") {
        write({ id: req.id, status: "ok", env: Object.keys(process.env).sort() });
        return;
    }
    write({ id: req.id, status: "ok", pid: process.pid, seen });
});
