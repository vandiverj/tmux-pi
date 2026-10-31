// TypeScript language-service host for one tsconfig (CX2 ctx-nav-web).
// Reads the working tree only. Uses the project's own `typescript` (never a bundled copy). Opens no socket, writes no file.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

export const SNIPPET_MAX = 200;
export const SIGNATURE_MAX = 400;

export class NavError extends Error {
    constructor(status, message, next = "") {
        super(message);
        this.status = status;
        this.next = next;
    }
}

const posix = (p) => p.split(path.sep).join("/");
const real = (p) => {
    try {
        return fs.realpathSync(p);
    } catch {
        return p;
    }
};
const byPath = (a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0);

/** Nearest `node_modules/typescript` from `startDir` up to `root` (inclusive). Never above root, never a copy shipped with tmux-pi. */
export function findTypescript(root, startDir) {
    const stop = path.resolve(root);
    let dir = path.resolve(startDir);
    for (;;) {
        const pkg = path.join(dir, "node_modules", "typescript");
        if (fs.existsSync(path.join(pkg, "package.json")) && fs.existsSync(path.join(pkg, "lib", "typescript.js"))) return pkg;
        if (dir === stop || path.dirname(dir) === dir) return null;
        dir = path.dirname(dir);
    }
}

export function loadTypescript(root, tsconfigDir, explicit) {
    const pkg = explicit || findTypescript(root, tsconfigDir);
    if (!pkg) {
        throw new NavError(
            "no_typescript",
            "no node_modules/typescript found between the tsconfig folder and the project root",
            "install the project's dependencies (outside this tool), then retry",
        );
    }
    const require = createRequire(path.join(pkg, "noop.js"));
    return { ts: require(path.join(pkg, "lib", "typescript.js")), pkg };
}

function kindOf(ts, sym) {
    const F = ts.SymbolFlags;
    const f = sym.flags;
    if (f & F.Class) return "class";
    if (f & F.Function) return "function";
    if (f & F.Enum) return "enum";
    if (f & F.Interface) return "interface";
    if (f & F.TypeAlias) return "type";
    if (f & F.Variable) return "variable";
    if (f & F.Module) return "namespace";
    if (f & F.Method) return "method";
    if (f & F.Property) return "property";
    return "unknown";
}

export class NavHost {
    constructor(ts, { root, tsconfig, hooks = {} }) {
        this.ts = ts;
        this.hooks = hooks;
        this.root = fs.realpathSync(root);
        let config;
        try {
            config = fs.realpathSync(tsconfig);
        } catch {
            throw new NavError("no_project", "tsconfig not found", "pass the tsconfig.json of the package that owns the file");
        }
        this.tsconfig = config;
        this.configDir = path.dirname(config);
        if (!this.inRoot(config)) throw new NavError("refused", "tsconfig is outside the project root");
        this.stats = new Map();
        this.snapshots = new Map();
        this.edgeCache = new Map();
        this.programBuilds = 0;
        this.lastProgram = null;
        this.lastChange = Date.now();
        this.coldBuildMs = null;
        this.lastBuildMs = null;
        this.loadConfig();
        const self = this;
        this.lsHost = {
            getCompilationSettings: () => self.options,
            getScriptFileNames: () => self.fileNames,
            getScriptVersion: (f) => self.version(f),
            getScriptSnapshot: (f) => self.snapshot(f),
            getCurrentDirectory: () => self.configDir,
            getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
            fileExists: (f) => ts.sys.fileExists(f),
            readFile: (f, e) => ts.sys.readFile(f, e),
            readDirectory: (...a) => ts.sys.readDirectory(...a),
            directoryExists: (d) => ts.sys.directoryExists(d),
            getDirectories: (d) => ts.sys.getDirectories(d),
            realpath: (p) => ts.sys.realpath(p),
            useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
            getProjectReferences: () => self.projectReferences,
            useSourceOfProjectReferenceRedirect: () => true,
        };
        this.ls = ts.createLanguageService(this.lsHost, ts.createDocumentRegistry());
    }

    // ---- config / files --------------------------------------------------------------------------------------------
    parseConfig() {
        const ts = this.ts;
        const read = ts.readConfigFile(this.tsconfig, ts.sys.readFile);
        if (read.error) {
            throw new NavError("no_project", ts.flattenDiagnosticMessageText(read.error.messageText, "\n"), "fix the tsconfig");
        }
        const sys = {
            ...ts.sys,
            onUnRecoverableConfigFileDiagnostic: (d) => {
                throw new NavError("no_project", ts.flattenDiagnosticMessageText(d.messageText, "\n"), "fix the tsconfig");
            },
        };
        const parsed = ts.getParsedCommandLineOfConfigFile(this.tsconfig, {}, sys);
        if (!parsed) throw new NavError("no_project", "tsconfig could not be parsed");
        return parsed;
    }

