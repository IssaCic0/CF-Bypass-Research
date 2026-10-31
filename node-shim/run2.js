/**
 * run2.js — A 路线（纯 JS 引擎 + 伪造层）实测：能否在不启动浏览器的情况下通过 CF 挑战。
 *
 * 流程：实时取挑战页 → jsdom + shims 执行 orchestrate 脚本 → 观察是否产生提交请求 /
 *       拿到 cf_clearance / 是否仍报错。
 */
const https = require("https");
const { JSDOM, VirtualConsole, CookieJar } = require("jsdom");
const { injectShims } = require("./shims");

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const TARGET = "https://steamdb.info/";
const WAIT_MS = 22000;

function fetchChallenge() {
  return new Promise((resolve, reject) => {
    const req = https.get(
      TARGET,
      { headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9" } },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (body += c));
        res.on("end", () =>
          resolve({ status: res.statusCode, headers: res.headers, body, setCookie: res.headers["set-cookie"] || [] })
        );
      }
    );
    req.on("error", reject);
    req.setTimeout(30000, () => req.destroy(new Error("timeout")));
  });
}

const requests = [];

(async () => {
  const page = await fetchChallenge();
  console.log(`首访 ${TARGET} -> ${page.status} bytes=${page.body.length}`);
  const cookieHeader = page.setCookie.map((c) => c.split(";")[0]).join("; ");
  console.log("服务端 Set-Cookie:", page.setCookie.map((c) => c.split(";")[0]).join(" | ") || "(无)");

  const jar = new CookieJar();
  for (const c of page.setCookie) {
    try {
      jar.setCookieSync(c, TARGET);
    } catch {}
  }

  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push("jsdomError: " + String(e.message || e).slice(0, 160)));
  vc.on("error", (...a) => errors.push("console.error: " + a.map(String).join(" ").slice(0, 160)));

  let shimReport = null;
  const dom = new JSDOM(page.body, {
    url: TARGET,
    referrer: TARGET,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    cookieJar: jar,
    virtualConsole: vc,
    beforeParse(window) {
      try {
        shimReport = injectShims(window);
      } catch (e) {
        errors.push("shim 注入失败: " + e.message);
      }
    },
  });

  console.log("\n=== shim 注入结果 ===");
  if (shimReport) {
    console.log(`  已安装 ${shimReport.installed.length} 项: ${shimReport.installed.join(", ")}`);
    if (shimReport.failed.length) console.log(`  失败: ${shimReport.failed.join(" | ")}`);
  }

  await new Promise((r) => setTimeout(r, WAIT_MS));

  const w = dom.window;
  const cookies = [];
  try {
    const sc = jar.getCookieStringSync(TARGET);
    sc.split("; ").forEach((c) => c && cookies.push(c.split("=")[0]));
  } catch {}

  console.log("\n=== 运行结果 ===");
  console.log(`  网络请求 ${requests.length} 条:`);
  [...new Set(requests)].slice(0, 14).forEach((u) => console.log("    " + u.slice(0, 120)));
  console.log(`  cookie 名: [${cookies.join(", ")}]`);
  console.log(`  cf_clearance: ${cookies.includes("cf_clearance") ? "✅ 拿到了" : "❌ 没有"}`);
  console.log(`  _cf_chl_opt 键数: ${Object.keys(w._cf_chl_opt || {}).length}`);

  const uniqErr = [...new Set(errors)];
  console.log(`\n  错误 ${errors.length} 条（去重 ${uniqErr.length}），前 10 条:`);
  uniqErr.slice(0, 10).forEach((e, i) => console.log(`    ${i + 1}. ${e}`));

  process.exit(0);
})().catch((e) => {
  console.error("FATAL:", e.message);
  process.exit(1);
});
