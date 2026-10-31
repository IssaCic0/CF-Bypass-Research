#!/usr/bin/env node
/**
 * 校验 docs/findings.json 里的每条研究结论是否"有据可查"。
 *
 * 为什么需要它：本仓库的核心卖点是"证据优先"，但证据目前是散文——
 * 报告一改稿，结论与证据就可能悄悄脱钩，而没有任何东西会报错。
 * 这个小脚本把"证据优先"变成 exit code。
 *
 * 校验规则（对应 docs/findings.json 的 schema）：
 *   1. id 唯一且形如 F-001
 *   2. verdict ∈ confirmed | needs_validation | rejected
 *   3. evidence 非空，且每个 path 在仓库里真实存在（防止文档漂移）
 *   4. needs_validation 不允许带 severity（未验证的问题不该被当成缺陷定级）
 *   5. confirmed 必须带 severity（已验证的问题要定级，才谈得上"修什么优先"）
 *
 * 零依赖、无副作用、不联网。
 * 用法：node scripts/validate-findings.mjs [--json] [--file docs/findings.json]
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function argValue(name) {
  const i = process.argv.indexOf(name);
  return i !== -1 ? process.argv[i + 1] : undefined;
}

const FINDINGS_PATH = resolve(ROOT, argValue("--file") ?? "docs/findings.json");

const VERDICTS = new Set(["confirmed", "needs_validation", "rejected"]);
const SEVERITIES = new Set(["critical", "high", "medium", "low"]);

const problems = [];

function problem(id, rule, message) {
  problems.push({ id, rule, message });
}

function main() {
  if (!existsSync(FINDINGS_PATH)) {
    console.error(`找不到 ${FINDINGS_PATH}`);
    process.exit(2);
  }

  let doc;
  try {
    doc = JSON.parse(readFileSync(FINDINGS_PATH, "utf8"));
  } catch (e) {
    console.error(`docs/findings.json 不是合法 JSON：${e.message}`);
    process.exit(2);
  }

  const findings = Array.isArray(doc.findings) ? doc.findings : null;
  if (!findings) {
    console.error("docs/findings.json 缺少 findings 数组");
    process.exit(2);
  }

  const seen = new Set();
  for (const f of findings) {
    const id = f?.id ?? "<无 id>";

    if (!/^F-\d{3}$/.test(id)) problem(id, "id-format", "id 必须形如 F-001");
    if (seen.has(id)) problem(id, "id-unique", "id 重复");
    seen.add(id);

    if (!f?.claim || typeof f.claim !== "string") {
      problem(id, "claim", "缺少 claim 字符串");
    }

    if (!VERDICTS.has(f?.verdict)) {
      problem(id, "verdict", `verdict 必须是 ${[...VERDICTS].join(" / ")}，实际 ${f?.verdict}`);
    }

    const evidence = Array.isArray(f?.evidence) ? f.evidence : [];
    if (evidence.length === 0) {
      problem(id, "evidence", "至少需要一条证据");
    }
    for (const ev of evidence) {
      if (!ev?.path) {
        problem(id, "evidence-path", "证据缺少 path");
        continue;
      }
      if (!existsSync(resolve(ROOT, ev.path))) {
        problem(id, "evidence-missing", `证据文件不存在：${ev.path}`);
      }
    }

    const hasSeverity = f?.severity !== undefined && f?.severity !== null;
    if (hasSeverity && !SEVERITIES.has(f.severity)) {
      problem(id, "severity-value", `severity 必须是 ${[...SEVERITIES].join(" / ")}`);
    }
    if (f?.verdict === "needs_validation" && hasSeverity) {
      problem(id, "severity-on-unverified", "needs_validation 不允许带 severity（未验证的不该定级）");
    }
    if (f?.verdict === "confirmed" && !hasSeverity) {
      problem(id, "severity-missing", "confirmed 必须带 severity");
    }
  }

  const asJson = process.argv.includes("--json");
  if (asJson) {
    console.log(
      JSON.stringify(
        { ok: problems.length === 0, findings: findings.length, problems },
        null,
        2,
      ),
    );
  } else {
    const byVerdict = findings.reduce((acc, f) => {
      acc[f.verdict] = (acc[f.verdict] ?? 0) + 1;
      return acc;
    }, {});
    console.log(
      `结论 ${findings.length} 条（confirmed ${byVerdict.confirmed ?? 0} / ` +
        `needs_validation ${byVerdict.needs_validation ?? 0} / rejected ${byVerdict.rejected ?? 0}）`,
    );
    if (problems.length === 0) {
      console.log("✅ 全部结论都有可定位的证据，且定级规则通过");
    } else {
      console.error(`❌ 发现 ${problems.length} 个问题：`);
      for (const p of problems) console.error(`  [${p.id}] ${p.rule}: ${p.message}`);
    }
  }

  process.exit(problems.length === 0 ? 0 : 1);
}

main();
