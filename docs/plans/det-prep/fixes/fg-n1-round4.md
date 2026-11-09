# FG-N1 round 4 (design change): what a wrapper hands on is scanned whole; exact parsing may only narrow

Same lane `fg-wrappers`, step N1, same touches. Review: tasks/review-coder-b9c961eade-185926 (F1-F4).
Rounds 1-3 emulated each wrapper's options and each review found a wrapper or shape the emulation got wrong
(families stacked-wrapper-strings, remote-command-uncertainty). This round changes the rule so a wrong option table
can no longer hide anything.

## Rule
1. **Bag-of-words floor for wrapper tails.** When a command starts with a wrapper (anything in the table, at any
   depth), take every word after the first wrapper word, and every word of every string a wrapper or a shell
   `-c`-style option hands on (split like the shell, recursively to the depth limit). If any of those words is a
   search or read program the detectors know (the same program sets they use for a plain command), the line is a
   search (redact_hook) and is judged by the read side as that program over the rest of the words. This floor
   applies whatever the option table says. The table may still be used to find the exact program and arguments for a
   more precise read-side judgement, but it can only add sensitivity, never remove the floor's.
2. **Remote and handed-on text: uncertain means sensitive.** Inside wrapper-supplied text (ssh remote line, `su -c`,
   `flock -c`, `script -c`, `sg`, `runuser -c`, `env -S`, nested shell strings), any of the following makes the read
   side sensitive: an unresolved expansion (`$X`, `${X}`, `$(...)`, backticks); a shell with no command string
   (`ssh host sh`, `bash` reading stdin); a shell with any option cluster before its string (`-cx`, `-xc`, `-ec`);
   an exec-style dispatcher (`find -exec`/`-execdir`/`-ok`, `xargs`, `docker exec`/`run`, `kubectl exec`,
   `nsenter`, `chroot`, `parallel`, `watch`, `eval`, `exec`, `command`, `builtin`). No allow-list of shapes.
3. **Concrete fixes the floor must cover (tests):** F1 `sudo -h localhost rg --hidden TOKEN .` and
   `script -F out.txt rg TOKEN --hidden .`; F2 `env -S 'sudo -n' rg --hidden TOKEN .`; F3's five lines verbatim.
4. **False positives stay controlled.** Main's 26-command probe (main-evidence/fp-probe.py of coder-b9c961eade) must
   give identical output to main, and add 15 more ordinary wrapper commands of your own whose tails hold no search or
   read program and no dispatcher (e.g. `sudo -u nobody make`, `timeout 30 npm test`, `nice -n 5 cargo build`,
   `ssh host uptime`, `env -i PATH=/usr/bin make`): each must be judged as on main. List all 41 with both results.
5. **Generated test, full product, stated explicitly (Main's amendment to round 3 item 4):** depth 1 and 2: the full
   product of every wrapper x option form (none, flag, valued, `--`, unknown option) x payload (direct, each string
   form, nested `sh -c`, `sh -cx`, unresolved `$VAR`, exec dispatcher). Depth 3: the full product of every wrapper
   triple with option form none and payloads direct and nested `sh -c`. No rotation or sampling; if the suite would
   exceed 20 s, move the generated class to `tests/test_wrapper_product.py` and state its time. Every read assertion
   enabled.

## Verification and claims
The same 12 modules as round 3, both interpreters, both modes. kill_guard and network-ask PREFIXES stay out of scope.