    /** Read the tsconfig again: options, references and the files it includes. Drops the import-edge cache (options may differ). */
    loadConfig() {
        const parsed = this.parseConfig();
        this.options = { ...parsed.options, noEmit: true };
        this.fileNames = parsed.fileNames;
        this.projectReferences = parsed.projectReferences;
        this.configStamp = this.stamp(this.tsconfig);
        this.edgeCache.clear();
    }

    /** Re-list the files the tsconfig includes, for questions that span the whole program (references, importers): files made since the last look. */
    rescan() {
        this.fileNames = this.parseConfig().fileNames;
    }

    stamp(file) {
        try {
            const s = fs.statSync(file);
            return `${s.mtimeMs}:${s.size}`;
        } catch {
            return "";
        }
    }

    beginRequest() {
        this.stats.clear();
        if (this.stamp(this.tsconfig) !== this.configStamp) this.loadConfig();
    }

    /**
     * What the language service sees as a file's version: a hash of its text, re-read only when (mtime,size) moved.
     * A hash and not the mtime, because TypeScript gives a package that sits in node_modules twice one source file and
     * one version, and asks for that version under both names; two copies never share an mtime, so an mtime version
     * made every call look like a change and every call built a new Program (and a new type checker).
     */
    version(file) {
        let v = this.stats.get(file);
        if (v === undefined) {
            v = this.entry(file).version;
            this.stats.set(file, v);
        }
        return v;
    }

    entry(file) {
        const stamp = this.stamp(file);
        if (stamp === "") return { stamp, version: "", snap: undefined };
        const hit = this.snapshots.get(file);
        if (hit && hit.stamp === stamp) return hit;
        let text;
        try {
            text = fs.readFileSync(file, "utf8");
        } catch {
            return { stamp: "", version: "", snap: undefined };
        }
        const made = {
            stamp,
            version: createHash("sha1").update(text).digest("hex"),
            snap: this.ts.ScriptSnapshot.fromString(text),
        };
        this.snapshots.set(file, made);
        return made;
    }

    snapshot(file) {
        if (this.version(file) === "") return undefined;
        return this.snapshots.get(file)?.snap;
    }

    program() {
        const t0 = Date.now();
        const program = this.ls.getProgram();
        if (program !== this.lastProgram) {
            this.lastProgram = program;
            this.lastChange = Date.now();
            this.programBuilds += 1;
            this.lastBuildMs = Date.now() - t0;
            if (this.coldBuildMs === null) this.coldBuildMs = this.lastBuildMs;
        }
        return program;
    }

    inRoot(p) {
        return p === this.root || p.startsWith(this.root + path.sep);
    }

    rel(p) {
        return this.inRoot(p) ? posix(path.relative(this.root, p)) : null;
    }

    isExternal(p) {
        return p.includes(`${path.sep}node_modules${path.sep}`);
    }

    /** `file` (absolute or relative to the root) as a real path inside the root, or a NavError. */
    resolveFile(file) {
        if (typeof file !== "string" || !file) throw new NavError("bad_request", "file is required", "pass file as a path string");
        const abs = path.resolve(this.root, file);
        let found;
        try {
            found = fs.realpathSync(abs);
        } catch {
            if (!this.inRoot(abs)) throw new NavError("refused", "path is outside the project root");
            throw new NavError("not_found", "no such file", "check the path");
        }
        if (!this.inRoot(found)) throw new NavError("refused", "path is outside the project root");
        return found;
    }

    // ---- locating --------------------------------------------------------------------------------------------------
    sourceFile(file) {
        let program = this.program();
        let sf = program.getSourceFile(file);
        if (!sf) {
            // A file created after the last config read: re-read the include globs once, then decide.
            this.loadConfig();
            program = this.program();
            sf = program.getSourceFile(file);
        }
        if (!sf) {
            throw new NavError("not_in_project", "file is not part of this tsconfig's program", "ask with the tsconfig of the package that owns it");
        }
        return { program, sf };
    }

    lineOf(sf, pos) {
        const { line, character } = sf.getLineAndCharacterOfPosition(pos);
        return { line: line + 1, column: character + 1 };
    }

