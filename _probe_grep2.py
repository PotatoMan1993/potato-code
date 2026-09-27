import io
import sys

sys.path.insert(0, "src")
from potatocode.tool.grep_tool import _MAX_LINE_CHARS as M, _iter_lines

Mxx = M
# 1) 内容丢失：在 [max, max+chunk) 之间放一个唯一标记
raw = "A" * (Mxx + 10) + "NEEDLE_TAIL" + "B" * (Mxx - 21)
out = list(_iter_lines(io.StringIO(raw), Mxx))
found = any("NEEDLE_TAIL" in t for _, t, _ in out)
print("single physical line -> yields:", len(out))
print("marker in discarded region [max, max+chunk) found anywhere?:", found)

# 2) 后续真实行的行号被抬高
content = "X" * int(2.5 * Mxx) + "\nsecond_real_line\nthird\n"
out2 = list(_iter_lines(io.StringIO(content), Mxx))
print("with trailing real lines -> yields:", len(out2))
for ln, txt, tl in out2:
    print(f"   lineno={ln} too_long={tl} head={txt[:20]!r}")
