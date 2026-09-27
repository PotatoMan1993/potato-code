import io
import sys

sys.path.insert(0, "src")
from potatocode.tool.grep_tool import _MAX_LINE_CHARS as M, _iter_lines

print("thresh", M, "chunk", 1 << 16)


def probe(L, label):
    out = list(_iter_lines(io.StringIO("A" * L), M))
    print(f"--- {label}: L={L} ({L / M:.2f}x) -> {len(out)} yields")
    for ln, txt, tl in out:
        print(f"   lineno={ln} len={len(txt)} too_long={tl} tail={txt[-1]!r}")


probe(2 * M, "2.0x")
probe(2 * M + 5000, "2.0x+")
probe(int(2.5 * M), "2.5x")
probe(3 * M, "3.0x")