    lineText(sf, pos, max = SNIPPET_MAX) {
        const { line } = sf.getLineAndCharacterOfPosition(pos);
        const starts = sf.getLineStarts();
        const text = sf.text.slice(starts[line], line + 1 < starts.length ? starts[line + 1] : sf.text.length);
        return text.trim().slice(0, max);
    }

    position(sf, req) {
        const ts = this.ts;
        if (Number.isInteger(req.line) && Number.isInteger(req.column)) {
            const lines = sf.getLineStarts().length;
            if (req.line < 1 || req.line > lines || req.column < 1) throw new NavError("bad_request", "line/column outside the file", "line and column are 1-based and must lie inside the file");
            return sf.getPositionOfLineAndCharacter(req.line - 1, req.column - 1);
        }
        if (typeof req.name === "string" && req.name) {
            if (Number.isInteger(req.line)) {
                const lines = sf.getLineStarts();
                if (req.line < 1 || req.line > lines.length) throw new NavError("bad_request", "line outside the file", "line is 1-based and must lie inside the file");
                const from = lines[req.line - 1];
                const to = req.line < lines.length ? lines[req.line] : sf.text.length;
                const escaped = req.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                const m = new RegExp(`(?<![\\w$])${escaped}(?![\\w$])`).exec(sf.text.slice(from, to));
                if (!m) throw new NavError("not_found", "name not on that line", "check line and name");
                return from + m.index;
            }
            let declared = null;
            let first = null;
            const visit = (node) => {
                if (declared !== null) return;
                if (ts.isIdentifier(node) && node.text === req.name) {
                    if (first === null) first = node.getStart(sf);
                    if (node.parent && node.parent.name === node) {
                        declared = node.getStart(sf);
                        return;
                    }
                }
                ts.forEachChild(node, visit);
            };
            visit(sf);
            const pos = declared ?? first;
            if (pos === null) throw new NavError("not_found", "name not found in file", "check the name, or pass line and column");
            return pos;
        }
        throw new NavError("bad_request", "pass line and column, or name (with or without line)", "e.g. {file, line, column} or {file, name}");
    }

    /** A location inside the root as {path, line, column, external}, or null when it is outside the root. `_sf` is for the caller. */
    loc(program, fileName, start) {
        const found = real(fileName);
        const rel = this.rel(found);
        if (rel === null) return null;
        const sf = program.getSourceFile(fileName) ?? program.getSourceFile(found);
        if (!sf) return { path: rel, line: null, column: null, external: this.isExternal(found) };
        const { line, column } = this.lineOf(sf, start);
        return { path: rel, line, column, external: this.isExternal(found), _sf: sf };
    }

    // ---- operations ------------------------------------------------------------------------------------------------
    run(req) {
        this.beginRequest();
        const op = req.op;
        if (op === "stats") return this.op_stats();
        if (!["definition", "references", "exports", "imports"].includes(op)) {
            throw new NavError("bad_request", `unknown op ${JSON.stringify(op)}`, "use definition, references, exports, imports or stats");
        }
        const file = this.resolveFile(req.file);
        const before = this.stamp(file);
        const out = this[`op_${op}`](req, file);
        if (this.hooks.beforeVerify) this.hooks.beforeVerify(op, file);
        if (this.stamp(file) !== before) {
            throw new NavError("stale_index", "the file changed while the answer was being computed", "repeat the call");
        }
        out.index_age_s = Math.round((Date.now() - this.lastChange) / 1000);
        return out;
    }

    op_definition(req, file) {
        const { program, sf } = this.sourceFile(file);
        const pos = this.position(sf, req);
        const defs = this.ls.getDefinitionAtPosition(file, pos) ?? [];
        const rows = [];
        let outside = 0;
        for (const d of defs) {
            const l = this.loc(program, d.fileName, d.textSpan.start);
            if (!l) {
                outside += 1;
                continue;
            }
            const dsf = l._sf;
            delete l._sf;
            rows.push({ ...l, name: d.name, kind: d.kind, container: d.containerName && !d.containerName.startsWith('"') ? d.containerName : "", text: dsf ? this.lineText(dsf, (d.contextSpan ?? d.textSpan).start, 160) : "" });
        }
        rows.sort((a, b) => byPath(a, b) || (a.line ?? 0) - (b.line ?? 0));
        let signature = "";
        let doc = "";
        const info = this.ls.getQuickInfoAtPosition(file, pos);
        if (info) {
            signature = this.ts.displayPartsToString(info.displayParts).slice(0, SIGNATURE_MAX);
            doc = this.ts.displayPartsToString(info.documentation).slice(0, SIGNATURE_MAX);
        }
        return { status: rows.length ? "ok" : "not_found", at: this.lineOf(sf, pos), definitions: rows, signature, doc, omitted_outside_root: outside };
    }

