#!/usr/bin/env node
/**
 * 逐模块覆盖率门禁（v5.0.1 P1-4）。
 *
 * 为什么需要：`pytest --cov-fail-under=80` 只看 `solver/` 的**整体**数字。
 * 2026-11-04 审计实测整体 82% 达标，而 bootstrap.py 只有 38%、engines.py 57%
 * —— 整体数字把盲区平均掉了，门禁于是给出了虚假的安全感。
 * 而那块盲区里当场就藏着真缺陷（bootstrap.py 引用了未导入的 SolveError，
 * 让文档承诺的"缺依赖 → 友好提示 + exit 2"变成 NameError 裸 traceback）。
 *
 * 规则（三条，刻意严格）：
 *   1. 报告里出现的每个 solver/ 模块都必须在策略里登记阈值；
 *      **未登记 ≠ 无门槛**，未登记直接报错（逼新增模块的人想清楚它该多少）。
 *   2. 策略里登记的每个模块都必须真的出现在报告里（文件改名/删除后忘了更新策略也是错）。
 *   3. 实测值不得低于登记的 min。带 `waiver` 的模块照样受 min 约束（防继续腐烂），
 *      只是额外打印豁免理由。
 *
 * 零依赖、不联网。
 * 用法：
 *   python -m pytest tests/ -q --cov=solver --cov-report=json:coverage.json
 *   node scripts/check-coverage.mjs [--coverage-json=coverage.json] [--policy=docs/coverage-policy.json]
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function argValue(name, fallback) {
  const hit = process.argv.find((a) => a.startsWith(`${name}=`));
  return hit ? hit.slice(name.length + 1) : fallback;
}

const COV_PATH = resolve(ROOT, argValue("--coverage-json", "coverage.json"));
const POLICY_PATH = resolve(ROOT, argValue("--policy", "docs/coverage-policy.json"));

const problems = [];
const rows = [];

function norm(p) {
  return p.replace(/\\/g, "/").replace(/^\.\//, "");
}

function main() {
  if (!existsSync(COV_PATH)) {
    console.error(`找不到覆盖率报告 ${COV_PATH}`);
    console.error("先跑：python -m pytest tests/ -q --cov=solver --cov-report=json:coverage.json");
    process.exit(2);
  }
  if (!existsSync(POLICY_PATH)) {
    console.error(`找不到策略文件 ${POLICY_PATH}`);
    process.exit(2);
  }

  let cov;
  let policy;
  try {
    cov = JSON.parse(readFileSync(COV_PATH, "utf8"));
  } catch (e) {
    console.error(`覆盖率报告不是合法 JSON：${e.message}`);
    process.exit(2);
  }
  try {
    policy = JSON.parse(readFileSync(POLICY_PATH, "utf8"));
  } catch (e) {
    console.error(`策略文件不是合法 JSON：${e.message}`);
    process.exit(2);
  }

  const modules = policy?.modules ?? {};
  const files = cov?.files ?? {};

  // 覆盖率报告里若是**绝对路径**（在别的目录生成、或传了绝对 --cov-report=json），
  // 下面的 `startsWith("solver/")` 会把所有条目都跳过 → 最后报出 N 条
  // "在策略里登记了但报告里没有"，把"跑错目录"误诊成"文件改名"。
  const looksAbsolute = Object.keys(files).some((p) => /^[A-Za-z]:[\\/]|^\//.test(p));
  if (looksAbsolute) {
    console.error("覆盖率报告里的键看起来是绝对路径，请在仓库根生成并运行：");
    console.error("  python -m pytest tests/ -q --cov=solver --cov-report=json:coverage.json");
    console.error("  node scripts/check-coverage.mjs --coverage-json=coverage.json");
    process.exit(2);
  }

  const seen = new Set();
  for (const [rawPath, info] of Object.entries(files)) {
    const path = norm(rawPath);
    if (!path.startsWith("solver/")) continue; // 只对 solver 包设门槛
    seen.add(path);

    const pct = Number(info?.summary?.percent_covered ?? NaN);
    const rule = modules[path];

    if (!rule) {
      problems.push(`${path}: 未在 docs/coverage-policy.json 登记覆盖率阈值（实测 ${pct.toFixed(1)}%）`);
      rows.push({ path, pct, min: null, verdict: "未登记" });
      continue;
    }
    const min = Number(rule.min);
    const low = !(pct >= min); // NaN 也走这里
    rows.push({
      path,
      pct,
      min,
      waived: Boolean(rule.waiver),
      verdict: low ? `低于 ${min}%` : rule.waiver ? "WAIVED" : "ok",
    });
    if (low) {
      problems.push(
        `${path}: 覆盖率 ${pct.toFixed(1)}% 低于门禁 ${min}%` +
          (rule.why ? `（该模块的门禁理由：${rule.why}）` : ""),
      );
    }
  }

  for (const path of Object.keys(modules)) {
    if (!seen.has(norm(path))) {
      problems.push(`${path}: 在策略里登记了，但覆盖率报告里没有它（文件改了名/被删？请同步策略）`);
    }
  }

  // 稳定输出：按模块名排序，便于 diff 与人工比对
  rows.sort((a, b) => a.path.localeCompare(b.path));
  const width = Math.max(...rows.map((r) => r.path.length), 20);
  console.log(`${"模块".padEnd(width)}  实测     门禁   结论`);
  for (const r of rows) {
    const pct = Number.isFinite(r.pct) ? `${r.pct.toFixed(1)}%`.padStart(6) : "   n/a";
    const min = r.min === null ? "   -" : `${r.min}%`.padStart(5);
    console.log(`${r.path.padEnd(width)}  ${pct}   ${min}   ${r.verdict}`);
  }

  const waived = rows.filter((r) => r.waived);
  if (waived.length > 0) {
    console.log("\n豁免（min 仍然生效，只是允许低于其它模块的口径）：");
    for (const r of waived) {
      console.log(`  · ${r.path}: ${modules[r.path].waiver}`);
    }
  }

  const total = cov?.totals?.percent_covered;
  if (total !== undefined) console.log(`\nsolver/ 整体：${Number(total).toFixed(2)}%`);

  if (problems.length === 0) {
    console.log(`✅ ${rows.length} 个模块全部达到各自门禁`);
    process.exit(0);
  }
  console.error(`\n❌ 发现 ${problems.length} 个问题：`);
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}

main();
