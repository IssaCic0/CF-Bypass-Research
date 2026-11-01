// 回放一致性复验：同一个 IP 下，用 **Go 客户端**（完整 Firefox 头部 + Go TLS）
// 回放一次真实站点，和"Python urllib 脚本回放"的结果对照。
//
// 要回答的问题：某些站点用脚本回放 403，到底是
//   (a) 服务/凭证的问题？  (b) 脚本的头部/TLS 不一致？  (c) 出口 IP 被提分？
// 用同一个服务、同一份凭证、同一个出口，只换**回放客户端**，就能把 (b) 从 (a)(c) 里分出来。
//
// 用法：
//   go run ./examples/replaycheck -solver http://127.0.0.1:8995 -token dummy \
//       -target https://epicgames.com/ -force
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
	solver := flag.String("solver", "http://127.0.0.1:8995", "求解服务地址")
	token := flag.String("token", "", "Bearer token（--no-auth 时随便填）")
	target := flag.String("target", "", "要复验的目标 URL")
	force := flag.Bool("force", false, "强制重新求解（忽略服务端缓存）")
	stopRedirect := flag.Bool("stop-redirect", false, "不跟随重定向（用来确认**第一跳**的状态码：CF 是放行还是拦）")
	replayURL := flag.String("replay-url", "", "可选：用同一份凭证回放到**另一个** URL（例如从 example.com 跟到 www.example.com）。空=用 -target")
	flag.Parse()

	if *target == "" {
		fmt.Fprintln(os.Stderr, "必须给 -target")
		os.Exit(2)
	}

	opts := []cfsolve.Option{cfsolve.WithSolveTimeout(90 * time.Second)}
	if *stopRedirect {
		opts = append(opts, cfsolve.WithReplayClient(&http.Client{
			Timeout:       30 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}))
	}
	if *token != "" {
		opts = append(opts, cfsolve.WithToken(*token))
	}
	c := cfsolve.New(*solver, opts...)

	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()

	// 1) 取凭证（force=false 时服务端缓存命中就不花求解预算）
	var cl *cfsolve.Clearance
	var err error
	t0 := time.Now()
	if *force {
		cl, err = c.GetFresh(ctx, *target)
	} else {
		cl, err = c.Get(ctx, *target)
	}
	if err != nil {
		fmt.Printf("RESOLVE_ERROR %v\n", err)
		os.Exit(1)
	}
	names := make([]string, 0, len(cl.CookieList))
	for _, ck := range cl.CookieList {
		names = append(names, ck.Name)
	}
	fmt.Printf("RESOLVE ok clearance_len=%d ua=%q cached=%v elapsed=%.2fs cookies=%v\n",
		len(cl.CFClearance), truncate(cl.UserAgent, 38), cl.Cached, time.Since(t0).Seconds(), names)

	// 2) 用 Go 客户端回放（它会套上 clearance 的 UA + 完整 cookie，并补 Firefox 导航头）
	to := *target
	if *replayURL != "" {
		to = *replayURL
	}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, to, nil)
	t1 := time.Now()
	resp, err := c.DoWith(ctx, req, cl)
	if err != nil {
		fmt.Printf("REPLAY_ERROR %v\n", err)
		os.Exit(1)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 40000))
	_ = resp.Body.Close()
	challenge := strings.Contains(strings.ToLower(string(body)), "just a moment") ||
		strings.Contains(strings.ToLower(string(body)), "_cf_chl_opt")
	fmt.Printf("REPLAY_GO status=%d bytes=%d challenge_page=%v elapsed=%.2fs\n",
		resp.StatusCode, len(body), challenge, time.Since(t1).Seconds())

	// 3) 对照：同样的 Go TLS/连接池，但**不带** clearance 头（确认 403 是不是"没凭证"造成的）
	req2, _ := http.NewRequestWithContext(ctx, http.MethodGet, to, nil)
	resp2, err := http.DefaultClient.Do(req2)
	if err == nil {
		body2, _ := io.ReadAll(io.LimitReader(resp2.Body, 4000))
		_ = resp2.Body.Close()
		fmt.Printf("REPLAY_NOCOOKIE status=%d bytes=%d\n", resp2.StatusCode, len(body2))
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
