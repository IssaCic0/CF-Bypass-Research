// Package cfsolve 是 Cloudflare cf_clearance 求解服务的 Go 客户端。
//
// 职责边界（本仓库最终方案的 Go 侧全部实现）：
//   - 调 solver/service.py（Camoufox）取 cf_clearance
//   - 按域名+出口标识缓存 + 提前续期（cf_clearance 天生绑 IP+UA，缓存键必须带出口标识）
//   - 回放：把 cookie + 求解时的 UA 套到一个 http.Request 上，用自己的连接池发出
//
// 为什么不用 uTLS：已验证 steamdb.info 这一个站在同 cookie/同 UA 下，
// curl_cffi 的 firefox/safari/chrome 三种 TLS 指纹回放均 200（n=1，见 docs/ 报告 §11）。
// 这只证明"该站不严查 JA3"，**不能外推为"所有站点都不查"**；遇到严格校验的站点再开兜底，
// 且兜底应覆盖 TLS 与 HTTP/2 两条独立指纹轴（裸 uTLS 只覆盖前者），不作为默认依赖。
//
// 四条硬边界（违反即失效）：同出口 IP · 同 UA · 约 30 分钟时效 · 按域隔离。
package cfsolve

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// Cookie 是结构化 cookie（含 domain/path/expires），优先于压缩后的 map 使用。
type Cookie struct {
	Name     string  `json:"name"`
	Value    string  `json:"value"`
	Domain   string  `json:"domain"`
	Path     string  `json:"path"`
	Expires  float64 `json:"expires"`
	HTTPOnly bool    `json:"http_only"`
	Secure   bool    `json:"secure"`
	SameSite string  `json:"same_site"`
}

// Clearance 一次求解结果。
type Clearance struct {
	CFClearance string            `json:"cf_clearance"`
	UserAgent   string            `json:"user_agent"`
	Cookies     map[string]string `json:"cookies"`     // deprecated：仅为兼容旧服务端
	CookieList  []Cookie          `json:"cookie_list"` // 结构化，优先使用
	ExpiresIn   int               `json:"expires_in"`
	ExpiresAt   float64           `json:"expires_at"`
	Elapsed     float64           `json:"elapsed"`
	Cached      bool              `json:"cached"`
	EgressID    string            `json:"egress_id"`
}

func (c *Clearance) expiresAt() time.Time {
	if c.ExpiresAt > 0 {
		return time.Unix(int64(c.ExpiresAt), 0)
	}
	return time.Now().Add(time.Duration(c.ExpiresIn) * time.Second)
}

// CookieHeader 拼出 Cookie 头（不按域名过滤，保留给旧调用方）。
func (c *Clearance) CookieHeader() string {
	h, _ := c.CookieHeaderFor("")
	return h
}

// CookieHeaderFor 拼出适用于 host 的 Cookie 头。
//
// 与原实现的区别（都是实测会咬人的点）：
//   - 按 domain 过滤：求解用的 context 会顺带拿到 challenges.cloudflare.com 等第三方 cookie，
//     无差别全发会给目标站一串不属于它的 cookie（异常指纹信号）。
//   - cf_clearance 恒排首位，其余按名称排序：原来直接遍历 map，每次请求顺序都不同。
func (c *Clearance) CookieHeaderFor(host string) (string, error) {
	if len(c.CookieList) > 0 {
		host = strings.ToLower(host)
		// 同名 cookie 只能发一个：CF 常见 host-only（www.example.com）与父域（.example.com）
		// 各下发一份 cf_clearance，两条都发会让服务端看到重复字段。
		// 规则：保留 domain 更具体的那条（host-only > 父域）。
		type kv struct {
			name, value, domain string
		}
		best := make(map[string]kv, len(c.CookieList))
		for _, ck := range c.CookieList {
			if host != "" && ck.Domain != "" && !domainMatches(host, ck.Domain) {
				continue
			}
			cur, seen := best[ck.Name]
			if !seen || domainSpecificity(ck.Domain) > domainSpecificity(cur.domain) {
				best[ck.Name] = kv{ck.Name, ck.Value, ck.Domain}
			}
		}
		picked := make([]kv, 0, len(best))
		for _, v := range best {
			picked = append(picked, v)
		}
		// 严格弱序：cf_clearance 恒在前，其余按名称；名称已去重，故不会出现 less(i,i)==true
		sort.SliceStable(picked, func(i, j int) bool {
			iCF := picked[i].name == "cf_clearance"
			jCF := picked[j].name == "cf_clearance"
			if iCF != jCF {
				return iCF
			}
			return picked[i].name < picked[j].name
		})
		if len(picked) == 0 {
			return "", fmt.Errorf("没有适用于 %q 的 cookie（cookie_list 里无同域项）", host)
		}
		var b strings.Builder
		for _, p := range picked {
			if b.Len() > 0 {
				b.WriteString("; ")
			}
			b.WriteString(p.name)
			b.WriteString("=")
			b.WriteString(p.value)
		}
		return b.String(), nil
	}

	if c.CFClearance == "" && len(c.Cookies) == 0 {
		return "", errors.New("clearance 不含任何 cookie")
	}
	names := make([]string, 0, len(c.Cookies))
	for k := range c.Cookies {
		names = append(names, k)
	}
	sort.Strings(names)
	var b strings.Builder
	if c.CFClearance != "" {
		b.WriteString("cf_clearance=")
		b.WriteString(c.CFClearance)
	}
	for _, k := range names {
		if k == "cf_clearance" {
			continue
		}
		if b.Len() > 0 {
			b.WriteString("; ")
		}
		b.WriteString(k)
		b.WriteString("=")
		b.WriteString(c.Cookies[k])
	}
	return b.String(), nil
}

