package cfsolve

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// poolNodeServer 起一个可切换"健康/故障"的假 solver。
type poolNodeServer struct {
	*httptest.Server
	broken atomic.Bool
	calls  atomic.Int32
	name   string
}

func newPoolNodeServer(t *testing.T, name string) *poolNodeServer {
	t.Helper()
	n := &poolNodeServer{name: name}
	n.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n.calls.Add(1)
		if n.broken.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"ok": false, "error_code": "browser-unavailable", "error": "down", "stage": "admit",
			})
			return
		}
		if r.URL.Path == "/health" {
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "state": "ready"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok": true, "solved": true,
			"cf_clearance": "C-" + name,
			"user_agent":   testUA,
			"cookie_list":  []map[string]any{{"name": "cf_clearance", "value": "C-" + name, "domain": ""}},
			"expires_in":   1500,
			"expires_at":   float64(time.Now().Add(20 * time.Minute).Unix()),
			"egress_id":    "default",
		})
	}))
	return n
}

func hostsOf(t *testing.T, p *Pool, n int) map[int][]string {
	t.Helper()
	out := map[int][]string{}
	for i := 0; i < n; i++ {
		host := "site" + strconv.Itoa(i) + ".example.com"
		node := p.pick(host)
		if node == nil {
			t.Fatalf("pick(%s) 返回 nil", host)
		}
		idx := -1
		for j, cand := range p.nodes {
			if cand == node {
				idx = j
			}
		}
		out[idx] = append(out[idx], host)
	}
	return out
}

func TestNewPoolRequiresNodes(t *testing.T) {
	if _, err := NewPool(nil); err == nil {
		t.Fatal("空节点列表应当报错")
	}
	if _, err := NewPool([]string{"", "/"}); err == nil {
		t.Fatal("无效节点应当报错")
	}
}

func TestPoolStickinessAndDistribution(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	b := newPoolNodeServer(t, "b")
	defer a.Close()
	defer b.Close()

	p, err := NewPool([]string{a.URL, b.URL})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	first := p.pick("www.example.com")
	for i := 0; i < 20; i++ {
		if got := p.pick("www.example.com"); got != first {
			t.Fatal("同域必须粘住同一个节点")
		}
	}

	dist := hostsOf(t, p, 200)
	if len(dist) != 2 {
		t.Fatalf("200 个域应分散到 2 个节点，实际分布：%v", dist)
	}
}

func TestPoolFailsOverToSecondNode(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	b := newPoolNodeServer(t, "b")
	defer a.Close()
	defer b.Close()
	a.broken.Store(true)

	p, err := NewPool([]string{a.URL, b.URL}, WithPoolMaxFailures(1), WithPoolDownDuration(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	// 找一个粘在 a 上的域
	var target string
	for i := 0; i < 200; i++ {
		h := "site" + strconv.Itoa(i) + ".example.com"
		if p.pick(h) == p.nodes[0] {
			target = "https://" + h
			break
		}
	}
	if target == "" {
		t.Skip("这个哈希分布下没有粘到节点 a 的域，跳过")
	}

	cl, err := p.Get(context.Background(), target)
	if err != nil {
		t.Fatalf("首个节点失败时应当 failover 到另一个节点: %v", err)
	}
	if cl.CFClearance != "C-b" {
		t.Fatalf("应由健康节点 b 提供凭证，实际 %q", cl.CFClearance)
	}

	health := p.Nodes()
	if health[0].Healthy {
		t.Fatal("连续失败达阈值后节点应被摘除")
	}
	if health[0].Failures == 0 {
		t.Fatal("失败计数应被记录")
	}
}

func TestPoolNodeRecoversAfterDownDuration(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	b := newPoolNodeServer(t, "b")
	defer a.Close()
	defer b.Close()

	p, err := NewPool([]string{a.URL, b.URL}, WithPoolMaxFailures(1), WithPoolDownDuration(80*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	p.nodes[0].markFail(errBoom{}, 1, 80*time.Millisecond)
	if p.nodes[0].healthy(time.Now()) {
		t.Fatal("刚摘除时应为不健康")
	}
	time.Sleep(120 * time.Millisecond)
	if !p.nodes[0].healthy(time.Now()) {
		t.Fatal("冷却结束后必须自动回归（不做永久黑名单）")
	}
}

type errBoom struct{}

func (errBoom) Error() string { return "boom" }

func TestPoolReportsCooldownWhenAllDown(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	defer a.Close()
	p, err := NewPool([]string{a.URL}, WithPoolMaxFailures(1), WithPoolDownDuration(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	p.nodes[0].markFail(errBoom{}, 1, time.Minute)
	_, err = p.Get(context.Background(), "https://example.com/")
	if err == nil {
		t.Fatal("所有节点冷却中应返回错误")
	}
	if !strings.Contains(err.Error(), "冷却") {
		t.Fatalf("错误信息应说明是在冷却，实际：%v", err)
	}
}

func TestPoolDoReplaysWithOwningNode(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	defer a.Close()

	var gotUA string
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		_, _ = w.Write([]byte("ok"))
	}))
	defer target.Close()

	p, err := NewPool([]string{a.URL})
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close()

	req, _ := http.NewRequest(http.MethodGet, target.URL, nil)
	resp, err := p.Do(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if gotUA != testUA {
		t.Fatalf("回放必须用求解时的 UA，实际 %q", gotUA)
	}
}

func TestPoolHealthProbeMarksNodes(t *testing.T) {
	a := newPoolNodeServer(t, "a")
	defer a.Close()

	p, err := NewPool([]string{a.URL}, WithPoolHealthProbe(20*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(80 * time.Millisecond)
	p.Close()

	before := a.calls.Load()
	if before == 0 {
		t.Fatal("开启探活后应当有 /health 请求")
	}
	if !p.Nodes()[0].Healthy {
		t.Fatal("健康节点不应被摘除")
	}
}