    op_references(req, file) {
        this.rescan();
        const { program, sf } = this.sourceFile(file);
        const pos = this.position(sf, req);
        const limit = Math.max(1, Math.min(Number.isInteger(req.limit) ? req.limit : 25, 200));
        const groups = this.ls.findReferences(file, pos) ?? [];
        const seen = new Set();
        const rows = [];
        const defs = [];
        let outside = 0;
        let external = 0;
        for (const g of groups) {
            const d = g.definition;
            const dl = this.loc(program, d.fileName, d.textSpan.start);
            if (dl) {
                const dsf = dl._sf;
                delete dl._sf;
                const name = dsf ? dsf.text.slice(d.textSpan.start, d.textSpan.start + d.textSpan.length) : "";
                defs.push({ ...dl, name, kind: d.kind });
            } else {
                outside += 1;
            }
            for (const r of g.references) {
                const l = this.loc(program, r.fileName, r.textSpan.start);
                if (!l) {
                    outside += 1;
                    continue;
                }
                const rsf = l._sf;
                delete l._sf;
                if (l.external) {
                    external += 1;
                    continue;
                }
                const key = `${l.path}:${r.textSpan.start}`;
                if (seen.has(key)) continue;
                seen.add(key);
                rows.push({ ...l, text: rsf ? this.lineText(rsf, r.textSpan.start) : "", is_definition: !!r.isDefinition, is_write: !!r.isWriteAccess });
            }
        }
        rows.sort((a, b) => byPath(a, b) || a.line - b.line || a.column - b.column);
        defs.sort((a, b) => byPath(a, b) || (a.line ?? 0) - (b.line ?? 0));
        return {
            status: rows.length || defs.length ? "ok" : "not_found",
            at: this.lineOf(sf, pos),
            definitions: defs,
            references: rows.slice(0, limit),
            total: rows.length,
            truncated: rows.length > limit,
            omitted_outside_root: outside,
            omitted_external: external,
        };
    }