// domainMatches 判断 cookie 的 domain 是否适用于 host（支持前导点与子域）。
func domainMatches(host, domain string) bool {
	d := strings.TrimPrefix(strings.ToLower(domain), ".")
	return host == d || strings.HasSuffix(host, "."+d)
}

// domainSpecificity 越具体越大：host-only（www.example.com）> 父域（.example.com）。
func domainSpecificity(domain string) int {
	return len(strings.TrimPrefix(domain, "."))
}

type solveReq struct {
	URL     string  `json:"url"`
	Timeout float64 `json:"timeout,omitempty"`
	Force   bool    `json:"force,omitempty"`
}

type solveResp struct {
	Ok        bool    `json:"ok"`
	Solved    bool    `json:"solved"`
	Error     string  `json:"error"`
	ErrorCode string  `json:"error_code"`
	Stage     string  `json:"stage"`
	Hint      string  `json:"hint"`
	Elapsed   float64 `json:"elapsed"`
	Clearance
}

// APIError 是求解服务返回的结构化失败（4xx/5xx）。
type APIError struct {
	StatusCode int
	Code       string
	Message    string
	Stage      string
	Hint       string
}

func (e *APIError) Error() string {
	msg := fmt.Sprintf("求解服务返回 %d %s: %s", e.StatusCode, e.Code, e.Message)
	if e.Hint != "" {
		msg += "（建议：" + e.Hint + "）"
	}
	return msg
}

// Client 求解服务客户端（并发安全）。
type Client struct {
	baseURL  string
	hc       *http.Client // 调求解服务用
	replay   *http.Client // 回放目标站用（独立连接池与超时策略）
	token    string       // 求解服务的 Bearer token
	egressID string       // 本客户端出口标识，参与缓存键

	// renewBefore 提前续期窗口：到期前多久主动刷新（避免请求打到过期 cookie）。
	renewBefore time.Duration
	// solveTimeout 单次求解超时（camoufox 单次 5–20s，给足余量）。
	solveTimeout time.Duration

	mu    sync.Mutex
	cache map[string]*Clearance // key = host|egressID
}

// Option 客户端可选项（保持 New(baseURL) 签名不变）。
type Option func(*Client)

// WithToken 设置求解服务的 Bearer token。
func WithToken(token string) Option { return func(c *Client) { c.token = token } }

// WithEgressID 设置出口标识。换代理出口必须换 id，否则会复用别处出口的凭证。
func WithEgressID(id string) Option { return func(c *Client) { c.egressID = id } }

// WithRenewBefore 设置提前续期窗口。
func WithRenewBefore(d time.Duration) Option { return func(c *Client) { c.renewBefore = d } }

// WithSolveTimeout 设置单次求解超时。
func WithSolveTimeout(d time.Duration) Option { return func(c *Client) { c.solveTimeout = d } }

// WithReplayClient 替换回放用的 HTTP 客户端（例如接入 uTLS 时）。
func WithReplayClient(hc *http.Client) Option { return func(c *Client) { c.replay = hc } }

// New 创建客户端。baseURL 形如 http://127.0.0.1:8878。
func New(baseURL string, opts ...Option) *Client {
	c := &Client{
		baseURL: strings.TrimSuffix(baseURL, "/"),
		// 必须覆盖服务端最坏耗时：(solveTimeout+5s 排队) + 兜底后端 90s ≈ 155s，
		// 否则客户端会先超时，拿到的是原始 net 错误而不是结构化 *APIError。
		hc:           &http.Client{Timeout: 240 * time.Second},
		replay:       defaultReplayClient(),
		renewBefore:  5 * time.Minute,
		cache:        map[string]*Clearance{},
		solveTimeout: 60 * time.Second,
		egressID:     "default",
	}
	for _, o := range opts {
		o(c)
	}
	return c
}

