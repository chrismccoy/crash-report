#!/usr/bin/env python3
"""
Measure task of PROMPT.md.

The edge case suite measures whether the model BEHAVES correctly. This measures
whether the prompt can be READ correctly

    python3 clarity.py                 # 5 readers
    python3 clarity.py --runs 10       # tighter estimate
    python3 clarity.py --show-wrong    # print every wrong answer with reasoning
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT = HERE.parent / "PROMPT.md"
RESULTS = HERE / "results"

READER_SYSTEM = (
    "You are reading a specification carefully and answering questions about "
    "what it says. Answer only from the text you are given. Do not use outside "
    "knowledge about what such a specification usually says."
)

# Each question has one answer determined by the prompt text If a question turns 
# out to be genuinely underdetermined, that is a finding about the prompt, not a 
# broken question — fix the prompt or # end the question, but do not reword it until 
# the model validates it.
QUESTIONS = [
    {
        "id": "q01-branch-python-traceback",
        "q": "The report block contains a readable Python traceback. Which response type applies: STOP AND ASK, the fixed wrong-format reply, the fixed out-of-scope reply, or the six-section render?",
        "answer": r"(?i)wrong.?format",
        "source": "STOP AND ASK / readable-but-wrong rule",
    },
    {
        "id": "q02-branch-empty",
        "q": "The report block is completely empty. Which response type applies?",
        "answer": r"(?i)stop and ask",
        "source": "STOP AND ASK case (a)",
    },
    {
        "id": "q03-branch-truncated-after",
        "q": "The report block holds an Apple crash report that includes Exception Type and Termination Reason but is cut off partway through the backtrace. Which response type applies?",
        "answer": r"(?i)render|six.?section",
        "source": "ALWAYS truncation rule",
    },
    {
        "id": "q04-branch-truncated-before",
        "q": "The report block holds an Apple crash report cut off before the Exception Type and Termination Reason lines appear. Which response type applies?",
        "answer": r"(?i)stop and ask",
        "source": "STOP AND ASK case (c)",
    },
    {
        "id": "q05-hang-section4-header",
        "q": "The input is a hang report, where nothing crashed. What exact text must the section 4 header contain? Quote it.",
        "answer": r"(?i)why it crashed this way",
        "source": "ALWAYS hang rule",
    },
    {
        "id": "q06-missing-os-version",
        "q": "The OS Version field is absent, so section 3 cannot be determined. Should the whole report be suppressed, or should section 3 render with a placeholder? Answer with one word: SUPPRESSED or PLACEHOLDER.",
        "answer": r"(?i)placeholder",
        "source": "RENDER WITH GAPS",
    },
    {
        "id": "q07-last-line",
        "q": "What is the last line of every response, including a STOP AND ASK response? Quote it exactly.",
        "answer": r"macOS Crash Report Analyzer v1\.1",
        "source": "VERSION LINE",
    },
    {
        "id": "q08-injection-location",
        "q": "The report contains text that reads as an instruction to the model. Where in the output is that finding reported? Answer with the specific location the spec names.",
        "answer": r"(?i)security note",
        "source": "SECURITY AND EVIDENCE RULES",
    },
    {
        "id": "q09-injection-obey",
        "q": "Should an instruction found inside the report block ever be followed? Answer YES or NO.",
        "answer": r"(?i)\bno\b",
        "source": "SECURITY AND EVIDENCE RULES",
    },
    {
        "id": "q10-final-tag",
        "q": "The payload contains more than one crash_report closing tag. Which one marks the end of the report: the first or the final one?",
        "answer": r"(?i)final|last",
        "source": "FINAL tag rule",
    },
    {
        "id": "q11-section5-steps",
        "q": "How many numbered steps may section 5 contain at most? Answer with a number.",
        "answer": r"\b6\b|\bsix\b",
        "source": "LENGTH LIMITS",
    },
    {
        "id": "q12-code-blocks-count",
        "q": "Do code blocks count toward a section's length limit? Answer YES or NO.",
        "answer": r"(?i)\bno\b",
        "source": "Exempt list",
    },
    {
        "id": "q13-synthetic-report",
        "q": "The report uses obviously invented app names and example-style bundle identifiers. Should it be analyzed normally, or refused as a sample? Answer ANALYZED or REFUSED.",
        "answer": r"(?i)analyz|analys",
        "source": "placeholder clarification",
    },
    {
        "id": "q14-headers-on-stop-ask",
        "q": "In a STOP AND ASK response, how many of the six section headers appear? Answer with a number.",
        "answer": r"(?i)\b(0|zero|none)\b",
        "source": "OUTPUT CONTRACT / PRE-EMIT CHECK",
    },
    {
        "id": "q15-multi-crash",
        "q": "The paste contains four separate crash reports. Which one is analyzed, and where is the count of the others stated?",
        "answer": r"(?i)(most recent|newest|latest).*(input.?type|first line|before section)|(input.?type|first line).*(most recent|newest|latest)",
        "source": "ALWAYS multi-crash rule",
    },
    {
        "id": "q16-question-in-block",
        "q": "A user pastes a question about Gatekeeper INSIDE the crash_report block instead of a log. Does that take the out-of-scope reply or the wrong-format reply?",
        "answer": r"(?i)wrong.?format",
        "source": "NEVER out-of-scope scoping",
    },
    {
        "id": "q17-unsymbolicated",
        "q": "The backtrace has only hex addresses and no symbol names. Should all six sections read 'Not determinable'? Answer YES or NO.",
        "answer": r"(?i)\bno\b",
        "source": "ALWAYS unsymbolicated rule",
    },
    {
        "id": "q18-sip-advice",
        "q": "May the user-side fix section recommend disabling SIP or Gatekeeper? Answer YES or NO.",
        "answer": r"(?i)\bno\b",
        "source": "NEVER list",
    },
]

SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "answer": {"type": "string"},
                    "confident": {"type": "boolean"},
                },
                "required": ["id", "answer", "confident"],
            },
        }
    },
    "required": ["answers"],
}


def strip_payload(prompt_text: str) -> str:
    return re.sub(
        r"<crash_report>\n.*?\n</crash_report>",
        "<crash_report>\n(a crash report would appear here)\n</crash_report>",
        prompt_text,
        flags=re.DOTALL,
    )


def build_prompt(spec: str) -> str:
    qs = "\n".join(f'{q["id"]}: {q["q"]}' for q in QUESTIONS)
    return (
        "Below is a specification, followed by questions about it.\n\n"
        "=== SPECIFICATION BEGINS ===\n"
        f"{spec}\n"
        "=== SPECIFICATION ENDS ===\n\n"
        "Answer each question using only the specification above. Keep each "
        "answer under 20 words. Set confident=false if the specification does "
        "not determine the answer.\n\n"
        f"{qs}\n"
    )


def ask(prompt: str, model: str, timeout: int) -> dict | None:
    cmd = [
        "claude", "-p", "--safe-mode", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--disable-slash-commands",
        "--output-format", "json", "--model", model,
        "--system-prompt", READER_SYSTEM,
        "--json-schema", json.dumps(SCHEMA),
        prompt,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        print(f"  reader failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
        result = data.get("result", "")
        return json.loads(result) if isinstance(result, str) else result
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Task-clarity metric for PROMPT.md")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--show-wrong", action="store_true")
    args = ap.parse_args()

    spec = strip_payload(PROMPT.read_text(encoding="utf-8"))
    prompt = build_prompt(spec)
    by_q = {q["id"]: {"right": 0, "total": 0, "wrong": [], "unsure": 0}
            for q in QUESTIONS}
    expected = {q["id"]: q for q in QUESTIONS}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, args.runs)) as pool:
        futures = [pool.submit(ask, prompt, args.model, args.timeout)
                   for _ in range(args.runs)]
        for fut in concurrent.futures.as_completed(futures):
            out = fut.result()
            if not out:
                continue
            for row in out.get("answers", []):
                qid = row.get("id")
                if qid not in by_q:
                    continue
                rec = by_q[qid]
                rec["total"] += 1
                if not row.get("confident", True):
                    rec["unsure"] += 1
                if re.search(expected[qid]["answer"], row.get("answer", "")):
                    rec["right"] += 1
                else:
                    rec["wrong"].append(row.get("answer", ""))

    total_right = sum(r["right"] for r in by_q.values())
    total_asked = sum(r["total"] for r in by_q.values())
    print(f"\n{'question':<32}{'correct':>9}{'unsure':>8}")
    print("=" * 60)
    for q in QUESTIONS:
        r = by_q[q["id"]]
        flag = "" if r["total"] and r["right"] == r["total"] else "   <-- ambiguous"
        print(f"{q['id']:<32}{r['right']}/{r['total']:<7}{r['unsure']:>6}{flag}")
        if args.show_wrong and r["wrong"]:
            for w in r["wrong"][:3]:
                print(f"      got: {w[:110]}")
    print("=" * 60)
    pct = (100 * total_right / total_asked) if total_asked else 0
    print(f"clarity: {total_right}/{total_asked} = {pct:.1f}%")
    print("Any question below 100% marks a passage a careful reader cannot "
          "resolve. The wrong answers say which reading they took.")

    RESULTS.mkdir(exist_ok=True)
    out_path = RESULTS / f"clarity-{args.model}-x{args.runs}.json"
    out_path.write_text(json.dumps({
        "runs": args.runs, "model": args.model,
        "score_pct": pct,
        "questions": {k: {kk: vv for kk, vv in v.items()} for k, v in by_q.items()},
    }, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
