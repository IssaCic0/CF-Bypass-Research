package cfsolve

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"

// fakeSolver 起一个假的求解服务，返回固定 clearance；calls 记录被调用次数。
func fakeSolver(t *testing.T, calls *int32, body func() map[string]any, status int) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(calls, 1)
		if r.URL.Path == "/stats" || r.URL.Path == "/health" {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "browser": "camoufox"})
			return
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"ok": false, "error_code": "unauthorized", "error": "缺少或错误的 Bearer token", "stage": "admit",
			})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body())
	}))
}

func okBody() map[string]any {
	return map[string]any{
		"ok":           true,
		"solved":       true,
		"cf_clearance": "CLEARANCE",
		"user_agent":   testUA,
		"cookies":      map[string]string{"cf_clearance": "CLEARANCE"},
		"cookie_list": []map[string]any{
			{"name": "cf_clearance", "value": "CLEARANCE", "domain": ".example.com", "path": "/", "expires": 1},
		},
		"expires_in": 1500,
		"expires_at": float64(time.Now().Add(20 * time.Minute).Unix()),
		"egress_id":  "default",
	}
}

func TestGetSendsBearerTokenAndParsesClearance(t *testing.T) {
	var calls int32
	srv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	cl, err := cli.Get(context.Background(), "https://www.example.com/")
	if err != nil {
		t.Fatalf("Get 失败: %v", err)
	}
	if cl.CFClearance != "CLEARANCE" || cl.UserAgent != testUA {
		t.Fatalf("解析结果不对: %+v", cl)
	}
	if len(cl.CookieList) != 1 || cl.CookieList[0].Domain != ".example.com" {
		t.Fatalf("cookie_list 未解析: %+v", cl.CookieList)
	}
}

func TestMissingTokenSurfacesAPIError(t *testing.T) {
	var calls int32
	srv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer srv.Close()

	cli := New(srv.URL) // 不带 token
	_, err := cli.Get(context.Background(), "https://www.example.com/")
	if err == nil {
		t.Fatal("应当返回 401 错误")
	}
	var apiErr *APIError
	if !asAPIError(err, &apiErr) {
		t.Fatalf("错误类型应为 *APIError，实际 %T: %v", err, err)
	}
	if apiErr.StatusCode != http.StatusUnauthorized || apiErr.Code != "unauthorized" {
		t.Fatalf("错误码不对: %+v", apiErr)
	}
}

func TestUnsolvedIsHTTP200WithErrorCode(t *testing.T) {
	var calls int32
	srv := fakeSolver(t, &calls, func() map[string]any {
		return map[string]any{
			"ok": false, "solved": false, "error_code": "unsolved",
			"error": "45s 内未取得 cf_clearance", "stage": "wait", "hint": "该域可能已升级",
		}
	}, http.StatusOK)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	_, err := cli.Get(context.Background(), "https://www.example.com/")
	var apiErr *APIError
	if !asAPIError(err, &apiErr) {
		t.Fatalf("应返回 *APIError，实际 %T: %v", err, err)
	}
	if apiErr.Code != "unsolved" || apiErr.Hint == "" {
		t.Fatalf("求解失败必须带 error_code 与 hint: %+v", apiErr)
	}
	if !strings.Contains(err.Error(), "建议") {
		t.Fatalf("错误信息应包含可执行建议: %v", err)
	}
}

func TestCacheHitSkipsSecondSolve(t *testing.T) {
	var calls int32
	srv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer srv.Close()

	cli := New(srv.URL, WithToken("test-token"))
	ctx := context.Background()
	if _, err := cli.Get(ctx, "https://www.example.com/"); err != nil {
		t.Fatal(err)
	}
	cl2, err := cli.Get(ctx, "https://www.example.com/")
	if err != nil {
		t.Fatal(err)
	}
	if !cl2.Cached {
		t.Fatal("第二次应命中本地缓存")
	}
	if cl2.Elapsed != 0 {
		t.Fatalf("缓存命中时 Elapsed 必须为 0，实际 %v", cl2.Elapsed)
	}
	if n := atomic.LoadInt32(&calls); n != 1 {
		t.Fatalf("缓存命中不应再调求解服务，实际调用 %d 次", n)
	}
}

func TestEgressIDSeparatesCache(t *testing.T) {
	var calls int32
	srv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer srv.Close()

	ctx := context.Background()
	a := New(srv.URL, WithToken("test-token"), WithEgressID("proxy-a"))
	b := New(srv.URL, WithToken("test-token"), WithEgressID("proxy-b"))
	if _, err := a.Get(ctx, "https://www.example.com/"); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Get(ctx, "https://www.example.com/"); err != nil {
		t.Fatal(err)
	}
	if n := atomic.LoadInt32(&calls); n != 2 {
		t.Fatalf("不同出口必须各解一次（否则会把 A 出口的凭证发给 B 出口），实际 %d 次", n)
	}
}

