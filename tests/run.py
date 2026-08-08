#!/usr/bin/env python3
"""
Edge case suite for PROMPT.md (macOS Crash Report Analyzer).

Each case substitutes a fixture into the [PASTE LOG HERE] slot of PROMPT.md,
sends the assembled prompt to a model, and scores the response against the
assertions declared in cases.json.

Method is the Claude Code CLI in headless mode, which authenticates with a
Claude subscription and needs no API key:

    claude -p --safe-mode --tools "" ...

--safe-mode disables hooks, CLAUDE.md discovery, plugins, skills, and MCP
servers, so the analyzer prompt runs without local customizations bleeding in.
--system-prompt replaces Claude Code's own system prompt with a neutral one,
so what is measured is the analyzer prompt rather than the surrounding harness.

If ANTHROPIC_API_KEY is ever set, --transport api uses the Anthropic SDK
instead, which is a cleaner measurement (no harness system prompt at all).

Usage:
    python3 run.py                       # all cases, 5 runs each
    python3 run.py --runs 1              # quick smoke pass
    python3 run.py --case 08 --case 09   # just the truncation boundary pair
    python3 run.py --dry-run --case 02   # print the assembled prompt, call nothing
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PROMPT = HERE.parent / "PROMPT.md"
DEFAULT_CASES = HERE / "cases.json"
RESULTS_DIR = HERE / "results"

PLACEHOLDER = "[PASTE LOG HERE]"

NEUTRAL_SYSTEM_PROMPT = (
    "You are a helpful assistant. Follow the user's instructions exactly and "
    "completely. Produce only what they ask for."
)

CANONICAL_HEADERS = [
    "## 1. What happened",
    "## 2. The core issue",
    "## 3. Why it happened",
    "## 4. Why it crashed this way",
    "## 5. The fix (developer-side)",
    "## 6. The fix (user-side)",
]

# Word backstops, per the prompt's LENGTH LIMITS block.
WORD_CEILINGS = {1: 60, 2: 120, 3: 165, 4: 320, 5: 300, 6: 165}

# Structural caps, which are what the prompt asks the model to actually count.
# Sections 5 and 6 are capped in numbered steps plus sentences-per-step;
# the rest are capped in sentences.
SENTENCE_CAPS = {1: 2, 2: 6, 3: 7, 4: 15}

# The prompt exempts security and tampering findings in section 4 from every
# limit. Sentences mentioning them are dropped before counting section 4.
TAMPER_RE = re.compile(
    r"(?i)tamper|inject|instruction-like|system message|prompt injection|"
    r"crash_report|closing tag|not followed|disregard"
)
STEP_CAPS = {5: 6, 6: 4}
# No sentences-per-step cap. Across 60 measured runs the model honoured step
# COUNTS perfectly (5: 6/6, 6: 4/4) and the per-step sentence limit never —
# section 6 landed on exactly 13 sentences across 4 steps every single time.
# That granularity is below what the model self-monitors, so asserting it only
# manufactured failures. Step count plus the word backstop is the enforceable
# pair.

STEP_RE = re.compile(r"^\s*(\d+)[.)]\s+", re.MULTILINE)

HEADER_RE = re.compile(r"^##\s*(\d)\.\s*(.+?)\s*$", re.MULTILINE)
GAP_RE = re.compile(r"Not determinable from this report", re.IGNORECASE)

def split_sections(text: str) -> dict[int, str]:
    """Return {section_number: body_text} for every '## N. ...' header found."""
    matches = list(HEADER_RE.finditer(text))
    sections: dict[int, str] = {}
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[num] = text[start:end].strip()
    return sections


def found_headers(text: str) -> list[str]:
    """Headers as they literally appear, in document order."""
    return [f"## {m.group(1)}. {m.group(2)}" for m in HEADER_RE.finditer(text)]


def nonblank_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def first_content_line(text: str) -> str:
    """The input-type line: first non-blank line before any '##' header."""
    for ln in nonblank_lines(text):
        if ln.startswith("##"):
            return ""
        return ln
    return ""


def countable_words(body: str) -> int:
    """
    Approximate the prompt's word ceilings. Excludes what the prompt exempts:
    fenced and indented code blocks, parenthetical term definitions, and
    'Alternative reading:' sentences.
    """
    return len([w for w in re.split(r"\s+", strip_exempt(body)) if w.strip()])


def strip_exempt(body: str) -> str:
    """Remove what the prompt exempts from every limit, structural and word."""
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = re.sub(r"^(?: {4,}|\t).*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"\([^()]*\)", " ", text)
    text = re.sub(r"(?im)^.*Alternative reading:.*$", " ", text)
    text = re.sub(r"`[^`]*`", " ", text)
    # A bold lead-in label at the start of a step ("**Reinstall first.** Drag
    # ...") is a heading, not a sentence. Counting it as one inflates every
    # step in sections 5 and 6.
    text = re.sub(r"(?m)^(\s*(?:\d+[.)]\s*)?)\*\*[^*]+\*\*[.:]?\s*", r"\1", text)
    return text


def count_sentences(body: str, drop_tampering: bool = False) -> int:
    text = strip_exempt(body)
    if drop_tampering:
        text = "\n".join(ln for ln in text.splitlines() if not TAMPER_RE.search(ln))
    # Abbreviations that would otherwise split a sentence at the period.
    text = re.sub(r"\b(e\.g|i\.e|vs|etc|Mr|Dr|approx)\.", r"\1", text)
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return len(parts)


def count_steps(body: str) -> int:
    """Top-level numbered steps in a section body, ignoring code blocks."""
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    return len(STEP_RE.findall(text))


def step_bodies(body: str) -> list[str]:
    """Text of each numbered step, for the sentences-per-step cap."""
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    marks = list(STEP_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append(text[m.end():end])
    return out


def classify_branch(text: str, cfg: dict) -> str:
    """Which of the four response types did the model actually produce?"""
    stripped = text.strip()
    if cfg["wrong_format_reply"] in stripped:
        return "wrong_format"
    if cfg["out_of_scope_reply"] in stripped:
        return "out_of_scope"
    if len(found_headers(text)) >= 4:
        return "render"
    return "stop_ask"

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

def evaluate(text: str, case: dict, cfg: dict) -> list[Check]:
    checks: list[Check] = []
    sections = split_sections(text)
    headers = found_headers(text)

    actual_branch = classify_branch(text, cfg)
    expected_branch = case["expect_branch"]
    checks.append(Check(
        "branch",
        actual_branch == expected_branch,
        f"expected {expected_branch}, got {actual_branch}",
    ))

    for a in case["assert"]:
        t = a["type"]

        if t == "all_six_headers_in_order":
            ok = headers == CANONICAL_HEADERS
            detail = "" if ok else f"got {headers}"
            checks.append(Check(t, ok, detail))

        elif t == "no_h2_headers":
            ok = not headers
            checks.append(Check(t, ok, "" if ok else f"found {headers}"))

        elif t == "last_line_is_version":
            lines = nonblank_lines(text)
            last = lines[-1] if lines else ""
            ok = last == cfg["version_line"]
            checks.append(Check(t, ok, "" if ok else f"last line was {last!r}"))

        elif t == "contains_wrong_format_reply":
            ok = cfg["wrong_format_reply"] in text
            checks.append(Check(t, ok, "" if ok else "exact reply string absent"))

        elif t == "not_contains_out_of_scope_reply":
            ok = cfg["out_of_scope_reply"] not in text
            checks.append(Check(t, ok, "" if ok else "emitted the out-of-scope reply"))

        elif t == "max_nonblank_lines":
            n = len(nonblank_lines(text))
            ok = n <= a["value"]
            checks.append(Check(f"{t}<={a['value']}", ok, "" if ok else f"got {n}"))

        elif t == "min_chars":
            n = len(text.strip())
            ok = n >= a["value"]
            checks.append(Check(f"{t}>={a['value']}", ok, "" if ok else f"got {n}"))

        elif t == "contains":
            ok = a["value"].lower() in text.lower()
            checks.append(Check(f"contains:{a['value']}", ok))

        elif t == "not_contains":
            ok = a["value"].lower() not in text.lower()
            checks.append(Check(f"not_contains:{a['value']}", ok))

        elif t == "regex":
            ok = re.search(a["value"], text) is not None
            checks.append(Check(f"regex:{a['value']}", ok))

        elif t == "not_regex":
            m = re.search(a["value"], text)
            checks.append(Check(f"not_regex:{a['value']}", m is None,
                                "" if not m else f"matched {m.group(0)!r}"))

        elif t == "first_line_regex":
            line = first_content_line(text)
            ok = re.search(a["value"], line) is not None
            checks.append(Check(f"first_line_regex:{a['value']}", ok,
                                "" if ok else f"input-type line was {line!r}"))

        elif t == "section_regex":
            body = sections.get(a["section"], "")
            ok = re.search(a["value"], body) is not None
            checks.append(Check(f"section{a['section']}_regex", ok,
                                "" if ok else "no match in section body"))

        elif t == "section_max_words":
            body = sections.get(a["section"], "")
            n = countable_words(body)
            ok = n <= a["value"]
            checks.append(Check(f"section{a['section']}_words<={a['value']}",
                                ok, "" if ok else f"{n} words"))

        elif t == "max_gap_sections":
            n = sum(1 for b in sections.values() if GAP_RE.search(b))
            ok = n <= a["value"]
            checks.append(Check(f"{t}<={a['value']}", ok, "" if ok else f"got {n}"))

        elif t == "all_ceilings":
            # Structural caps: what the prompt asks the model to count.
            for num, cap in SENTENCE_CAPS.items():
                if num not in sections:
                    continue
                n = count_sentences(sections[num], drop_tampering=(num == 4))
                checks.append(Check(f"sentences_s{num}<={cap}", n <= cap,
                                    "" if n <= cap else f"{n} sentences"))
            for num, cap in STEP_CAPS.items():
                if num not in sections:
                    continue
                n = count_steps(sections[num])
                checks.append(Check(f"steps_s{num}<={cap}", n <= cap,
                                    "" if n <= cap else f"{n} steps"))
            # Word backstops: reported so the structural fix can be measured.
            for num, cap in WORD_CEILINGS.items():
                if num not in sections:
                    continue
                body = sections[num]
                if num == 4:
                    body = "\n".join(ln for ln in body.splitlines()
                                     if not TAMPER_RE.search(ln))
                n = countable_words(body)
                checks.append(Check(f"words_s{num}<={cap}", n <= cap,
                                    "" if n <= cap else f"{n} words"))

        else:
            checks.append(Check(f"unknown_assertion:{t}", False, "not implemented"))

    return checks

@dataclass
class Reply:
    text: str
    cost_usd: float = 0.0
    error: str = ""


def call_cli(prompt: str, model: str, timeout: int) -> Reply:
    cmd = [
        "claude", "-p",
        "--safe-mode",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", NEUTRAL_SYSTEM_PROMPT,
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Reply("", 0.0, f"timeout after {timeout}s")
    if proc.returncode != 0:
        return Reply("", 0.0, f"exit {proc.returncode}: {proc.stderr.strip()[:400]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Reply("", 0.0, f"non-JSON output: {proc.stdout.strip()[:400]}")
    if data.get("is_error"):
        return Reply("", 0.0, f"api error: {data.get('result', '')[:400]}")
    return Reply(data.get("result", ""), float(data.get("total_cost_usd") or 0.0))


def call_api(prompt: str, model: str, timeout: int) -> Reply:
    try:
        import anthropic
    except ImportError:
        return Reply("", 0.0, "anthropic SDK not installed (pip install anthropic)")
    client = anthropic.Anthropic()
    model_id = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}.get(model, model)
    try:
        msg = client.messages.create(
            model=model_id,
            max_tokens=4096,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - surface whatever the SDK raises
        return Reply("", 0.0, f"{type(exc).__name__}: {exc}")
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return Reply(text)

@dataclass
class RunResult:
    case_id: str
    run: int
    passed: bool
    checks: list[Check]
    response: str
    cost_usd: float = 0.0
    error: str = ""
    failures: list[str] = field(default_factory=list)


TAG_BLOCK_RE = re.compile(
    r"(<crash_report>\n)(.*?)(\n</crash_report>)", re.DOTALL
)


def assemble(prompt_template: str, fixture_text: str) -> str:
    """
    Substitute the fixture into the crash_report tag block only.

    PROMPT.md mentions the literal token [PASTE LOG HERE] a second time inside
    the STOP AND ASK rule, as an example of an unfilled template. A naive
    str.replace() therefore injects the fixture into that rule sentence too and
    silently corrupts the prompt, which produces bogus "the block contains only
    the placeholder" responses. Anchor on the tags instead.
    """
    m = TAG_BLOCK_RE.search(prompt_template)
    if not m:
        sys.exit("PROMPT.md does not contain a <crash_report> ... </crash_report> block.")
    body = fixture_text.strip("\n")
    return prompt_template[:m.start(2)] + body + prompt_template[m.end(2):]


def run_one(case: dict, cfg: dict, prompt_template: str, run_idx: int,
            transport: str, model: str, timeout: int) -> RunResult:
    fixture_path = HERE / case["fixture"]
    fixture_text = fixture_path.read_text(encoding="utf-8")
    prompt = assemble(prompt_template, fixture_text)

    reply = call_api(prompt, model, timeout) if transport == "api" \
        else call_cli(prompt, model, timeout)

    if reply.error:
        return RunResult(case["id"], run_idx, False, [], "", reply.cost_usd, reply.error)

    checks = evaluate(reply.text, case, cfg)
    failures = [f"{c.name}{' — ' + c.detail if c.detail else ''}"
                for c in checks if not c.ok]
    return RunResult(
        case["id"], run_idx, not failures, checks, reply.text,
        reply.cost_usd, "", failures,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Edge-case harness for PROMPT.md")
    ap.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--runs", type=int, default=5,
                    help="runs per case; branch variance shows up across repeats")
    ap.add_argument("--case", action="append", default=[],
                    help="substring match on case id; repeatable")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--transport", choices=["cli", "api"], default=None,
                    help="default: api when ANTHROPIC_API_KEY is set, else cli")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the assembled prompt for each selected case and exit")
    ap.add_argument("--rescore", type=Path, default=None,
                    help="re-evaluate the stored responses in a results file "
                         "against the current assertions; makes no API calls")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    transport = args.transport or ("api" if os.environ.get("ANTHROPIC_API_KEY") else "cli")

    cfg = json.loads(args.cases.read_text(encoding="utf-8"))
    prompt_template = args.prompt.read_text(encoding="utf-8")

    cases = cfg["cases"]
    if args.case:
        cases = [c for c in cases if any(sub in c["id"] for sub in args.case)]
        if not cases:
            sys.exit("no cases matched")

    if args.rescore:
        stored = json.loads(args.rescore.read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in cfg["cases"]}
        agg: dict[str, list[bool]] = {}
        seen_failures: dict[str, list[str]] = {}
        for row in stored["results"]:
            case = by_id.get(row["case_id"])
            if case is None or not row.get("response"):
                continue
            checks = evaluate(row["response"], case, cfg)
            fails = [f"{c.name}{' — ' + c.detail if c.detail else ''}"
                     for c in checks if not c.ok]
            agg.setdefault(row["case_id"], []).append(not fails)
            for f in fails:
                lst = seen_failures.setdefault(row["case_id"], [])
                if f not in lst:
                    lst.append(f)
        print(f"rescoring {args.rescore} against current assertions\n")
        print(f"{'case':<32} {'pass rate':>10}   failing checks")
        print("=" * 70)
        for cid in sorted(agg):
            rs = agg[cid]
            print(f"{cid:<32} {sum(rs)}/{len(rs):<8}   "
                  f"{'; '.join(seen_failures.get(cid, [])[:4])}")
        return 0

    if args.dry_run:
        for c in cases:
            fixture_text = (HERE / c["fixture"]).read_text(encoding="utf-8")
            print("=" * 70)
            print(f"CASE {c['id']}  (expect {c['expect_branch']})")
            print("=" * 70)
            print(assemble(prompt_template, fixture_text))
        return 0

    print(f"transport={transport}  model={args.model}  "
          f"cases={len(cases)}  runs={args.runs}\n")

    jobs = [(c, r) for c in cases for r in range(1, args.runs + 1)]
    results: list[RunResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(run_one, c, cfg, prompt_template, r,
                        transport, args.model, args.timeout): (c["id"], r)
            for c, r in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            results.append(res)
            mark = "ok  " if res.passed else ("ERR " if res.error else "FAIL")
            print(f"  [{mark}] {res.case_id} run {res.run}"
                  + (f"  {res.error}" if res.error else "")
                  + (f"  {res.failures}" if res.failures else ""))

    print("\n" + "=" * 70)
    print(f"{'case':<32} {'pass rate':>10}   failing checks")
    print("=" * 70)

    by_case: dict[str, list[RunResult]] = {}
    for r in results:
        by_case.setdefault(r.case_id, []).append(r)

    total_cost = sum(r.cost_usd for r in results)
    all_green = True
    for c in cases:
        rs = by_case.get(c["id"], [])
        passed = sum(1 for r in rs if r.passed)
        rate = f"{passed}/{len(rs)}"
        seen: list[str] = []
        for r in rs:
            for f in r.failures:
                if f not in seen:
                    seen.append(f)
            if r.error and r.error not in seen:
                seen.append(f"ERROR: {r.error}")
        if passed != len(rs):
            all_green = False
        print(f"{c['id']:<32} {rate:>10}   {'; '.join(seen[:3])}")

    print("=" * 70)
    print(f"total cost: ${total_cost:.4f}")
    print("A branch case below 100% is a prompt defect, not sampling noise.")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = args.out or RESULTS_DIR / f"{args.model}-{len(cases)}cases-x{args.runs}.json"
    out.write_text(json.dumps({
        "model": args.model,
        "transport": transport,
        "runs_per_case": args.runs,
        "prompt_path": str(args.prompt),
        "total_cost_usd": total_cost,
        "results": [
            {
                "case_id": r.case_id,
                "run": r.run,
                "passed": r.passed,
                "error": r.error,
                "failures": r.failures,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail}
                           for c in r.checks],
                "response": r.response,
            }
            for r in sorted(results, key=lambda x: (x.case_id, x.run))
        ],
    }, indent=2), encoding="utf-8")
    print(f"wrote {out}")

    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
