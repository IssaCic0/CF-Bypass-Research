// 池演练（E5：崩溃隔离 / 摘除时延 / 自动回归）。
//
// 为什么需要它：`pool.go` 的摘除与回归只有单测（用 httptest 假节点），
// 从没在**真实进程**上演练过"杀一个 worker 会怎样"。
// 这个程序只做一件事：每秒打印一次 `pool.Nodes()` 的健康快照，
// 由外部脚本负责"杀进程 / 重启进程"，然后从日志里读时延。
//
// 用法：
//   go run ./examples/pooldrill -nodes http://127.0.0.1:8981,http://127.0.0.1:8982 \
//       -duration 60s -interval 1s -probe 2s
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/lza6/CF-Bypass-Research/gocfsolve"
)

func main() {
	nodes := flag.String("nodes", "", "逗号分隔的求解服务地址")
	duration := flag.Duration("duration", 60*time.Second, "观察总时长")
	interval := flag.Duration("interval", time.Second, "快照间隔")
	probe := flag.Duration("probe", 2*time.Second, "主动探活间隔；0=关闭")
	finalGet := flag.String("final-get", "", "可选：结尾对这个 URL 发一次 Get（会真的求解，慎用；默认不跑）")
	flag.Parse()

	if strings.TrimSpace(*nodes) == "" {
		fmt.Fprintln(os.Stderr, "必须给 -nodes")
		os.Exit(2)
	}
	urls := strings.Split(*nodes, ",")
	for i := range urls {
		urls[i] = strings.TrimSpace(urls[i])
	}

	opts := []cfsolve.PoolOption{
		cfsolve.WithPoolMaxFailures(2),
		cfsolve.WithPoolDownDuration(30 * time.Second),
	}
	if *probe > 0 {
		opts = append(opts, cfsolve.WithPoolHealthProbe(*probe))
	}
	pool, err := cfsolve.NewPool(urls, opts...)
	if err != nil {
		fmt.Fprintf(os.Stderr, "建池失败: %v\n", err)
		os.Exit(1)
	}
	defer pool.Close()

	deadline := time.Now().Add(*duration)
	tick := 0
	for time.Now().Before(deadline) {
		snapshot := map[string]any{
			"t":     tick,
			"at":    time.Now().Format("15:04:05"),
			"nodes": pool.Nodes(),
		}
		line, _ := json.Marshal(snapshot)
		fmt.Println(string(line))
		tick++
		time.Sleep(*interval)
	}

	// 结尾可选打一次。**默认不做**：演练若拿一个无盾站去 Get，会真的启动一次求解
	// 并在 ctx 到期时必然失败 —— 那是脚本设计问题，不是池的问题，很容易被误读成"池坏了"。
	if *finalGet != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		defer cancel()
		if _, err := pool.Get(ctx, *finalGet); err != nil {
			fmt.Printf("FINAL get-error: %v\n", err)
		} else {
			fmt.Println("FINAL get-ok: true")
		}
	}
}
