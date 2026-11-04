#!/usr/bin/env node
/**
 * 校验手写的 `docs/openapi.json` 与 `solver/service.py` 的真实路由是否一致（v5.0.1 P2-2）。
 *
 * 为什么需要：本项目不引入 FastAPI（policy.md『零新依赖』），所以 API 规格是**手写的**。
 * 手写规格的天敌是漂移：加了个端点忘了写规格 → 对接方按规格写代码就 404；
 * 删了个端点忘了删规格 → 对接方以为能用，实际 404。后者比前者更难查。
 * `docs/API.md`（28KB 散文）没法机器校验，但**路由集合**可以。
 *
 * 三条规则：
 *   1. 规格里声明的 `path + method` 必须与源码里实际出现的**完全一致**（双向比对）。
 *   2. 除 `/health`（容器探活）外，每个操作都必须继承全局 Bearer 鉴权
 *      —— 显式写 `"security": []` 就等于"我把这个端点开放了"，必须是有意为之。
 *   3. `info.version` 必须等于 `docs/facts.json` 的 version（与 validate-docs.mjs 同源）。
 *
 * 关于源码扫描的诚实说明：这里用正则从 `do_GET`/`do_POST` 里提取 `path == "/x"` 与
 * `path != "/x"`（后者是 `if path != "/solve"` 这种"剩下的都归它"的写法）。
 * 它是**近似**而不是 AST —— 所以脚本会先断言"至少提取到 N 条路由"，一旦路由写法变了
 * 到让正则失效，脚本会**大声报错**而不是静默通过（那才是真正的风险）。
 *
 * 零依赖、不联网。
 * 用法：node scripts/validate-openapi.mjs [--spec=docs/openapi.json]
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function argValue(name, fallback) {
  const hit = process.argv.find((a) => a.startsWith(`${name}=`));
  return hit ? hit.slice(name.length + 1) : fallback;
}

const SPEC_PATH = resolve(ROOT, argValue("--spec", "docs/openapi.json"));
const SRC_PATH = resolve(ROOT, "solver/service.py");
const FACTS_PATH = resolve(ROOT, "docs/facts.json");

/**
 * 刻意公开（免鉴权）的端点。**加一条必须先想清楚"未鉴权可读的内容会不会泄露什么"**。
 *   · GET /health —— 容器探活；未带 token 时只回最小集（state/worker/browser），不含内部细节
 *   · GET /       —— 只读状态页 HTML；页面本身**不含任何服务端数据**，
 *                    数据要靠用户在页面上填 token 去读 /health /status /logs
 */
const PUBLIC_PATHS = new Set(["GET /health", "GET /"]);

//: 路由提取的下限。低于这个数说明"提取规则失效"，绝不能当成"路由变少了"。
const MIN_GET_ROUTES = 5;
const MIN_POST_ROUTES = 3;

const problems = [];
const problem = (where, message) => problems.push(`${where}: ${message}`);

/** 取 `def <name>(` 到下一个顶层 `def ` / `class ` / 文件尾之间的源码。 */
function methodBody(source, name) {
  const start = source.indexOf(`    def ${name}(`);
  if (start === -1) return "";
  const rest = source.slice(start + 1);
  const endRel = rest.search(/\n    (?:def |class )/);
  return endRel === -1 ? rest : rest.slice(0, endRel);
}

function extractRoutes(source) {
  const routes = { GET: new Set(), POST: new Set() };
  for (const [method, fn] of [
    ["GET", "do_GET"],
    ["POST", "do_POST"],
  ]) {
    const body = methodBody(source, fn);
    if (!body) {
      problem("solver/service.py", `找不到 ${fn}()，路由提取失败`);
      continue;
    }
    // path == "/x"   → 该方法处理 /x
    // path != "/x"   → "剩下的都归这里"，等价于该方法处理 /x
    for (const m of body.matchAll(/path\s*[!=]=\s*"(\/[^"]*)"/g)) {
      routes[method].add(m[1]);
    }
  }
  return routes;
}

function specRoutes(spec) {
  const out = { GET: new Set(), POST: new Set() };
  for (const [path, item] of Object.entries(spec.paths ?? {})) {
    for (const method of Object.keys(item ?? {})) {
      const upper = method.toUpperCase();
      if (!out[upper]) continue; // parameters / summary 之类的非方法键
      out[upper].add(path);
    }
  }
  return out;
}