// defaultReplayClient 回放专用客户端。
//
// 原实现直接用 http.DefaultClient：它没有 Timeout（只能靠调用方 ctx），
// 且 DefaultTransport 的 MaxIdleConnsPerHost=2，会让稳态回放反复做 TLS 握手。
func defaultReplayClient() *http.Client {
	return &http.Client{
		Timeout: 0, // 交给调用方 ctx 控制，避免与 ctx 冲突
		Transport: &http.Transport{
			Proxy: http.ProxyFromEnvironment,
			DialContext: (&net.Dialer{
				Timeout:   10 * time.Second,
				KeepAlive: 30 * time.Second,
			}).DialContext,
			MaxIdleConns:          100,
			MaxIdleConnsPerHost:   8,
			IdleConnTimeout:       90 * time.Second,
			TLSHandshakeTimeout:   10 * time.Second,
			ExpectContinueTimeout: 1 * time.Second,
			ForceAttemptHTTP2:     true,
		},
	}
}

func (c *Client) cacheKey(target string) string {
	return hostOf(target) + "|" + c.egressID
}

// Get 取 clearance（优先缓存，快到期自动刷新）。
func (c *Client) Get(ctx context.Context, target string) (*Clearance, error) {
	host := hostOf(target)
	if host == "" {
		return nil, fmt.Errorf("无法解析域名: %s", target)
	}

	c.mu.Lock()
	cached := c.cache[c.cacheKey(target)]
	c.mu.Unlock()

	if cached != nil && time.Now().Add(c.renewBefore).Before(cached.expiresAt()) {
		// 命中本地缓存：复制一份并标记，避免调用方误以为又启动了一次浏览器
		hit := *cached
		hit.Cached = true
		hit.Elapsed = 0
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
	body, err := json.Marshal(solveReq{URL: target, Timeout: c.solveTimeout.Seconds(), Force: force})
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/solve", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}

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

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, &APIError{
			StatusCode: resp.StatusCode,
			Code:       sr.ErrorCode,
			Message:    sr.Error,
			Stage:      sr.Stage,
			Hint:       sr.Hint,
		}
	}
	if !sr.Ok {
		// HTTP 200 + ok:false = 求解失败（不是请求失败），语义见 docs/API.md
		return nil, &APIError{StatusCode: resp.StatusCode, Code: sr.ErrorCode, Message: sr.Error, Stage: sr.Stage, Hint: sr.Hint}
	}
	if sr.CFClearance == "" {
		return nil, errors.New("求解服务未返回 cf_clearance")
	}

	cl := &Clearance{
		CFClearance: sr.CFClearance,
		UserAgent:   sr.UserAgent,
		Cookies:     sr.Cookies,
		CookieList:  sr.CookieList,
		ExpiresIn:   sr.ExpiresIn,
		ExpiresAt:   sr.ExpiresAt,
		Elapsed:     sr.Elapsed,
		Cached:      sr.Cached, // 服务端缓存命中时为 true
		EgressID:    sr.EgressID,
	}
	c.mu.Lock()
	c.cache[c.cacheKey(target)] = cl
	c.mu.Unlock()
	return cl, nil
}

// Do 用求解得到的 UA + Cookie 回放一个请求。
//
// 会覆盖 req 的 User-Agent 与 Cookie 头——这是必须的，cf_clearance 绑 UA，换了 UA 即失效。
func (c *Client) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	cl, err := c.Get(ctx, req.URL.String())
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
	host := strings.ToLower(req.URL.Hostname())
	cookie, err := cl.CookieHeaderFor(host)
	if err != nil {
		return nil, err
	}

	req.Header.Set("User-Agent", cl.UserAgent)
	req.Header.Set("Cookie", cookie)
	if req.Header.Get("Accept") == "" {
		// 与 Firefox 的默认导航头保持一致（UA 是 Firefox，就别发 Chrome 系的 Accept）
		req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	}
	if req.Header.Get("Accept-Language") == "" {
		req.Header.Set("Accept-Language", "en-US,en;q=0.5")
	}
	if req.Header.Get("Upgrade-Insecure-Requests") == "" {
		req.Header.Set("Upgrade-Insecure-Requests", "1")
	}
	if req.Header.Get("Sec-Fetch-Dest") == "" {
		req.Header.Set("Sec-Fetch-Dest", "document")
		req.Header.Set("Sec-Fetch-Mode", "navigate")
		req.Header.Set("Sec-Fetch-Site", "none")
		req.Header.Set("Sec-Fetch-User", "?1")
	}
	return c.replay.Do(req.WithContext(ctx))
}

// Stats 透传求解服务的统计。
func (c *Client) Stats(ctx context.Context) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/stats", nil)
	if err != nil {
		return nil, err
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
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

// Health 查询 /health（公开端点，不需要 token）；返回是否就绪。
func (c *Client) Health(ctx context.Context) (map[string]any, bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/health", nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, false, err
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil {
		return nil, false, err
	}
	return out, resp.StatusCode == http.StatusOK, nil
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
