"""alias_shapes.py — 计算属性 / 别名规则的「自动推导」（T-71，Python 侧，stdlib only）。

对 deobf 后的脚本抽取所有计算属性访问 `obj[expr]`，把 `expr` 归一化成形态签名：

  IDENT / MEMBER(IDENT,IDENT) / CALL(IDENT,[LIT,EXPR]) / BINOP(+) / FUNC(...)

输出 `{形态签名: {count, static_evaluable, static_instances, dynamic_instances,
examples[{file,line}]}}`。

**「是否可静态求值」是实测的，不是猜的**：用一个简化求值器（`evaluate`）真跑一遍——
它认识字面量、`const/let/var` 声明的常量对象/常量、成员取值、`+` 连接；遇到函数调用、
未知标识符一律判「不可静态求值」。所以 `obj[helper.key]`（helper 是常量对象）判静态，
`obj[getKey()]` 判动态——正是 11-03 补丁手工放宽的那两行必须区分的情形。

**边界（本轮）**：只做 Python 侧清单生成与断言，**不接 Rust 侧**
（`properties.rs` / `expression.rs` 的清单接入是 L3，需独立 commit）。
数据侧用 `--out` 生成清单，Rust 侧以后用 `include_str!` 消费同一份数据。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):  # 以脚本方式运行时补上同目录 import
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from primitive_gap import mask_source  # noqa: E402

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<ident>[A-Za-z_$][\w$]*)
  | (?P<op>\+\+|--|===|!==|=>|<=|>=|&&|\|\||[-+*/%<>=!&|^~?:.,;()\[\]{}])
""",
    re.X,
)

_PREC = {
    "||": 1, "&&": 2, "|": 3, "^": 4, "&": 5,
    "==": 6, "!=": 6, "===": 6, "!==": 6,
    "<": 7, ">": 7, "<=": 7, ">=": 7,
    "+": 8, "-": 8, "*": 9, "/": 9, "%": 9,
}
_KEYWORDS = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await "
    "var let const if for while switch catch function class this super".split()
)
_UNRESOLVED = object()


class ParseError(Exception):
    """归一化失败（表达式超出简化解析器的语法子集）。"""


# ---------------------------------------------------------------- 词法 / 字符串解码