function main() {
  for (const [label, path] of [
    ["spec", SPEC_PATH],
    ["源码", SRC_PATH],
    ["facts", FACTS_PATH],
  ]) {
    if (!existsSync(path)) {
      console.error(`找不到 ${label}：${path}`);
      process.exit(2);
    }
  }

  let spec;
  try {
    spec = JSON.parse(readFileSync(SPEC_PATH, "utf8"));
  } catch (e) {
    console.error(`docs/openapi.json 不是合法 JSON：${e.message}`);
    process.exit(2);
  }

  if (!/^3\.\d+\.\d+$/.test(String(spec.openapi ?? ""))) {
    problem("docs/openapi.json", `openapi 必须是 3.x 版本串，实际 ${spec.openapi}`);
  }

  // ---- 规则 3：版本号与事实源一致 ----
  let facts = {};
  try {
    facts = JSON.parse(readFileSync(FACTS_PATH, "utf8"));
  } catch (e) {
    problem("docs/facts.json", `不是合法 JSON：${e.message}`);
  }
  if (facts.version && spec.info?.version !== facts.version) {
    problem(
      "docs/openapi.json",
      `info.version 写的是 ${spec.info?.version}，事实源是 ${facts.version}`,
    );
  }

  // ---- 规则 1：路由双向比对 ----
  const declared = specRoutes(spec);
  const actual = extractRoutes(readFileSync(SRC_PATH, "utf8"));

  for (const [method, min] of [
    ["GET", MIN_GET_ROUTES],
    ["POST", MIN_POST_ROUTES],
  ]) {
    if (actual[method].size < min) {
      problem(
        "solver/service.py",
        `只从 ${method === "GET" ? "do_GET" : "do_POST"} 提取到 ${actual[method].size} 条路由` +
          `（下限 ${min}）—— 路由写法变了导致提取规则失效，请更新本脚本的正则，不要当成"路由变少了"`,
      );
    }
  }

  for (const method of ["GET", "POST"]) {
    for (const path of actual[method]) {
      if (!declared[method].has(path)) {
        problem("docs/openapi.json", `源码里有 ${method} ${path}，规格里没写（对接方会以为不存在）`);
      }
    }
    for (const path of declared[method]) {
      if (!actual[method].has(path)) {
        problem("docs/openapi.json", `规格里写了 ${method} ${path}，源码里没有（对接方会按 404 排查半天）`);
      }
    }
  }

  // ---- 规则 2：鉴权声明 ----
  for (const [path, item] of Object.entries(spec.paths ?? {})) {
    for (const [method, op] of Object.entries(item ?? {})) {
      const upper = method.toUpperCase();
      if (upper !== "GET" && upper !== "POST") continue;
      const explicitlyOpen = Array.isArray(op.security) && op.security.length === 0;
      if (PUBLIC_PATHS.has(`${upper} ${path}`)) {
        if (!explicitlyOpen) {
          problem(
            "docs/openapi.json",
            `${upper} ${path} 应当显式声明 "security": []（它是刻意公开的：见 PUBLIC_PATHS）`,
          );
        }
        continue;
      }
      if (explicitlyOpen) {
        problem(
          "docs/openapi.json",
          `${upper} ${path} 被声明为免鉴权（"security": []）。本项目除 /health 外没有任何公开端点`,
        );
      }
    }
  }

  // ---- 输出 ----
  const fmt = (set) => [...set].sort().join(", ") || "（空）";
  console.log(`规格声明  GET: ${fmt(declared.GET)}`);
  console.log(`源码实现  GET: ${fmt(actual.GET)}`);
  console.log(`规格声明  POST: ${fmt(declared.POST)}`);
  console.log(`源码实现  POST: ${fmt(actual.POST)}`);

  if (problems.length === 0) {
    const total = declared.GET.size + declared.POST.size;
    console.log(`✅ ${total} 个操作与源码一致，鉴权声明正确，version 与事实源一致`);
    process.exit(0);
  }
  console.error(`❌ 发现 ${problems.length} 个问题：`);
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}

main();