func TestCookieHeaderForFiltersDomainAndPutsClearanceFirst(t *testing.T) {
	cl := &Clearance{
		CFClearance: "C",
		CookieList: []Cookie{
			{Name: "__cf_bm", Value: "bm", Domain: ".example.com"},
			{Name: "zzz", Value: "z", Domain: ".example.com"},
			{Name: "cf_clearance", Value: "C", Domain: ".example.com"},
			{Name: "_ga", Value: "should-be-dropped", Domain: ".other.com"},
		},
	}
	got, err := cl.CookieHeaderFor("www.example.com")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(got, "_ga") {
		t.Fatalf("不该把别的域的 cookie 发出去: %s", got)
	}
	if !strings.HasPrefix(got, "cf_clearance=C") {
		t.Fatalf("cf_clearance 必须排首位: %s", got)
	}
	// 多次调用必须完全一致（原来遍历 map，顺序随机）
	for i := 0; i < 5; i++ {
		again, _ := cl.CookieHeaderFor("www.example.com")
		if again != got {
			t.Fatalf("cookie 头顺序不稳定: %q vs %q", got, again)
		}
	}
}

func TestDoWithHonorsContextDeadline(t *testing.T) {
	var calls int32
	solverSrv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer solverSrv.Close()

	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(2 * time.Second)
		_, _ = w.Write([]byte("late"))
	}))
	defer slow.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	cl, err := cli.Get(context.Background(), slow.URL)
	if err != nil {
		t.Fatal(err)
	}
	cl.CookieList = []Cookie{{Name: "cf_clearance", Value: "CLEARANCE", Path: "/"}}

	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, slow.URL, nil)

	start := time.Now()
	_, err = cli.DoWith(ctx, req, cl)
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("ctx 到期应当返回错误")
	}
	if elapsed > 1500*time.Millisecond {
		t.Fatalf("回放没有遵守 ctx 超时（用了 %v），说明绕过了自身 client 配置", elapsed)
	}
}

func TestDoWithSetsFirefoxConsistentHeaders(t *testing.T) {
	var calls int32
	solverSrv := fakeSolver(t, &calls, okBody, http.StatusOK)
	defer solverSrv.Close()

	var got http.Header
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		_, _ = w.Write([]byte("ok"))
	}))
	defer target.Close()

	cli := New(solverSrv.URL, WithToken("test-token"))
	cl, err := cli.Get(context.Background(), target.URL)
	if err != nil {
		t.Fatal(err)
	}
	// httptest 的 host 是 127.0.0.1；把 cookie 的 domain 置空表示"不过滤"
	cl.CookieList = []Cookie{{Name: "cf_clearance", Value: "CLEARANCE", Path: "/"}}
	req, _ := http.NewRequest(http.MethodGet, target.URL, nil)
	resp, err := cli.DoWith(context.Background(), req, cl)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if got.Get("User-Agent") != testUA {
		t.Fatalf("回放必须用求解时的 UA，实际 %q", got.Get("User-Agent"))
	}
	if got.Get("Cookie") == "" {
		t.Fatal("回放必须带 Cookie")
	}
	for _, h := range []string{"Upgrade-Insecure-Requests", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site"} {
		if got.Get(h) == "" {
			t.Fatalf("缺少 Firefox 侧会发的头 %s（UA 说 Firefox 却没有这些头 = 自相矛盾）", h)
		}
	}
	if got.Get("Accept") == "" || got.Get("Accept-Language") == "" {
		t.Fatal("Accept / Accept-Language 应设置")
	}
}

// asAPIError 是 errors.As 的薄封装，避免测试文件再 import errors 造成噪音。
func asAPIError(err error, target **APIError) bool {
	for e := err; e != nil; {
		if ae, ok := e.(*APIError); ok {
			*target = ae
			return true
		}
		type unwrapper interface{ Unwrap() error }
		u, ok := e.(unwrapper)
		if !ok {
			return false
		}
		e = u.Unwrap()
	}
	return false
}


// 同名 cookie 只能发一个，且保留更具体的 domain（Critic 抓到的 ⑤）。
func TestCookieHeaderDeduplicatesSameNameKeepingMostSpecific(t *testing.T) {
	cl := &Clearance{
		CFClearance: "HOSTONLY",
		CookieList: []Cookie{
			{Name: "cf_clearance", Value: "PARENT", Domain: ".example.com"},
			{Name: "cf_clearance", Value: "HOSTONLY", Domain: "www.example.com"},
			{Name: "__cf_bm", Value: "bm", Domain: ".example.com"},
		},
	}
	got, err := cl.CookieHeaderFor("www.example.com")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(got, "cf_clearance=") != 1 {
		t.Fatalf("同名 cookie 不能发两份: %s", got)
	}
	if !strings.HasPrefix(got, "cf_clearance=HOSTONLY") {
		t.Fatalf("应保留 domain 更具体的那条: %s", got)
	}
	// 顺序必须稳定（比较器改成严格弱序后不再反转）
	for i := 0; i < 10; i++ {
		again, _ := cl.CookieHeaderFor("www.example.com")
		if again != got {
			t.Fatalf("顺序不稳定: %q vs %q", got, again)
		}
	}
}

// 没有任何同域 cookie 时必须报错，而不是发一个空 Cookie 头去撞墙。
func TestCookieHeaderForErrorsWhenNoCookieMatchesHost(t *testing.T) {
	cl := &Clearance{CookieList: []Cookie{{Name: "cf_clearance", Value: "X", Domain: ".other.com"}}}
	if _, err := cl.CookieHeaderFor("www.example.com"); err == nil {
		t.Fatal("应当返回错误")
	}
}
