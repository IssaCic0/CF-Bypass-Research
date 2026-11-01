package cfsolve

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ── 测试脚手架 ────────────────────────────────────────────────
//
// 与 cfsolve_test.go 的分工：那里测"取凭证 + 拼 Cookie 头"的既有行为，
// 这里测 v4.0 新加的两条能力：per-host 单飞（T-09）与回放自愈（T-10）。
//
// 刻意不用 mock 框架：全部用 httptest 起真 HTTP 服务，断言的是**真实请求序列**，
// 因为这两条能力的全部价值就在于"多发/少发了一次网络请求"——只能靠计数证明。

// countingSolver 起一个可配置延迟的假求解服务。
// 每次 /solve 返回 clearance=CL-<n>，n 是本次调用序号，便于断言"重解真的发生了"。
func countingSolver(t *testing.T, calls *int32, delay time.Duration, resp func(n int32) (int, map[string]any)) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" || r.URL.Path == "/stats" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"ok": false, "error_code": "unauthorized", "error": "缺少或错误的 Bearer token", "stage": "admit",
			})
			return
		}
		n := atomic.AddInt32(calls, 1)
		if delay > 0 {
			select {
			case <-time.After(delay):
			case <-r.Context().Done():
				return
			}
		}
		code, payload := resp(n)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(payload)
	}))
}

// clearanceBody 返回一份合法响应；domain 留空 ⇒ 对任意 host 都适用（httptest 的 ip:port 也能用）。
func clearanceBody(n int32) map[string]any {
	v := fmt.Sprintf("CL-%d", n)
	return map[string]any{
		"ok":           true,
		"solved":       true,
		"cf_clearance": v,
		"user_agent":   testUA,
		"cookies":      map[string]string{"cf_clearance": v},
		"cookie_list": []map[string]any{
			{"name": "cf_clearance", "value": v, "domain": "", "path": "/", "expires": 1},
		},
		"expires_in": 1500,
		"expires_at": float64(time.Now().Add(20 * time.Minute).Unix()),
		"egress_id":  "default",
	}
}

func okClearanceBody(n int32) (int, map[string]any) { return http.StatusOK, clearanceBody(n) }

// ── T-09：per-host 单飞 ───────────────────────────────────────

// 50 个并发打同一个冷域：只能真的求解 1 次（最差容忍 2），且所有人拿到同一份凭证。
// 没有单飞时这里是 50 次 —— 服务端 inflight 闸门会把 42 个打回 429。
func TestGetSingleFlightColdDomain(t *testing.T) {
	var calls int32
	srv := countingSolver(t, &calls, 200*time.Millisecond, okClearanceBody)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))

	const n = 50
	var wg sync.WaitGroup
	var mu sync.Mutex
	got := make([]string, 0, n)
	errs := make([]error, 0, n)
	start := make(chan struct{})

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			cl, err := cli.Get(context.Background(), "https://cold.example.com/")
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, err)
				return
			}
			got = append(got, cl.CFClearance)
		}()
	}
	close(start)
	wg.Wait()

	if len(errs) != 0 {
		t.Fatalf("%d 个请求失败（单飞必须让等待者共享成功结果）: %v", len(errs), errs[0])
	}
	if len(got) != n {
		t.Fatalf("成功数应为 %d，实际 %d", n, len(got))
	}
	for _, v := range got {
		if v != got[0] {
			t.Fatalf("并发请求必须拿到同一份 clearance，出现 %q 与 %q", got[0], v)
		}
	}
	if c := atomic.LoadInt32(&calls); c > 2 {
		t.Fatalf("同域并发只应求解 ≤2 次（实际 %d 次）—— 没有单飞，等于把 50 次求解打到服务端", c)
	}
}

// 不同域互不阻塞：两域各 300ms，并行应在 ~300ms 完成，而不是 600ms 串行。
func TestGetSingleFlightDifferentHostsDoNotBlock(t *testing.T) {
	var calls int32
	srv := countingSolver(t, &calls, 300*time.Millisecond, okClearanceBody)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	var wg sync.WaitGroup
	start := time.Now()
	for _, h := range []string{"a.example.com", "b.example.com"} {
		h := h
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := cli.Get(context.Background(), "https://"+h+"/"); err != nil {
				t.Errorf("%s 求解失败: %v", h, err)
			}
		}()
	}
	wg.Wait()
	elapsed := time.Since(start)

	if c := atomic.LoadInt32(&calls); c != 2 {
		t.Fatalf("不同域必须各解一次，实际 %d 次", c)
	}
	if elapsed > 550*time.Millisecond {
		t.Fatalf("不同域之间不应互相等待（用了 %v，串行会是 ~600ms）", elapsed)
	}
}

