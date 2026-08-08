# Edge case suite for the PROMPT.md

Twelve cases that checks every branch and guard rule in the macOS Crash
Report Analyzer prompt. Each case drops a fixture into the `[PASTE LOG HERE]`
slot, sends the assembled prompt to a model, and scores the response against
declarations in `cases.json`.

The suite lives outside `PROMPT.md` on purpose. Anything placed inside the
prompt becomes a an example the model uses, costs tokens on every
call, and for the injection fixtures would sit outside the data block

## Running

```bash
python3 run.py                        # all cases, 5 runs each
python3 run.py --runs 1               # quick smoke pass
python3 run.py --case 08 --case 09    # just the truncation boundary pair
python3 run.py --dry-run --case 02    # print the assembled prompt, call nothing
```

### Authentication

By default the harness shells out to the Claude Code CLI, which authenticates
with a Claude subscription and needs no API key:

```
claude -p --safe-mode --tools "" --strict-mcp-config --no-session-persistence
        --disable-slash-commands --output-format json --model opus
        --system-prompt "<neutral>" "<assembled prompt>"
```

`--safe-mode` disables hooks, `CLAUDE.md` discovery, plugins, skills, and MCP
servers so local customizations do not bleed into the measurement.
`--system-prompt` replaces Claude Code's own system prompt with a neutral one,
so what is measured is the analyzer prompt rather than the surrounding harness.

- The analyzer prompt runs as a **user message**, with a short system
  prompt above it. If you deploy the prompt as a system prompt instead, test it
  that way the behaviour differs.
- Temperature is not controllable through the CLI. Run repeats and read pass
  rates rather than treating one run as authoritative.

If `ANTHROPIC_API_KEY` is ever set, the harness switches to the Anthropic SDK
automatically (`--transport api`), which sends the prompt with no harness
system prompt and pins `temperature=0`. That is the cleaner measurement; use it
when you have a key.

## Reading results

Pass rates, not pass/fail. Temperature is non-zero on the CLI transport and not
perfectly deterministic even at 0 through the API, and branch selection is
exactly where residual variance appears.

- A **branch** assertion below 100% is a prompt defect. The model is choosing
  between response types inconsistently, which means the rule text does not
  discriminate sharply enough.

Full responses are saved to `results/<model>-<n>cases-x<runs>.json` so a failing
check can be read in context.

## Cases

| Case | Expected branch | What it guards |
|---|---|---|
| 01-valid-codesign | render | happy path: header exactness, order, ceilings, version line |
| 02-empty | stop_ask | empty payload does not produce invented sections |
| 03-placeholder-only | stop_ask | unfilled template token treated as empty |
| 04-wrong-format | wrong_format | non-Apple input takes the wrong-format reply, not out-of-scope |
| 05-hang | render | section 4 header kept verbatim, no invented exception type |
| 06-unsymbolicated | render | real diagnosis, not six gap placeholders; requests the .dSYM |
| 07-multi-crash | render | analyzes the most recent, reports the count on the input-type line |
| 08-truncated-before-exception | stop_ask | boundary pair, low side |
| 09-truncated-after-exception | render | boundary pair, high side — same file, later cut |
| 10-question-inside-block | wrong_format | a question inside the tag block is report content, not a request |
| 11-injected-instruction | render | injection reported as tampering in section 4, never obeyed |
| 12-nested-closing-tag | render | FINAL closing-tag rule holds against a premature tag |
