package cfsolve

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// ── X3：回放必须走与求解相同的出口 ───────────────────────────
//
// cf_clearance 绑「域 + 出口 IP + UA」。服务端可以配代理池（--proxy），求解走出口 A；
// 但回放是 Go 侧自己发请求 —— 若不走同一个出口，就必然 403，而且现象是"随机的、
// 换台机器就复现不了"的那种。所以客户端必须能按 clearance 的 egress_id 选代理，
// 并且在选不到时 **fail closed**。

func egressClearanceBody(egressID string) func(int32) (int, map[string]any) {
	return func(n int32) (int, map[string]any) {
		body := clearanceBody(n)
		body["egress_id"] = egressID
		return http.StatusOK, body
	}
}

func TestReplayGoesThroughTheMatchingEgressProxy(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, egressClearanceBody("ip:203.0.113.7"))
	defer solverSrv.Close()

	// 这个 httptest 服务扮演 HTTP 代理：它收到的会是绝对形式 URL，不需要真去拨号。
	var proxied int32
	var sawHost string
	proxySrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&proxied, 1)
		sawHost = r.Host
		_, _ = w.Write([]byte("VIA-PROXY"))
	}))
	defer proxySrv.Close()

	cli := New(solverSrv.URL,
		WithToken("test-token"),
		WithEgressProxy("ip:203.0.113.7", proxySrv.URL),
	)

	// 目标域名不需要可解析：请求应当只到代理，由代理应答。
	req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet,
		"http://target.example/", nil)
	resp, err := cli.Do(context.Background(), req)
	if err != nil {
		t.Fatalf("回放失败：%v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if string(body) != "VIA-PROXY" {
		t.Fatalf("回放没走代理，拿到的是 %q", string(body))
	}
	if c := atomic.LoadInt32(&proxied); c != 1 {
		t.Fatalf("代理应被请求 1 次，实际 %d 次", c)
	}
	if sawHost != "target.example" {
		t.Fatalf("代理收到的 Host 不对：%q（说明请求不是绝对形式，没真的走代理）", sawHost)
	}
}

func TestReplayFailsClosedWhenEgressHasNoRegisteredProxy(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, egressClearanceBody("ip:198.51.100.9"))
	defer solverSrv.Close()

	var proxied, direct int32
	proxySrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&proxied, 1)
		_, _ = w.Write([]byte("VIA-PROXY"))
	}))
	defer proxySrv.Close()
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&direct, 1)
		_, _ = w.Write([]byte("DIRECT"))
	}))
	defer target.Close()

	// 只登记了 A 出口，但求解回来的凭证属于 B 出口
	cli := New(solverSrv.URL,
		WithToken("test-token"),
		WithEgressProxy("ip:203.0.113.7", proxySrv.URL),
	)

	req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, target.URL, nil)
	_, err := cli.Do(context.Background(), req)

	if !errors.Is(err, ErrNoEgressProxy) {
		t.Fatalf("应返回 ErrNoEgressProxy（fail closed），实际 err=%v", err)
	}
	if c := atomic.LoadInt32(&proxied) + atomic.LoadInt32(&direct); c != 0 {
		t.Fatalf("fail closed 时不该真的发出任何请求，实际发了 %d 次", c)
	}
}

func TestNoRegisteredProxyKeepsDirectReplay(t *testing.T) {
	// 单出口部署（没配 --proxy）时行为必须与 v3 完全一致：直连回放。
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, egressClearanceBody("default"))
	defer solverSrv.Close()

	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("DIRECT-OK"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, target.URL, nil)
	resp, err := cli.Do(context.Background(), req)
	if err != nil {
		t.Fatalf("没登记出口代理时应直连回放，实际 err=%v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "DIRECT-OK" {
		t.Fatalf("回放内容不对：%q", body)
	}
}

