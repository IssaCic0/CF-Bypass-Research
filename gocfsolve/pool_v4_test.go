package cfsolve

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// 池侧的三个"把好节点误杀"与"看不见真故障"的问题（v4.0 T-07b）：
//   1) 终止性错误（参数写错/未鉴权/队列满）不是节点的锅，不能记成节点故障；
//   2) 探活只认 err != nil，服务端"503 + ready:false"（浏览器死了）反而什么都不做；
//   3) 回放 403 后仍给节点记 OK，导致凭证已死的节点在池视图里永远健康。

// 固定状态码的假节点：status 决定一切；healthMode 决定 /health 的行为。
type fixedNode struct {
	*httptest.Server
	calls atomic.Int32
}

func newFixedNode(t *testing.T, solveStatus int, solveBody map[string]any, healthStatus int, healthBody map[string]any) *fixedNode {
	t.Helper()
	n := &fixedNode{}
	n.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/health" {
			w.WriteHeader(healthStatus)
			_ = json.NewEncoder(w).Encode(healthBody)
			return
		}
		w.WriteHeader(solveStatus)
		_ = json.NewEncoder(w).Encode(solveBody)
	}))
	return n
}

func goodClearance(n int32) map[string]any {
	return clearanceBody(n)
}

// 终止性错误（401）既不该摘除节点，也不该 failover 到其它节点。
func TestPoolTerminalErrorIsNotNodeFault(t *testing.T) {
	bad := newFixedNode(t, http.StatusUnauthorized,
		map[string]any{"ok": false, "error_code": "unauthorized", "error": "缺少或错误的 Bearer token", "stage": "admit"},
		http.StatusOK, map[string]any{"ok": true, "state": "ready"})
	defer bad.Close()
	other := newFixedNode(t, http.StatusOK, goodClearance(1), http.StatusOK, map[string]any{"ok": true, "state": "ready"})
	defer other.Close()

	p, err := NewPool([]string{bad.URL, other.URL}, WithPoolMaxFailures(2), WithPoolToken("test-token"))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	// 找一个"主节点就是 bad"的域，才能确定性地验证"没有 failover"
	host := ""
	for i := 0; i < 500; i++ {
		h := "t" + string(rune('a'+i%26)) + string(rune('a'+(i/26)%26)) + ".example.com"
		if node := p.pick(h); node != nil && node.base == bad.URL {
			host = h
			break
		}
	}
	if host == "" {
		t.Skip("无法构造出主节点为 bad 的域，跳过（哈希分布问题）")
	}

	_, err = p.Get(context.Background(), "https://"+host+"/")
	if err == nil {
		t.Fatal("401 应上抛错误")
	}
	var ae *APIError
	if !asAPIError(err, &ae) || ae.Code != "unauthorized" {
		t.Fatalf("应返回 unauthorized 的 *APIError，实际 %v", err)
	}

	// 关键断言 1：终止性错误不 failover（换谁都是 401，只浪费一次往返）
	if c := other.calls.Load(); c != 0 {
		t.Fatalf("终止性错误不该 failover，其它节点被打 %d 次", c)
	}
	// 关键断言 2：节点没被记成故障
	for _, nh := range p.Nodes() {
		if nh.BaseURL == bad.URL && nh.Failures != 0 {
			t.Fatalf("401 不是节点的锅，却记了 %d 次失败（会把好节点摘掉）", nh.Failures)
		}
	}
}

// 429/过载：要 failover，但不能把节点记成故障（它只是忙）。
func TestPoolSaturationDoesNotMarkNodeDown(t *testing.T) {
	busy := newFixedNode(t, http.StatusTooManyRequests,
		map[string]any{"ok": false, "error_code": "busy", "error": "并发求解数已达上限", "stage": "admit"},
		http.StatusOK, map[string]any{"ok": true, "state": "ready"})
	defer busy.Close()

	p, err := NewPool([]string{busy.URL}, WithPoolMaxFailures(1), WithPoolToken("test-token"))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	if _, err := p.Get(context.Background(), "https://x.example.com/"); err == nil {
		t.Fatal("429 应当上抛 busy")
	}
	nh := p.Nodes()[0]
	if !nh.Healthy {
		t.Fatalf("过载不是节点故障，不该被摘除（failures=%d last_error=%q）", nh.Failures, nh.LastError)
	}
	if nh.Failures != 0 {
		t.Fatalf("过载不该累加失败计数，实际 %d", nh.Failures)
	}
}

// 探活必须能发现"HTTP 200/503 + ready:false"这种最该发现的故障。
func TestPoolHealthProbeMarksNotReadyNode(t *testing.T) {
	// 服务端"浏览器死了"的表现：503 + 合法 JSON（ready=false），HTTP 层完全成功
	dead := newFixedNode(t, http.StatusServiceUnavailable,
		map[string]any{"ok": false, "error_code": "browser-unavailable", "error": "down", "stage": "admit"},
		http.StatusServiceUnavailable,
		map[string]any{"ok": true, "state": "degraded", "browser_alive": false})
	defer dead.Close()

	p, err := NewPool([]string{dead.URL},
		WithPoolMaxFailures(1), WithPoolToken("test-token"),
		WithPoolHealthProbe(50*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if nh := p.Nodes()[0]; !nh.Healthy {
			return // 探活把它标记了，符合预期
		}
		time.Sleep(25 * time.Millisecond)
	}
	nh := p.Nodes()[0]
	t.Fatalf("探活对 ready=false 完全无效：节点仍被判为健康（failures=%d last_error=%q）", nh.Failures, nh.LastError)
}

// 回放 403 = 凭证已死，节点不该被记成 OK。
func TestPoolDo403DropsStaleCredentialWithoutBlamingNode(t *testing.T) {
	node := newFixedNode(t, http.StatusOK, goodClearance(1),
		http.StatusOK, map[string]any{"ok": true, "state": "ready"})
	defer node.Close()

	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer target.Close()

	// 关掉客户端自愈，单独验证池这一层的行为：403 之后凭证必须被丢掉。
	p, err := NewPool([]string{node.URL},
		WithPoolMaxFailures(1), WithPoolToken("test-token"),
		WithPoolClientOptions(WithRevalidate(false)))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	for i := 0; i < 2; i++ {
		req, _ := http.NewRequestWithContext(context.Background(), http.MethodGet, target.URL, nil)
		resp, err := p.Do(context.Background(), req)
		if err != nil {
			t.Fatalf("第 %d 次 Do 应当返回 403 响应而不是错误: %v", i+1, err)
		}
		_ = resp.Body.Close()
	}

	// 关键断言 1：节点**仍然健康** —— 403 是凭证的问题（也可能是目标站的业务封锁），
	// 不该把健康的求解器摘掉（那会让一组好节点被两次业务 403 全部下线）。
	if nh := p.Nodes()[0]; !nh.Healthy {
		t.Fatalf("节点被误判为故障（failures=%d last_error=%q）——403 不该记成节点故障",
			nh.Failures, nh.LastError)
	}
	// 关键断言 2：凭证被丢掉并重解了。旧实现无条件 markOK 且不清缓存，
	// 于是失效的凭证一直被复用，第二次 Do 不会产生新的求解（calls 停在 1）。
	if c := node.calls.Load(); c != 2 {
		t.Fatalf("403 应使该域凭证失效并重解一次：期望累计 2 次求解，实际 %d 次", c)
	}
}
