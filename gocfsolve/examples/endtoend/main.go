// 端到端验证：Go 网关 → cf_clearance 求解服务 → 回放目标站。
//
// 用法：
//   1) 先起求解服务：python solver/service.py --port 8878   （首启自动写 ./.env）
//   2) go run ./examples/endtoend -target https://steamdb.info/
//
// token 的来源优先级：-token 参数 → 环境变量 CF_SOLVE_TOKEN → 仓库根目录 ./.env
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/lza6/CF-Bypass-Research/gocfsolve"
)

func main() {
	target := flag.String("target", "https://steamdb.info/", "要访问的受 CF 保护的地址")
	solver := flag.String("solver", "http://127.0.0.1:8878", "求解服务地址")
	tokenFlag := flag.String("token", "", "求解服务 Bearer token（默认读 CF_SOLVE_TOKEN 或 ./.env）")
	egressID := flag.String("egress-id", os.Getenv("CF_EGRESS_ID"), "出口标识（换代理出口必须换它）")
	flag.Parse()

	token := *tokenFlag
	if token == "" {
		token = os.Getenv("CF_SOLVE_TOKEN")
	}
	if token == "" {
		token = readTokenFromEnvFile(".env")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()

	opts := []cfsolve.Option{cfsolve.WithToken(token)}
	if *egressID != "" {
		opts = append(opts, cfsolve.WithEgressID(*egressID))
	}
	cli := cfsolve.New(*solver, opts...)

	// 0) 健康检查（/health 是公开端点，且是三态语义）
	health, ready, err := cli.Health(ctx)
	if err != nil {
		fmt.Printf("❌ 求解服务不可用（%s）：%v\n   先启动: python solver/service.py --port 8878\n", *solver, err)
		os.Exit(1)
	}
	fmt.Printf("✅ 求解服务在线：state=%v browser_alive=%v queue_depth=%v\n",
		health["state"], health["browser_alive"], health["queue_depth"])
	if !ready {
		fmt.Printf("❌ 服务未就绪（state=%v, last_error=%v）\n", health["state"], health["last_error"])
		os.Exit(1)
	}
	if token == "" {
		fmt.Println("❌ 没有拿到 token：用 -token、CF_SOLVE_TOKEN，或把 .env 放在当前目录")
		os.Exit(1)
	}

	// 1) 取 clearance（第一次必然全新求解）
	t0 := time.Now()
	cl, err := cli.Get(ctx, *target)
	if err != nil {
		fmt.Printf("❌ 取 clearance 失败：%v\n", err)
		os.Exit(1)
	}
	ua := cl.UserAgent
	if len(ua) > 50 {
		ua = ua[:50]
	}
	fmt.Printf("✅ 取得 cf_clearance（%d 字节, UA=%s…, 求解 %.2fs, 总耗时 %.2fs, cached=%v, cookies=%d）\n",
		len(cl.CFClearance), ua, cl.Elapsed, time.Since(t0).Seconds(), cl.Cached, len(cl.CookieList))

	// 2) 回放请求
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, *target, nil)
	if err != nil {
		fmt.Printf("❌ 构造请求失败：%v\n", err)
		os.Exit(1)
	}
	t1 := time.Now()
	resp, err := cli.DoWith(ctx, req, cl)
	if err != nil {
		fmt.Printf("❌ 回放失败：%v\n", err)
		os.Exit(1)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 400000))
	resp.Body.Close()

	challenge := strings.Contains(string(body), "Just a moment") ||
		strings.Contains(string(body), "请稍候") ||
		strings.Contains(string(body), "cf-mitigated")
	fmt.Printf("📄 回放结果：status=%d 长度=%d 挑战页=%v 耗时=%.2fs\n",
		resp.StatusCode, len(body), challenge, time.Since(t1).Seconds())

	// 3) 二次调用应命中缓存
	cl2, err := cli.Get(ctx, *target)
	if err == nil {
		fmt.Printf("🔁 二次调用：cached=%v（命中缓存即不再启动浏览器）\n", cl2.Cached)
	}

	if resp.StatusCode == 200 && !challenge {
		fmt.Println("\n✅ 端到端通过：求解 → 回放 → 200 真实页面")
	} else {
		fmt.Println("\n❌ 端到端未通过")
		os.Exit(2)
	}
}

// readTokenFromEnvFile 从 .env 里读 CF_SOLVE_TOKEN（没有就返回空串）。
func readTokenFromEnvFile(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return ""
	}
	raw, err := os.ReadFile(abs)
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "CF_SOLVE_TOKEN=") {
			return strings.TrimSpace(strings.TrimPrefix(line, "CF_SOLVE_TOKEN="))
		}
	}
	return ""
}
