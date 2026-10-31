// 端到端验证：Go 网关 → cf_clearance 求解服务 → 回放目标站。
//
// 用法：
//   1) 先起求解服务：python solver/service.py --port 8878
//   2) go run ./examples/endtoend -target https://steamdb.info/ -solver http://127.0.0.1:8878
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/lza6/CF-Bypass-Research/gocfsolve"
)

func main() {
	target := flag.String("target", "https://steamdb.info/", "要访问的受 CF 保护的地址")
	solver := flag.String("solver", "http://127.0.0.1:8878", "求解服务地址")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()

	cli := cfsolve.New(*solver)

	// 0) 服务健康检查
	stats, err := cli.Stats(ctx)
	if err != nil {
		fmt.Printf("❌ 求解服务不可用（%s）：%v\n   先启动: python solver/service.py --port 8878\n", *solver, err)
		os.Exit(1)
	}
	fmt.Printf("✅ 求解服务在线：%v\n", stats)

	// 1) 取 clearance（第一次必然全新求解）
	t0 := time.Now()
	cl, err := cli.Get(ctx, *target)
	if err != nil {
		fmt.Printf("❌ 取 clearance 失败：%v\n", err)
		os.Exit(1)
	}
	fmt.Printf("✅ 取得 cf_clearance（%d 字节, UA=%s…, 求解 %.2fs, 总耗时 %.2fs, cached=%v）\n",
		len(cl.CFClearance), cl.UserAgent[:min(50, len(cl.UserAgent))], cl.Elapsed, time.Since(t0).Seconds(), cl.Cached)

	// 2) 回放请求
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, *target, nil)
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
	cl2, _ := cli.Get(ctx, *target)
	fmt.Printf("🔁 二次调用：cached=%v（命中缓存即不再启动浏览器）\n", cl2.Cached)

	if resp.StatusCode == 200 && !challenge {
		fmt.Println("\n✅ 端到端通过：求解 → 回放 → 200 真实页面")
	} else {
		fmt.Println("\n❌ 端到端未通过")
		os.Exit(2)
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
