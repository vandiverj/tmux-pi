# Authoring and validating a tmux-pi skill

1. Copy `templates/skill-template/` (not auto-discovered) to `catalog/skills/<id>/`; rename the directory and `name:` together.
2. Edit the description (what it does and when to use it; <=1024 chars, aim for <=300) and body.
3. Validate a new skill: `bin/tp-skill-check catalog/skills/<id> --single-file`. The existing catalogue skills (`counterpoint`, `document-transform`, `product-fit`, `test-skill-behavior`, `threat-model`) predate frontmatter and fail that mode; check them with `--legacy` instead. They are not being migrated: the selector reads `description` from the JSON catalogue, so frontmatter has no runtime effect there.
4. Register it in `config/skill-catalog.json` with narrow `markers`/`phrases`, and add one positive and one negative selection case to `tests/test_skill_catalog.py`.

In a tmux-pi session, a `coder` worker can draft the skill and its trigger cases; main runs `tp-skill-check` and checks the selected packet, then asks an independent reviewer when scripts, external data, or authority boundaries warrant one. Skills grant no tool authority. Only put a skill in `skills/` if every Claude session should see it (see [Delivery](#delivery)).

This checker enforces a conservative local subset of the [Agent Skills specification](https://agentskills.io/specification); one-hop references and a contents heading on references over 100 lines are *house rules informed by* [Anthropic](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) and [OpenAI](https://github.com/openai/skills/tree/main/skills/.system/skill-creator), not loader truncation rules. For a skill being distributed to other clients, also run that client's own validator (for example `skills-ref validate <dir>` or OpenAI skill-creator's `quick_validate.py`) and inspect its bundled scripts before enabling it.

## Local policy enforced by `tp-skill-check`

- Frontmatter: flat single-line `key: value`; keys `name`, `description`, optional `license`. `allowed-tools`, `hooks` and other keys are rejected. No block scalars, lists or nested maps (limitation of the stdlib parser; `metadata` is unsupported).
- `name`: directory name, <=64 chars, lowercase letters/digits, single hyphens.
- Links to local files must be relative, inside the skill directory, symlink-free, and exist. Symlinks anywhere in the skill fail.
- Unquoted values may not contain `: ` or ` #`; quote them (single quotes, `''` escapes; double quotes without backslashes). Trailing comments are rejected.
- Limitation: TOC/one-hop checks apply only to `.md` files linked from SKILL.md (inline, image, `[x]: path`, `href`/`src` links; code fences and external URLs are ignored). Unlinked files and bare paths in prose are not inspected.
- References are one hop: a reference file may not link to further local files. References over 100 lines need a `Contents` heading in their first 30 lines (a local convention, not a vendor requirement).
- `--single-file`: the catalogue packet inlines only `SKILL.md`, so any local link fails. Prefer this for catalogue skills.
- `--legacy`: checks the existing frontmatter-less catalogue skills (size cap only); use it for those instead of `--single-file`. The checker never runs on the catalogue implicitly.

## Selection rules

`bin/tp-skills` (backed by `lib/skill_catalog.py`) is the only selector; Pi main and `tp-spawn` worker packets both call it.

- `markers` are explicit `/commands`, matched case-insensitively. A marker counts when it starts a token, optionally after an opening bracket or quote, and may be followed by punctuation: `/make-qa,`, `Please run /threat-model.` and `(/threat-model)` all select. A marker continued as a path, longer name or filename does not: `src/make-qa/notes`, `http://host/threat-model`, `/threat-model.md`, `/counterpoint-v2`.
- `phrases` match whole words anywhere in the request, including a worker task that only mentions them. Prefer a deliberate request form (`conduct a security review`, `draft a contract spec`, `adversarial pass on this plan`) over a bare noun phrase (`security review`, `contract spec`, `adversarial pass`) that also appears in ordinary prose. The explicit marker always remains available.
- At most `max_selected` (3) skills are returned. Explicit markers rank ahead of phrase/file matches; ties fall back to catalogue order, not request order, so a fourth explicit marker is dropped.
- `status` is informational. It is copied into the selection and packet heading for the reader and for provenance; nothing gates on it, so an `experimental` skill is selected and injected exactly like a `pilot` one. Limit an experimental skill's reach with narrow `markers`/`phrases`, not with its status text.

## Delivery

- Catalogue skills (`catalog/skills/`) reach a model only when selected: Pi main gets a turn-local system section (Pi runs with `--no-skills`), and Pi or Claude workers get the body inlined in the task packet.
- `skills/` is symlinked into the Claude plugin (`claude/plugins/tmux-pi/skills`), so anything there is a native Claude skill whose name and description are listed in every `tp-claude` session and worker.
- `make-qa` is deliberately both. It stays in `skills/` because that is what provides `/tmux-pi:make-qa` on Claude, and it stays in the catalogue because that is the only route to Pi and to worker packets. The body is therefore not always loaded twice: Pi only ever sees the catalogue copy, and a Claude session that is not doing QA work sees just the plugin's name and description. Only a Claude worker whose task matches gets the body in its packet while the plugin skill is also loadable through the Skill tool; whether it loads it again is up to the model. Do not copy this pattern for a new skill without the same need for a Claude slash command.

## Selector smoke check

    echo "/<id> sample request" | bin/tp-skills --mode select      # expect the id
    echo "Please run /<id>." | bin/tp-skills --mode select         # expect the id
    echo "Fix a TypeScript validation bug" | bin/tp-skills --mode select   # expect []