    op_exports(req, file) {
        const ts = this.ts;
        const { program, sf } = this.sourceFile(file);
        const limit = Math.max(1, Math.min(Number.isInteger(req.limit) ? req.limit : 200, 500));
        const checker = program.getTypeChecker();
        const mod = checker.getSymbolAtLocation(sf);
        if (!mod) return { status: "ok", is_module: false, exports: [], total: 0, truncated: false };
        const rows = [];
        for (const sym of checker.getExportsOfModule(mod)) {
            const decl = sym.declarations?.[0];
            const here = decl && decl.getSourceFile() === sf;
            let target = sym;
            if (sym.flags & ts.SymbolFlags.Alias) {
                try {
                    target = checker.getAliasedSymbol(sym);
                } catch {
                    target = sym;
                }
            }
            const tdecl = target.declarations?.[0];
            const there = tdecl && tdecl.getSourceFile() !== sf;
            let definedAt = null;
            if (there) {
                const l = this.loc(program, tdecl.getSourceFile().fileName, tdecl.getStart());
                if (l) {
                    delete l._sf;
                    definedAt = l.line ? `${l.path}:${l.line}` : l.path;
                }
            }
            rows.push({
                name: sym.name,
                kind: kindOf(ts, target),
                line: here ? this.lineOf(sf, decl.getStart()).line : null,
                reexport: !here || !!there,
                defined_at: definedAt,
            });
        }
        rows.sort((a, b) => (a.line ?? 1e9) - (b.line ?? 1e9) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
        return { status: "ok", is_module: true, exports: rows.slice(0, limit), total: rows.length, truncated: rows.length > limit };
    }

    resolver() {
        const ts = this.ts;
        const cache = ts.createModuleResolutionCache(this.configDir, (f) => f, this.options);
        const host = {
            fileExists: (f) => ts.sys.fileExists(f),
            readFile: (f) => ts.sys.readFile(f),
            directoryExists: (d) => ts.sys.directoryExists(d),
            getDirectories: (d) => ts.sys.getDirectories(d),
            realpath: (p) => ts.sys.realpath(p),
            getCurrentDirectory: () => this.configDir,
            useCaseSensitiveFileNames: ts.sys.useCaseSensitiveFileNames,
        };
        return (spec, containing) => {
            const r = ts.resolveModuleName(spec, containing, this.options, host, cache).resolvedModule;
            return r ? real(r.resolvedFileName) : null;
        };
    }

    /** Import edges of one file, cached by the file's (mtime,size). Unresolved edges are retried on every call. */
    edgesOf(file, sf, resolve) {
        const v = this.version(file);
        const hit = this.edgeCache.get(file);
        if (hit && hit.version === v) {
            // A target that was deleted, or a specifier that did not resolve before, is resolved again.
            for (const e of hit.edges) if (e.resolved === null || !fs.existsSync(e.resolved)) e.resolved = resolve(e.specifier, file);
            return hit.edges;
        }
        const pre = this.ts.preProcessFile(sf.text, true, true);
        const edges = pre.importedFiles.map((imp) => ({
            specifier: imp.fileName,
            line: this.lineOf(sf, imp.pos).line,
            resolved: resolve(imp.fileName, file),
        }));
        this.edgeCache.set(file, { version: v, edges });
        return edges;
    }

    op_imports(req, file) {
        const direction = req.direction ?? "imports";
        if (direction !== "imports" && direction !== "importers") throw new NavError("bad_request", "direction must be imports or importers", 'use direction "imports" or "importers"');
        const limit = Math.max(1, Math.min(Number.isInteger(req.limit) ? req.limit : 25, 200));
        if (direction === "importers") this.rescan();
        const { program, sf } = this.sourceFile(file);
        const resolve = this.resolver();
        const target = this.rel(file);
        if (direction === "imports") {
            const rows = this.edgesOf(file, sf, resolve).map((e) => {
                const rel = e.resolved === null ? null : this.rel(e.resolved);
                return {
                    specifier: e.specifier,
                    line: e.line,
                    resolved: rel,
                    external: e.resolved === null ? !e.specifier.startsWith(".") : this.isExternal(e.resolved) || rel === null,
                };
            });
            rows.sort((a, b) => a.line - b.line || (a.specifier < b.specifier ? -1 : a.specifier > b.specifier ? 1 : 0));
            return {
                status: "ok",
                direction,
                target,
                edges: rows.slice(0, limit),
                total: rows.length,
                truncated: rows.length > limit,
                unresolved: rows.filter((r) => r.resolved === null).length,
            };
        }
        const depth = Math.max(1, Math.min(Number.isInteger(req.depth) ? req.depth : 1, 2));
        const byTarget = new Map();
        let unresolved = 0;
        for (const f of program.getSourceFiles()) {
            const found = real(f.fileName);
            if (!this.inRoot(found) || this.isExternal(found)) continue;
            for (const e of this.edgesOf(found, f, resolve)) {
                if (e.resolved === null) {
                    unresolved += 1;
                    continue;
                }
                if (!byTarget.has(e.resolved)) byTarget.set(e.resolved, []);
                byTarget.get(e.resolved).push({ from: found, line: e.line, specifier: e.specifier });
            }
        }
        const rows = [];
        let frontier = [file];
        const done = new Set([file]);
        for (let d = 1; d <= depth; d += 1) {
            const next = [];
            for (const t of frontier) {
                for (const e of byTarget.get(t) ?? []) {
                    rows.push({ from: this.rel(e.from), line: e.line, specifier: e.specifier, to: this.rel(t), depth: d });
                    if (!done.has(e.from)) {
                        done.add(e.from);
                        next.push(e.from);
                    }
                }
            }
            frontier = next;
        }
        rows.sort((a, b) => a.depth - b.depth || (a.from < b.from ? -1 : a.from > b.from ? 1 : a.line - b.line));
        return { status: "ok", direction, target, edges: rows.slice(0, limit), total: rows.length, truncated: rows.length > limit, unresolved };
    }

    op_stats() {
        const program = this.program();
        const mem = process.memoryUsage();
        return {
            status: "ok",
            typescript: this.ts.version,
            tsconfig: this.rel(this.tsconfig),
            root_files: this.fileNames.length,
            program_files: program.getSourceFiles().length,
            program_builds: this.programBuilds,
            cold_build_ms: this.coldBuildMs,
            last_build_ms: this.lastBuildMs,
            import_edges_files: this.edgeCache.size,
            rss_mb: Math.round(mem.rss / 1048576),
            heap_mb: Math.round(mem.heapUsed / 1048576),
            index_age_s: Math.round((Date.now() - this.lastChange) / 1000),
        };
    }
}