def _decode_string(raw: str) -> str:
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
               "0": "\0", "\\": "\\", "'": "'", '"': '"', "`": "`"}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt == "x" and i + 3 < len(body) + 1:
                try:
                    out.append(chr(int(body[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt == "u" and i + 5 < len(body) + 1:
                try:
                    out.append(chr(int(body[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            out.append(escapes.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _tokenize(text: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            toks.append(("other", text[i]))
            i += 1
            continue
        i = m.end()
        kind = m.lastgroup or "other"
        if kind != "ws":
            toks.append((kind, m.group()))
    toks.append(("eof", ""))
    return toks


# ---------------------------------------------------------------- 简化解析器（表达式子集）


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokenize(text)
        self.i = 0

    # -- 游标
    def _peek(self, k: int = 0) -> tuple[str, str]:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else ("eof", "")

    def _take(self) -> tuple[str, str]:
        tok = self._peek()
        self.i += 1
        return tok

    def _expect(self, text: str) -> tuple[str, str]:
        tok = self._peek()
        if tok[1] != text:
            raise ParseError(f"expect {text!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    # -- 入口
    def parse_all(self):
        node = self._expression()
        if self._peek()[0] != "eof":
            raise ParseError(f"trailing token {self._peek()[1]!r}")
        return node

    def _expression(self):
        return self._binary(0)

    def _binary(self, min_prec: int):
        left = self._unary()
        while True:
            kind, text = self._peek()
            if kind != "op" or text not in _PREC:
                return left
            prec = _PREC[text]
            if prec < min_prec:
                return left
            self._take()
            right = self._binary(prec + 1)
            left = ("bin", text, left, right)

    def _unary(self):
        kind, text = self._peek()
        if (kind == "op" and text in ("!", "-", "+", "~")) or \
                (kind == "ident" and text in ("typeof", "void", "delete")):
            self._take()
            return ("un", text, self._unary())
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        while True:
            kind, text = self._peek()
            if kind != "op":
                return node
            if text == ".":
                self._take()
                prop_kind, prop_text = self._peek()
                if prop_kind not in ("ident", "num"):
                    raise ParseError("bad member")
                self._take()
                node = ("member", node, ("lit", prop_text), False)
            elif text == "[":
                self._take()
                key = self._expression()
                self._expect("]")
                node = ("member", node, key, True)
            elif text == "(":
                node = ("call", node, self._arguments())
            else:
                return node

    def _arguments(self) -> list:
        self._expect("(")
        args = []
        if self._peek()[1] == ")":
            self._take()
            return args
        while True:
            args.append(self._expression())
            if self._peek()[1] == ",":
                self._take()
                continue
            self._expect(")")
            return args

    def _primary(self):
        kind, text = self._peek()
        if kind == "num":
            self._take()
            try:
                value = float(int(text, 16)) if text.lower().startswith("0x") else float(text)
            except ValueError:
                raise ParseError("bad number") from None
            return ("lit", value)
        if kind == "str":
            self._take()
            return ("lit", _decode_string(text))
        if kind == "ident":
            if text in ("true", "false"):
                self._take()
                return ("lit", text == "true")
            if text in ("null", "undefined"):
                self._take()
                return ("lit", None)
            if text == "function":
                self._skip_function()
                return ("func",)
            if text == "async":
                self._take()
                if self._peek()[1] == "function":
                    self._take()
                self._skip_function()
                return ("func",)
            if self._peek(1)[1] == "=>":
                self._take()
                self._take()
                self._skip_arrow_body()
                return ("func",)
            self._take()
            return ("ident", text)
        if kind == "op" and text == "(":
            self._take()
            if self._peek()[1] == ")":
                self._take()
                if self._peek()[1] == "=>":
                    self._take()
                    self._skip_arrow_body()
                    return ("func",)
                raise ParseError("empty parens")
            node = self._expression()
            self._expect(")")
            if self._peek()[1] == "=>":
                self._take()
                self._skip_arrow_body()
                return ("func",)
            return node
        if kind == "op" and text == "[":
            self._take()
            items = []
            if self._peek()[1] == "]":
                self._take()
                return ("array", items)
            while True:
                items.append(self._expression())
                if self._peek()[1] == ",":
                    self._take()
                    if self._peek()[1] == "]":
                        self._take()
                        return ("array", items)
                    continue
                self._expect("]")
                return ("array", items)
        if kind == "op" and text == "{":
            return self._object()
        raise ParseError(f"unexpected token {text!r}")

    def _object(self):
        self._expect("{")
        pairs: list[tuple[str, object]] = []
        if self._peek()[1] == "}":
            self._take()
            return ("object", pairs)
        while True:
            kind, text = self._peek()
            if kind == "str":
                self._take()
                key = _decode_string(text)
            elif kind in ("ident", "num"):
                self._take()
                key = text
            elif kind == "op" and text == "[":
                self._take()
                key_node = self._expression()
                self._expect("]")
                key = key_node[1] if key_node[0] == "lit" else None
            else:
                raise ParseError("bad object key")
            if self._peek()[1] == ":":
                self._take()
                value = self._expression()
            else:
                value = ("ident", key)
            pairs.append((key, value))
            if self._peek()[1] == ",":
                self._take()
                if self._peek()[1] == "}":
                    self._take()
                    return ("object", pairs)
                continue
            self._expect("}")
            return ("object", pairs)

    # -- 跳过函数体（只保留 "这里是函数" 这一信息）
    def _skip_function(self):
        if self._peek()[1] == "*":  # generator
            self._take()
        if self._peek()[0] == "ident" and self._peek()[1] not in ("(", "{"):
            self._take()
        self._skip_balanced("(", ")")
        self._skip_balanced("{", "}")

    def _skip_balanced(self, open_tok: str, close_tok: str):
        self._expect(open_tok)
        depth = 1
        while depth:
            kind, text = self._take()
            if kind == "eof":
                raise ParseError("unbalanced")
            if text == open_tok:
                depth += 1
            elif text == close_tok:
                depth -= 1

    def _skip_arrow_body(self):
        if self._peek()[1] == "{":
            self._skip_balanced("{", "}")
        else:
            self._expression()


# ---------------------------------------------------------------- 渲染 / 求值


def render(node) -> str:
    kind = node[0]
    if kind == "lit":
        return "LIT"
    if kind == "ident":
        return "IDENT"
    if kind == "member":
        _k, base, prop, _computed = node
        if prop[0] == "lit" and isinstance(prop[1], str):
            prop_sig = "IDENT"  # 字符串常量 key 与 `a.b` 同形，避免因引号写法分裂
        else:
            prop_sig = render(prop)
        return f"MEMBER({render(base)},{prop_sig})"
    if kind == "call":
        _k, callee, args = node
        kinds = ",".join("LIT" if arg[0] == "lit" else "EXPR" for arg in args)
        return f"CALL({render(callee)},[{kinds}])"
    if kind == "bin":
        return f"BINOP({node[1]})"
    if kind == "un":
        return f"UNARY({node[1]})"
    if kind == "func":
        return "FUNC(...)"
    if kind == "array":
        return "ARRAY([LIT])" if all(i[0] == "lit" for i in node[1]) else "ARRAY([...])"
    if kind == "object":
        return "OBJECT({...})"
    return "EXPR"


def evaluate(node, env: dict):
    """简化求值器：能求出来才算「可静态求值」，否则 `_UNRESOLVED`。"""
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ident":
        return env.get(node[1], _UNRESOLVED)
    if kind == "member":
        _k, base_node, prop_node, _computed = node
        base = evaluate(base_node, env)
        key = prop_node[1] if prop_node[0] == "lit" else evaluate(prop_node, env)
        if base is _UNRESOLVED or key is _UNRESOLVED:
            return _UNRESOLVED
        if isinstance(base, dict):
            return base.get(key, _UNRESOLVED)
        if isinstance(base, (list, str)) and isinstance(key, float) and key.is_integer():
            idx = int(key)
            if 0 <= idx < len(base):
                return base[idx]
        return _UNRESOLVED
    if kind == "bin":
        left, right = evaluate(node[2], env), evaluate(node[3], env)
        if left is _UNRESOLVED or right is _UNRESOLVED:
            return _UNRESOLVED
        op = node[1]
        if op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return _js_str(left) + _js_str(right)
            if isinstance(left, float) and isinstance(right, float):
                return left + right
            return _UNRESOLVED
        if not (isinstance(left, float) and isinstance(right, float)):
            return _UNRESOLVED
        try:
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                return left / right if right else _UNRESOLVED
            if op == "%":
                return left % right if right else _UNRESOLVED
        except (ZeroDivisionError, OverflowError):
            return _UNRESOLVED
        return _UNRESOLVED
    if kind == "un":
        operand = evaluate(node[2], env)
        if operand is _UNRESOLVED:
            return _UNRESOLVED
        if node[1] == "-" and isinstance(operand, float):
            return -operand
        if node[1] == "+" and isinstance(operand, float):
            return operand
        if node[1] == "!":
            return not _truthy(operand)
        return _UNRESOLVED
    if kind == "array":
        items = [evaluate(item, env) for item in node[1]]
        return _UNRESOLVED if any(i is _UNRESOLVED for i in items) else items
    if kind == "object":
        out = {}
        for key, value_node in node[1]:
            if key is None:
                return _UNRESOLVED
            value = evaluate(value_node, env)
            if value is _UNRESOLVED:
                return _UNRESOLVED
            out[key] = value
        return out
    return _UNRESOLVED  # call / func / 其它：动态


def _js_str(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _truthy(value) -> bool:
    return not (value is None or value is False or value == 0 or value == "")


# ---------------------------------------------------------------- 抽取计算属性访问


def _bracket_pairs(masked: str) -> dict[int, int]:
    """一次扫描配对全部括号（O(n)）：开括号下标 → 闭括号下标。"""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for i, ch in enumerate(masked):
        if ch in "([{":
            stack.append(i)
        elif ch in ")]}":
            if stack:
                pairs[stack.pop()] = i
    return pairs


def find_computed_members(text: str) -> list[dict]:
    """找出所有 `obj[expr]` 的 expr（注释/字符串里的括号不参与配对）。"""
    masked = mask_source(text)
    pairs = _bracket_pairs(masked)
    found: list[dict] = []
    for i, ch in enumerate(masked):
        if ch != "[":
            continue
        j = i - 1
        while j >= 0 and masked[j] in " \t\r\n":
            j -= 1
        if j < 0 or not (masked[j].isalnum() or masked[j] in "_$)]"):
            continue
        if masked[j].isalnum() or masked[j] in "_$":
            k = j
            while k >= 0 and (masked[k].isalnum() or masked[k] in "_$"):
                k -= 1
            if masked[k + 1:j + 1] in _KEYWORDS:
                continue
        end = pairs.get(i, -1)
        if end < 0:
            continue
        expr = text[i + 1:end]
        if not expr.strip():
            continue
        found.append({"expr": expr, "index": i, "line": text.count("\n", 0, i) + 1})
    return found


def _declared_env(text: str) -> dict:
    """收集 `var/let/const NAME = <expr>` 里能静态求出的常量（按源码顺序）。"""
    masked = mask_source(text)
    env: dict = {}
    for m in re.finditer(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=", masked):
        start = m.end()
        end = _expression_end(masked, start)
        if end <= start:
            continue
        try:
            node = _Parser(text[start:end]).parse_all()
        except ParseError:
            continue
        value = evaluate(node, env)
        if value is not _UNRESOLVED:
            env[m.group(1)] = value
    return env


def _expression_end(masked: str, start: int) -> int:
    depth = 0
    for i in range(start, len(masked)):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif ch in ";\n," and depth == 0:
            return i
    return len(masked)


def normalize_expr(expr: str) -> str:
    """表达式 → 形态签名（去掉所有变量名，因此换名不换形）。"""
    try:
        return render(_Parser(expr).parse_all())
    except (ParseError, RecursionError):
        return "EXPR"


def analyze_source(text: str, filename: str = "iframe.deobfuscated.js") -> dict:
    """统计形态签名：次数 / 是否可静态求值（实测）/ 前 3 例。"""
    env = _declared_env(text)
    buckets: dict[str, dict] = {}
    for access in find_computed_members(text):
        try:
            node = _Parser(access["expr"]).parse_all()
            signature = render(node)
            static = evaluate(node, env) is not _UNRESOLVED
        except (ParseError, RecursionError):
            signature, static = "EXPR", False
        bucket = buckets.setdefault(signature, {
            "count": 0, "static_instances": 0, "dynamic_instances": 0, "examples": [],
        })
        bucket["count"] += 1
        bucket["static_instances" if static else "dynamic_instances"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append({"file": filename, "line": access["line"]})
    report: dict = {}
    for signature in sorted(buckets):
        bucket = buckets[signature]
        report[signature] = {
            "count": bucket["count"],
            "static_evaluable": bucket["dynamic_instances"] == 0 and bucket["static_instances"] > 0,
            "static_instances": bucket["static_instances"],
            "dynamic_instances": bucket["dynamic_instances"],
            "examples": bucket["examples"],
        }
    return report


def format_table(report: dict) -> str:
    if not report:
        return "未发现计算属性访问（obj[expr]）。"
    lines = [f"{'形态签名':<40}{'次数':>4}  可静态求值  前 3 例"]
    for signature, info in report.items():
        examples = ", ".join(f"{e['file']}:{e['line']}" for e in info["examples"])
        lines.append(
            f"{signature:<40}{info['count']:>4}  "
            f"{'是' if info['static_evaluable'] else '否':<10}  {examples}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="计算属性形态签名（T-71 Python 侧）")
    ap.add_argument("js", help="iframe.deobfuscated.js 路径")
    ap.add_argument("--out", default=None,
                    help="清单落盘路径（默认 <js 同目录>/alias-shapes.json）")
    ap.add_argument("--json", action="store_true", help="只打印 JSON")
    ap.add_argument("--check", default=None, metavar="RULES_JSON",
                    help="漂移校验：重新生成后与给定清单逐字节比较，不一致退出码 1")
    args = ap.parse_args(argv)
    js = Path(args.js)
    report = analyze_source(js.read_text(encoding="utf-8", errors="replace"), js.name)
    generated = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        # Rust 侧 src/alias_rules.json 就是这份字节，两边必须一致，否则规则静默失效。
        checked = Path(args.check)
        try:
            existing = checked.read_text(encoding="utf-8")
        except OSError as error:
            print(f"读不到清单 {checked}: {error}", file=sys.stderr)
            return 2
        if existing != generated:
            print(f"清单与规则语料不一致（漂移）：{checked}", file=sys.stderr)
            print("重新生成：python tools/cfsolve-patches/analyze/alias_shapes.py "
                  f"{args.js} --out {checked}", file=sys.stderr)
            return 1
        print(f"清单与规则语料一致：{checked}（{len(report)} 条形态）")
        return 0
    out = Path(args.out) if args.out else js.parent / "alias-shapes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generated, encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
          if args.json else format_table(report))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