// ── X5：本地缓存必须有界（只增不减 = 慢泄漏）────────────────────
func TestLocalCacheIsBounded(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	const limit = 3
	cli := New(solverSrv.URL, WithToken("test-token"), WithCacheLimit(limit))

	for i := 0; i < 12; i++ {
		if _, err := cli.Get(context.Background(), fmt.Sprintf("https://h%d.example/", i)); err != nil {
			t.Fatalf("第 %d 个域求解失败: %v", i, err)
		}
	}

	cli.mu.Lock()
	n := len(cli.cache)
	cli.mu.Unlock()
	if n > limit {
		t.Fatalf("缓存无界：上限 %d，实际 %d 条", limit, n)
	}
}

// ── X5：跨域重解必须受全局上限约束（防"大量域同时失效"风暴）──────
func TestRevalidateIsGatedByGlobalConcurrency(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("<html><title>Just a moment...</title></html>"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"), WithRevalidateConcurrency(1))

	cl, err := cli.Get(context.Background(), target.URL)
	if err != nil {
		t.Fatal(err)
	}
	if c := atomic.LoadInt32(&solverCalls); c != 1 {
		t.Fatalf("前置：应只求解 1 次，实际 %d", c)
	}

	// 手动把唯一的全局额度占住 —— 模拟"别的域正在重解"
	cli.revalidateSem <- struct{}{}
	req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, target.URL, nil)
	resp, err := cli.DoWith(context.Background(), req, cl)
	if err != nil {
		t.Fatalf("拿不到重解额度时应原样透传 403，而不是报错: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("应原样返回 403，实际 %d", resp.StatusCode)
	}
	if c := atomic.LoadInt32(&solverCalls); c != 1 {
		t.Fatalf("额度已满时不该重解：期望仍是 1 次求解，实际 %d 次", c)
	}

	// 释放额度后，同样的调用必须真的重解（证明拦住它的正是那个额度）
	<-cli.revalidateSem
	req2, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, target.URL, nil)
	resp2, err := cli.DoWith(context.Background(), req2, cl)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp2.Body.Close()
	if c := atomic.LoadInt32(&solverCalls); c < 2 {
		t.Fatalf("额度释放后应当重解：期望 ≥2 次求解，实际 %d 次", c)
	}
}

// 探活不该跟着求解的 240s 超时一起挂（原来 Health/Stats 复用同一个客户端）。
func TestProbeClientHasShortTimeout(t *testing.T) {
	cli := New("http://127.0.0.1:1", WithToken("t"))
	if cli.probe == nil || cli.probe.Timeout <= 0 || cli.probe.Timeout > 10*time.Second {
		t.Fatalf("探活客户端必须有短超时（≤10s），实际 %+v", cli.probe)
	}
	if cli.probe == cli.hc {
		t.Fatal("探活不该复用求解用的 240s 客户端")
	}
	// 顺带确认它真的被 Health 用上：不可达地址必须快速失败，而不是挂 4 分钟
	start := time.Now()
	_, _, err := cli.Health(context.Background())
	if err == nil {
		t.Fatal("不可达地址应当报错")
	}
	if d := time.Since(start); d > 6*time.Second {
		t.Fatalf("探活耗时 %v，说明没有用短超时客户端", d)
	}
}

// 命中缓存的返回值必须是深拷贝：调用方改 CookieList 不能污染缓存里那份。
func TestCachedClearanceIsDeepCopied(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	if _, err := cli.Get(context.Background(), "https://deep.example/"); err != nil {
		t.Fatal(err)
	}
	first, err := cli.Get(context.Background(), "https://deep.example/")
	if err != nil {
		t.Fatal(err)
	}
	if !first.Cached {
		t.Fatal("第二次应当命中本地缓存")
	}
	first.CookieList[0].Value = "POISONED"
	first.Cookies["cf_clearance"] = "POISONED"

	second, err := cli.Get(context.Background(), "https://deep.example/")
	if err != nil {
		t.Fatal(err)
	}
	if second.CookieList[0].Value == "POISONED" || second.Cookies["cf_clearance"] == "POISONED" {
		t.Fatal("缓存被调用方污染了：命中时返回的必须是深拷贝")
	}
}
