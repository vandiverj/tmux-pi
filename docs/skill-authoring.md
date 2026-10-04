# Authoring and validating a tmux-pi skill

1. Copy `templates/skill-template/` (not auto-discovered) to `catalog/skills/<id>/`; rename the directory and `name:` together.
2. Edit the description (what it does and when to use it; <=1024 chars, aim for <=300) and body.
3. Validate: `bin/tp-skill-check catalog/skills/<id> --single-file`
4. Register it in `config/skill-catalog.json` with narrow `markers`/`phrases`, and add one positive and one negative selection case to `tests/test_skill_catalog.py`.

In a tmux-pi session, a `coder` worker can draft the skill and its trigger cases; main runs `tp-skill-check` and checks the selected packet, then asks an independent reviewer when scripts, external data, or authority boundaries warrant one. Skills grant no tool authority. Only put a skill in `skills/` if every Claude session should see it.

This checker enforces a conservative local subset of the [Agent Skills specification](https://agentskills.io/specification); one-hop references and a contents heading on references over 100 lines are *house rules informed by* [Anthropic](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) and [OpenAI](https://github.com/openai/skills/tree/main/skills/.system/skill-creator), not loader truncation rules. For a skill being distributed to other clients, also run that client's own validator (for example `skills-ref validate <dir>` or OpenAI skill-creator's `quick_validate.py`) and inspect its bundled scripts before enabling it.

## Local policy enforced by `tp-skill-check`

- Frontmatter: flat single-line `key: value`; keys `name`, `description`, optional `license`. `allowed-tools`, `hooks` and other keys are rejected. No block scalars, lists or nested maps (limitation of the stdlib parser; `metadata` is unsupported).
- `name`: directory name, <=64 chars, lowercase letters/digits, single hyphens.
- Links to local files must be relative, inside the skill directory, symlink-free, and exist. Symlinks anywhere in the skill fail.
- Unquoted values may not contain `: ` or ` #`; quote them (single quotes, `''` escapes; double quotes without backslashes). Trailing comments are rejected.
- Limitation: TOC/one-hop checks apply only to `.md` files linked from SKILL.md (inline, image, `[x]: path`, `href`/`src` links; code fences and external URLs are ignored). Unlinked files and bare paths in prose are not inspected.
- References are one hop: a reference file may not link to further local files. References over 100 lines need a `Contents` heading in their first 30 lines (a local convention, not a vendor requirement).
- `--single-file`: the catalogue packet inlines only `SKILL.md`, so any local link fails. Prefer this for catalogue skills.
- `--legacy`: checks the existing frontmatter-less catalogue skills (size cap only). The checker never runs on the catalogue implicitly.

## Selector smoke check

    echo "/<id> sample request" | bin/tp-skills --mode select      # expect the id
    echo "Fix a TypeScript validation bug" | bin/tp-skills --mode select   # expect []
