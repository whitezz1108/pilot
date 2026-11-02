"""Build a versioned Qwen3 boundary patch from the archived installed sources."""
from pathlib import Path
import difflib
import hashlib
import json

root = Path(__file__).resolve().parent
sources = {
    "reasoning_parser.py": "parser/reasoning_parser.py",
    "reasoner_grammar_backend.py": "constrained/reasoner_grammar_backend.py",
}
manifest = {}
for name, destination in sources.items():
    before = (root / "before" / name).read_text()
    after = before
    if name == "reasoning_parser.py":
        needle = '    def __init__(\n'
        start = after.index('class Qwen3Detector(')
        pos = after.index(needle, start)
        after = after[:pos] + ('    # Shared contract with ReasonerGrammarBackend: only </think> ends thought.\n'
                              '    requires_explicit_reasoning_end = True\n\n') + after[pos:]
        old = ('            # Qwen3.5 sometimes opens ``<tool_call>`` without closing\n'
               '            # ``</think>``; treat it as an implicit reasoning close.\n'
               '            tool_start_token="<tool_call>",\n')
        assert after.count(old) == 1
        after = after.replace(old, '            # Tool parsing receives only text after the explicit reasoning end.\n'
                                   '            tool_start_token=None,\n')
    else:
        old = '        self._enable_strict_thinking = enable_strict_thinking\n'
        assert after.count(old) == 1
        after = after.replace(old, '        # The detector and decoder must share one reasoning boundary.\n'
            '        self._explicit_reasoning_end = getattr(\n'
            '            reasoning_parser.detector, "requires_explicit_reasoning_end", False\n'
            '        )\n'
            '        self._enable_strict_thinking = enable_strict_thinking or self._explicit_reasoning_end\n')
        old = '            excluded_ids += new_ids\n'
        assert after.count(old) == 1
        after = after.replace(old, '            if self._explicit_reasoning_end and len(new_ids) != 1:\n'
            '                raise ValueError(\n'
            '                    f"Explicit reasoning boundary requires a single control token: {token!r}"\n'
            '                )\n' + old)
    compile(after, name, "exec")
    (root / "after").mkdir(exist_ok=True)
    (root / "after" / name).write_text(after)
    (root / (name + ".patch")).write_text("".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile="before/"+name, tofile="after/"+name)))
    manifest[name] = {"destination": destination,
        "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
        "after_sha256": hashlib.sha256(after.encode()).hexdigest()}
(root / "patch_manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
print(json.dumps(manifest))