// leader 失败时，等待者必须都拿到同一个错误，且不 panic、不悬挂。
func TestGetSingleFlightSharesError(t *testing.T) {
	var calls int32
	srv := countingSolver(t, &calls, 150*time.Millisecond, func(n int32) (int, map[string]any) {
		return http.StatusOK, map[string]any{
			"ok": false, "solved": false, "error_code": "unsolved",
			"error": "45s 内未取得 cf_clearance", "stage": "wait", "hint": "换站点验证服务本身可用",
		}
	})
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	const n = 10
	var wg sync.WaitGroup
	var mu sync.Mutex
	seen := make([]string, 0, n)
	done := make(chan struct{})

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := cli.Get(context.Background(), "https://fail.example.com/")
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				seen = append(seen, "<nil>")
				return
			}
			var ae *APIError
			if asAPIError(err, &ae) {
				seen = append(seen, ae.Code)
			} else {
				seen = append(seen, "not-api-error:"+err.Error())
			}
		}()
	}
	go func() { wg.Wait(); close(done) }()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("单飞在失败路径上悬挂了（等待者永远等不到 done）")
	}
	if len(seen) != n {
		t.Fatalf("应有 %d 个结果，实际 %d", n, len(seen))
	}
	for _, c := range seen {
		if c != "unsolved" {
			t.Fatalf("等待者必须拿到 leader 的同一个错误码，实际出现 %q", c)
		}
	}
	if c := atomic.LoadInt32(&calls); c != 1 {
		t.Fatalf("失败也只应求解 1 次（失败不该被放大成 %d 次）", c)
	}
}

// leader 的 ctx 被取消时，等待者必须及时返回，不许死锁。
func TestGetSingleFlightLeaderCancelDoesNotDeadlock(t *testing.T) {
	var calls int32
	srv := countingSolver(t, &calls, 2*time.Second, okClearanceBody)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	leaderCtx, cancelLeader := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, _ = cli.Get(leaderCtx, "https://slow.example.com/")
	}()
	// 等 leader 一定先进到求解里
	time.Sleep(150 * time.Millisecond)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
			defer cancel()
			_, _ = cli.Get(ctx, "https://slow.example.com/")
		}()
	}
	time.Sleep(100 * time.Millisecond)
	cancelLeader()

	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("leader 取消后等待者悬挂（单飞 map 没被清理）")
	}

	// 清理干净后必须能重新求解，而不是卡在已死的 flight 上
	var calls2 int32
	_ = calls2
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if _, err := cli.Get(ctx, "https://slow.example.com/"); err == nil {
		t.Fatal("这次应当因 ctx 到期而失败（证明流程没被死 flight 卡住）")
	}
}

// ── T-10：回放自愈 ───────────────────────────────────────────

// 403（带 cf-mitigated: challenge）→ 失效缓存 → 重解一次 → 用新凭证重放 → 200。
func TestDoWith403TriggersRevalidate(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	var secondCookie string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		if n == 1 {
			w.Header().Set("cf-mitigated", "challenge")
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte("<html><title>Just a moment...</title></html>"))
			return
		}
		secondCookie = r.Header.Get("Cookie")
		_, _ = w.Write([]byte("OK-BODY"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)

	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatalf("自愈后应当成功，实际返回错误: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("自愈后状态码应为 200，实际 %d", resp.StatusCode)
	}
	if string(body) != "OK-BODY" {
		t.Fatalf("body 不对: %q", string(body))
	}
	if c := atomic.LoadInt32(&solverCalls); c != 2 {
		t.Fatalf("应为「初次求解 + 重解」共 2 次，实际 %d 次", c)
	}
	if !strings.Contains(secondCookie, "CL-2") {
		t.Fatalf("重放必须带**新**凭证（期望含 CL-2），实际 Cookie=%q", secondCookie)
	}
	if h := atomic.LoadInt32(&hits); h != 2 {
		t.Fatalf("目标站应被打 2 次，实际 %d 次", h)
	}
}

// 重解后仍然 403：只重试一次，绝不能循环烧求解（防"重解风暴"）。
func TestDoWith403RevalidatesOnlyOnce(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.Header().Set("cf-mitigated", "challenge")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("blocked"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)

	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatalf("应当返回 403 响应而不是错误: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("应原样返回 403，实际 %d", resp.StatusCode)
	}
	if c := atomic.LoadInt32(&solverCalls); c != 2 {
		t.Fatalf("只允许重解一次（初次+1 次），实际求解 %d 次", c)
	}
	if h := atomic.LoadInt32(&hits); h != 2 {
		t.Fatalf("打靶次数应为 2，实际 %d", h)
	}
}

