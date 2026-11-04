// alias-rules.js — T-71 的「规则语料」（rule corpus），不是真实流量样本。
//
// 每条 `record[<expr>]` 就是一条待判定的规则实例。`static_evaluable` 由 alias_shapes.py
// 里的简化求值器**实测**得出（能真求值才算静态），不是手写的布尔值。
//
// 生成 cfsolve 的嵌入清单（Rust 侧 include_str! 消费同一份数据）：
//   python tools/cfsolve-patches/analyze/alias_shapes.py \
//     tools/cfsolve-patches/analyze/fixtures/alias-rules.js \
//     --out /d/cfsolve-upstream/src/alias_rules.json
//
// 漂移校验（退出码非 0 即漂移）：
//   python tools/cfsolve-patches/analyze/alias_shapes.py \
//     tools/cfsolve-patches/analyze/fixtures/alias-rules.js \
//     --check /d/cfsolve-upstream/src/alias_rules.json
//
// 注意：只有**进入** cfsolve 计算属性判定的形态才需要登记。字符串字面量 key
// （`record["x"]`）在两边都由 `ast::member_str` 先解析掉，永远不会走到规则表。

var helper = { key: "name", name: "value" };
var record = {};
var a, b;
var getKey = function () { return "name"; };

// 唯一被数据判定为「静态别名」的形态（= 11-03 补丁里手工放宽的那一行）。
record[helper.key];

// 以下形态必须在数据里判「动态」，证明放宽没过头。
record[getKey()];
record[helper.get()];
record[a + b];
