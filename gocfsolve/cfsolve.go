// Package cfsolve 是 Cloudflare cf_clearance 求解服务的 Go 客户端。
//
// 职责边界（本仓库最终方案的 Go 侧全部实现）：
//   - 调 solver/service.py（Camoufox）取 cf_clearance
//   - 按域名缓存 + 提前续期（cf_clearance 天生绑 IP+UA，缓存是语义正确的）
//   - 回放：把 cookie + 求解时的 UA 套到一个 http.Request 上，用标准库发出
//
// 为什么不用 uTLS：实测该批站点不严查 JA3（curl_cffi firefox/safari/chrome 三指纹均 200），
// 标准库 net/http 已足够。遇到严查 JA3 的站点再开 uTLS 兜底，不作为默认依赖。
//
// 四条硬边界（违反即失效）：同出口 IP · 同 UA · 约 30 分钟时效 · 按域隔离。
package cfsolve

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Clearance 一次求解结果。
type Clearance struct {
	CFClearance string            `json:"cf_clearance"`
	UserAgent   string            `json:"user_agent"`
	Cookies     map[string]string `json:"cookies"`
	ExpiresIn   int               `json:"expires_in"`
	ExpiresAt   float64           `json:"expires_at"`
	Elapsed     float64           `json:"elapsed"`
	Cached      bool              `json:"cached"`
}

func (c *Clearance) expiresAt() time.Time {
	if c.ExpiresAt > 0 {
		return time.Unix(int64(c.ExpiresAt), 0)
	}
	return time.Now().Add(time.Duration(c.ExpiresIn) * time.Second)
}

// CookieHeader 拼出可直接用的 Cookie 头。
func (c *Clearance) CookieHeader() string {
	var b strings.Builder
	for k, v := range c.Cookies {
		if b.Len() > 0 {
			b.WriteString("; ")
		}
		b.WriteString(k)
		b.WriteString("=")
		b.WriteString(v)
	}
	return b.String()
}

type solveReq struct {
	URL     string  `json:"url"`
	Timeout float64 `json:"timeout,omitempty"`
	Force   bool    `json:"force,omitempty"`
}

type solveResp struct {
	Ok      bool   `json:"ok"`
	Error   string `json:"error"`
	Elapsed float64 `json:"elapsed"`
	Clearance
}

// Client 求解服务客户端（并发安全）。
type Client struct {
	baseURL string
	hc      *http.Client
	// renewBefore 提前续期窗口：到期前多久主动刷新（避免请求打到过期 cookie）。
	renewBefore time.Duration

	mu    sync.Mutex
	cache map[string]*Clearance // host -> clearance
	// solveTimeout 单次求解超时（camoufox 单次 5–20s，给足余量）。
	solveTimeout time.Duration
}

// New 创建客户端。baseURL 形如 http://127.0.0.1:8878。
func New(baseURL string) *Client {
	return &Client{
		baseURL:      strings.TrimSuffix(baseURL, "/"),
		hc:           &http.Client{Timeout: 120 * time.Second},
		renewBefore:  5 * time.Minute,
		cache:        map[string]*Clearance{},
		solveTimeout: 60 * time.Second,
	}
}

// Get 取 clearance（优先缓存，快到期自动刷新）。
func (c *Client) Get(ctx context.Context, target string) (*Clearance, error) {
	host := hostOf(target)
	if host == "" {
		return nil, fmt.Errorf("无法解析域名: %s", target)
	}

	c.mu.Lock()
	cached := c.cache[host]
	c.mu.Unlock()

	if cached != nil && time.Now().Add(c.renewBefore).Before(cached.expiresAt()) {
		// 命中本地缓存：复制一份并标记，避免调用方误以为又启动了一次浏览器
		hit := *cached
		hit.Cached = true
		return &hit, nil
	}
	// force=false：允许求解服务返回它自己的域名缓存（省掉一次浏览器求解）
	return c.solve(ctx, target, false)
}

// GetFresh 强制重新求解（忽略本地缓存与服务端缓存）。
func (c *Client) GetFresh(ctx context.Context, target string) (*Clearance, error) {
	return c.solve(ctx, target, true)
}

// solve 是 Get/GetFresh 的共同实现。force=false 时服务端可返回它自己的缓存。
func (c *Client) solve(ctx context.Context, target string, force bool) (*Clearance, error) {
	host := hostOf(target)
	body, err := json.Marshal(solveReq{URL: target, Timeout: c.solveTimeout.Seconds(), Force: force})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/solve", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("调用求解服务失败: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	var sr solveResp
	if err := json.Unmarshal(raw, &sr); err != nil {
		return nil, fmt.Errorf("解析求解响应失败: %w（body: %s）", err, truncate(string(raw), 200))
	}
	if !sr.Ok {
		return nil, fmt.Errorf("求解失败: %s", sr.Error)
	}
	if sr.CFClearance == "" {
		return nil, fmt.Errorf("求解服务未返回 cf_clearance")
	}

	cl := &Clearance{
		CFClearance: sr.CFClearance,
		UserAgent:   sr.UserAgent,
		Cookies:     sr.Cookies,
		ExpiresIn:   sr.ExpiresIn,
		ExpiresAt:   sr.ExpiresAt,
		Elapsed:     sr.Elapsed,
		Cached:      sr.Cached, // 服务端缓存命中时为 true
	}
	c.mu.Lock()
	c.cache[host] = cl
	c.mu.Unlock()
	return cl, nil
}

// Do 用求解得到的 UA + Cookie 回放一个请求。
//
// 会覆盖 req 的 User-Agent 与 Cookie 头——这是必须的，cf_clearance 绑 UA，换了 UA 即失效。
func (c *Client) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	target := req.URL.String()
	cl, err := c.Get(ctx, target)
	if err != nil {
		return nil, err
	}
	return c.DoWith(ctx, req, cl)
}

// DoWith 用指定的 clearance 回放（便于外部自行管理缓存）。
func (c *Client) DoWith(ctx context.Context, req *http.Request, cl *Clearance) (*http.Response, error) {
	if req.Header == nil {
		req.Header = http.Header{}
	}
	req.Header.Set("User-Agent", cl.UserAgent)
	req.Header.Set("Cookie", cl.CookieHeader())
	if req.Header.Get("Accept") == "" {
		req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	}
	if req.Header.Get("Accept-Language") == "" {
		req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	}
	return http.DefaultClient.Do(req.WithContext(ctx))
}

// Stats 透传求解服务的统计。
func (c *Client) Stats(ctx context.Context) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/stats", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out map[string]any
	err = json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out)
	return out, err
}

func hostOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return strings.ToLower(u.Hostname())
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