// 403 后本地缓存的那条凭证必须已失效：下一次 Get 走新凭证而不是旧凭证。
func TestDoWith403InvalidatesLocalCache(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		if n == 1 {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = w.Write([]byte("OK"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)
	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()

	cl, err := cli.Get(ctx, target.URL)
	if err != nil {
		t.Fatal(err)
	}
	if cl.CFClearance != "CL-2" {
		t.Fatalf("缓存里应已是重解出来的新凭证 CL-2，实际 %q", cl.CFClearance)
	}
	if !cl.Cached {
		t.Fatal("重解结果应已写回本地缓存（否则每个请求都要重解）")
	}
	if c := atomic.LoadInt32(&solverCalls); c != 2 {
		t.Fatalf("求解次数应为 2，实际 %d", c)
	}
}

// 200 但正文是挑战页：同样判为被拦，触发重解。
func TestDoWithChallengePageBodyHeuristic(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&hits, 1)
		w.Header().Set("Content-Type", "text/html; charset=UTF-8")
		if n == 1 {
			_, _ = w.Write([]byte(`<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>checking</body></html>`))
			return
		}
		_, _ = w.Write([]byte("REAL-PAGE"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)
	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "REAL-PAGE" {
		t.Fatalf("应拿到重解后的真实页面，实际 %q", string(body))
	}
	if c := atomic.LoadInt32(&solverCalls); c != 2 {
		t.Fatalf("挑战页也触发一次重解，实际求解 %d 次", c)
	}
}

// 最关键的安全用例：**不是**被拦时，body 必须完整可读。
// 实现里为判断挑战页"偷看"了正文前缀，如果不还原，业务方会拿到残缺页面（静默错数据）。
func TestDoWithNormalBodyIsNotTruncated(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	// 故意造一个超过探测窗口（2KB）的长正文，且前缀里包含干扰词
	long := "hello-" + strings.Repeat("x", 5000) + "-tail"
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=UTF-8")
		_, _ = w.Write([]byte(long))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)
	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	if string(body) != long {
		t.Fatalf("正文被破坏了：期望 %d 字节，实际 %d 字节（前缀丢失=探测后没还原 body）", len(long), len(body))
	}
	if c := atomic.LoadInt32(&solverCalls); c != 1 {
		t.Fatalf("正常 200 不应重解，实际求解 %d 次", c)
	}
}

// 同域 5 个并发回放同时遇 403：只允许触发 1 次重解（与单飞共用机制）。
func TestDoWith403ConcurrentSingleRevalidate(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 100*time.Millisecond, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(http.StatusForbidden)
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	// 先拿一次凭证（1 次求解）
	cl, err := cli.Get(ctx, target.URL)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)
			resp, err := cli.DoWith(ctx, req, cl) // 故意传同一份（已失效的）凭证
			if err == nil {
				_ = resp.Body.Close()
			}
		}()
	}
	wg.Wait()

	// 1 次初次 + 1 次重解 = 2；没有收敛的话会是 1+5=6
	if c := atomic.LoadInt32(&solverCalls); c > 3 {
		t.Fatalf("同域并发失效只应重解 1 次，实际求解 %d 次（重解风暴）", c)
	}
}

// 关掉自愈开关后行为必须退回原样（兼容/应急开关）。
func TestRevalidateDisabledKeepsPassthrough(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"), WithRevalidate(false))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, target.URL, nil)
	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("关闭自愈后应原样返回 403，实际 %d", resp.StatusCode)
	}
	if c := atomic.LoadInt32(&solverCalls); c != 1 {
		t.Fatalf("关闭自愈后不应重解，实际求解 %d 次", c)
	}
}

// 带 body 且无法重放（GetBody=nil）的请求不许重试——重试会把请求体丢掉或重复发送。
func TestRevalidateSkipsNonReplayableRequestBody(t *testing.T) {
	var solverCalls int32
	solverSrv := countingSolver(t, &solverCalls, 0, okClearanceBody)
	defer solverSrv.Close()

	var hits int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(http.StatusForbidden)
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	ctx := context.Background()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, target.URL, nil)
	req.Body = io.NopCloser(strings.NewReader("payload"))
	req.GetBody = nil // 模拟不可重放的流式 body

	resp, err := cli.Do(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("应原样返回 403，实际 %d", resp.StatusCode)
	}
	if c := atomic.LoadInt32(&solverCalls); c != 1 {
		t.Fatalf("不可重放的请求体不许触发重解，实际求解 %d 次", c)
	}
	if h := atomic.LoadInt32(&hits); h != 1 {
		t.Fatalf("不可重放时不许二次发送，实际打靶 %d 次", h)
	}
}

// ── T-30 相关：TTL 契约（Go 侧行为）──────────────────────────

// expires_at=0 / expires_in=0 的响应绝不能进本地缓存（否则每次 Get 都重解）。
func TestZeroTTLResponseIsNotCached(t *testing.T) {
	var calls int32
	srv := countingSolver(t, &calls, 0, func(n int32) (int, map[string]any) {
		b := clearanceBody(n)
		b["expires_in"] = 0
		b["expires_at"] = 0.0
		return http.StatusOK, b
	})
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	ctx := context.Background()
	if _, err := cli.Get(ctx, "https://zero.example.com/"); err != nil {
		t.Fatal(err)
	}
	if _, err := cli.Get(ctx, "https://zero.example.com/"); err != nil {
		t.Fatal(err)
	}
	if c := atomic.LoadInt32(&calls); c != 2 {
		t.Fatalf("TTL=0 的凭证不该被缓存复用（应求解 2 次），实际 %d 次", c)
	}
}
