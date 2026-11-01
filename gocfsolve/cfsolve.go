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
	probe    *http.Client // 探活/统计用：**短超时**，健康检查不该跟着求解的 240s 一起挂
	token    string       // 求解服务的 Bearer token
	egressID string       // 本客户端出口标识，参与缓存键

	// renewBefore 提前续期窗口：到期前多久主动刷新（避免请求打到过期 cookie）。
	renewBefore time.Duration
	// solveTimeout 单次求解超时（camoufox 单次 5–20s，给足余量）。
	solveTimeout time.Duration
	// revalidate 回放遇到 403/挑战页时，是否自动失效缓存并重解一次（默认开）。
	revalidate bool
	// revalidateSem 全局重解并发上限：单域并发已由 flights 收敛，这里管**跨域**，
	// 防"CF 改版导致大量域同时失效"时把浏览器打满。
	revalidateSem chan struct{}
	// replayExplicit 记录 replay 是否由调用方显式注入（WithReplayClient）。
	// 显式注入时不再按出口切换 Transport —— 出口一致性由调用方负责。
	replayExplicit bool
	// proxies 出口标识 → 代理地址；replayClients 是按出口缓存的回放客户端。
	// 求解走哪个出口，回放就必须走同一个（cf_clearance 绑 IP，换出口 = 静默 403）。
	proxies       map[string]*url.URL
	replayClients map[string]*http.Client

	mu    sync.Mutex
	cache map[string]*Clearance // key = host|egressID
	// maxEntries 本地缓存条目上限（0=不限）。只增不减的 map 是一条慢泄漏。
	maxEntries int
	// flights 是 per-key 单飞表：同一个 key 的并发请求只让一个真的去求解，其余等结果。
	// 没有它时，50 个并发冷域请求 = 50 次求解，服务端 inflight 闸门只能把它们打回 429。
	flights map[string]*flight
}

// ErrNoEgressProxy 表示"这个出口标识没有登记代理，拒绝回放"。
//
// 失败关闭是刻意的：宁可直接报错，也不要从一个与求解时不同的出口发出去 —— 那必然拿到
// 403，还会被误判成"CF 又改版了"，是最难归因的一类故障。
var ErrNoEgressProxy = errors.New("出口未登记代理")

// flight 是一次进行中的求解。done 在所有路径上都会被 close（含失败与 panic 边界），
// 否则等待者会永久阻塞——这是单飞实现最常见的死锁来源。
type flight struct {
	done chan struct{}
	cl   *Clearance
	err  error
}

// : 判断"是否被 CF 拦下"时最多偷看的正文字节数。
// 只用于 200 状态下的挑战页识别；偷看后**必须**把 body 还原（见 peekBody）。
const challengePeekBytes = 2048

// : 挑战页的特征串（小写比较）。命中任一即认为"这不是真实页面"。
var challengeMarkers = []string{
	"just a moment",
	"checking your browser",
	"cf-mitigated",
	"_cf_chl_opt",
	"/cdn-cgi/challenge-platform/",
	"challenges.cloudflare.com",
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
// 注意：一旦显式注入，客户端不再按 clearance 的 egress_id 自动选出口，由你负责。
func WithReplayClient(hc *http.Client) Option {
	return func(c *Client) {
		c.replay = hc
		c.replayExplicit = true
	}
}

// WithEgressProxy 为某个出口标识登记代理地址（形如 http://user:pass@host:port）。
//
// 为什么必须有它：服务端可以配代理池（`--proxy`），求解走出口 A；但回放是 Go 侧自己
// 发请求，若不按 clearance 的 `egress_id` 选同一个代理，就会从出口 B 出去 ——
// cf_clearance 绑 IP，结果是最难排查的静默 403。
//
// 纪律：**一旦登记了任何出口代理，回放就 fail closed** —— 拿到一个没登记代理的
// egress_id 时直接返回 ErrNoEgressProxy，而不是"悄悄直连试试"。
func WithEgressProxy(egressID, proxyURL string) Option {
	return func(c *Client) {
		u, err := url.Parse(strings.TrimSpace(proxyURL))
		if err != nil || u.Host == "" {
			return // 解析不了就当没配；第一次回放会以 ErrNoEgressProxy 暴露出来
		}
		if c.proxies == nil {
			c.proxies = map[string]*url.URL{}
		}
		c.proxies[strings.TrimSpace(egressID)] = u
	}
}

// WithRevalidateConcurrency 设置**跨域**重解并发上限（默认 2）。
func WithRevalidateConcurrency(n int) Option {
	return func(c *Client) {
		if n > 0 {
			c.revalidateSem = make(chan struct{}, n)
		}
	}
}

// WithCacheLimit 设置本地缓存条目上限（默认 1024；<=0 表示不限）。
func WithCacheLimit(n int) Option { return func(c *Client) { c.maxEntries = n } }

// WithRevalidate 开关"回放自愈"（默认开）。
//
// 关掉它等于回到 v3 的行为：403 / 挑战页原样透传给业务方。
// 应急时可用（例如怀疑重解会放大故障），但不建议长期关闭——凭证失效后业务侧会
// 静默拿到 403 直到缓存自然过期（最长 ttl），这是最容易被当成"服务挂了"的故障。
func WithRevalidate(enabled bool) Option {
	return func(c *Client) { c.revalidate = enabled }
}

// New 创建客户端。baseURL 形如 http://127.0.0.1:8878。
func New(baseURL string, opts ...Option) *Client {
	c := &Client{
		baseURL: strings.TrimSuffix(baseURL, "/"),
		// 服务端后端链按 deadline 分预算，总耗时 ≤ 请求 timeout(≤120s) + 单次排队余量。
		// 这里留到 240s：再小会让客户端先超时，拿到裸 net 错误而不是结构化 *APIError。
		hc:            &http.Client{Timeout: 240 * time.Second},
		probe:         &http.Client{Timeout: 5 * time.Second},
		replay:        defaultReplayClient(),
		replayClients: map[string]*http.Client{},
		renewBefore:   5 * time.Minute,
		cache:         map[string]*Clearance{},
		flights:       map[string]*flight{},
		maxEntries:    1024,
		revalidate:    true,
		revalidateSem: make(chan struct{}, 2),
		solveTimeout:  60 * time.Second,
		egressID:      "default",
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
//
// 并发语义：同一个「域名+出口」的并发 Get 会合并成**一次**求解（per-host 单飞）。
// 为什么要合并：冷域上 N 个并发请求 = N 次浏览器求解，服务端 inflight 闸门只会把
// 多出来的打回 429，用户侧看到的是"随机失败"，而不是"排队变慢"。
func (c *Client) Get(ctx context.Context, target string) (*Clearance, error) {
	host := hostOf(target)
	if host == "" {
		return nil, fmt.Errorf("无法解析域名: %s", target)
	}
	if hit := c.cachedCopy(c.cacheKey(target)); hit != nil {
		return hit, nil
	}
	// force=false：允许求解服务返回它自己的域名缓存（省掉一次浏览器求解）
	return c.solveShared(ctx, target, false)
}

// cachedCopy 返回缓存副本（未命中/已进入续期窗口时返回 nil）。
// 返回副本而不是指针：调用方会改 CFClearance 之类的字段（测试与业务都有），共享指针会互相污染。
func (c *Client) cachedCopy(key string) *Clearance {
	c.mu.Lock()
	defer c.mu.Unlock()
	cached := c.cache[key]
	if cached == nil {
		return nil
	}
	if !time.Now().Add(c.renewBefore).Before(cached.expiresAt()) {
		return nil
	}
	hit := *cached
	hit.Cached = true
	hit.Elapsed = 0
	// 深拷贝可变字段：只做结构体浅拷贝的话，CookieList 切片和 Cookies map 仍与缓存共享，
	// 调用方一旦排序/裁剪就会污染缓存里那份（而且不会有任何报错）。
	hit.CookieList = append([]Cookie(nil), cached.CookieList...)
	if cached.Cookies != nil {
		cp := make(map[string]string, len(cached.Cookies))
		for k, v := range cached.Cookies {
			cp[k] = v
		}
		hit.Cookies = cp
	}
	return &hit
}

// rememberLocked 写缓存并在超上限时淘汰（调用方必须已持 c.mu）。
//
// 淘汰策略刻意简单：先清所有已过期项；仍然超额就删掉任意一条。
// 不引入 LRU 的理由：这里的键是"域名+出口"，热点分布长尾且条目上限只是防泄漏，
// 淘汰错了的代价只是多解一次，不值得为它维护访问顺序。
func (c *Client) rememberLocked(key string, cl *Clearance) {
	if c.maxEntries > 0 && len(c.cache) >= c.maxEntries {
		now := time.Now()
		for k, v := range c.cache {
			if !now.Before(v.expiresAt()) {
				delete(c.cache, k)
			}
		}
		if len(c.cache) >= c.maxEntries {
			for k := range c.cache {
				delete(c.cache, k)
				break
			}
		}
	}
	c.cache[key] = cl
}

// GetFresh 强制重新求解（忽略本地缓存与服务端缓存）。
func (c *Client) GetFresh(ctx context.Context, target string) (*Clearance, error) {
	return c.solveShared(ctx, target, true)
}

// solveShared 是 Get/GetFresh 的单飞外壳：同 key 并发只让 leader 真的求解。
//
// 三个必须成立的细节（少一个就会引入新缺陷）：
//  1. 失败也要 close(done) 并从 flights 里删除，否则等待者永久阻塞；
//  2. 等待者必须先尊重**自己**的 ctx（leader 慢不代表等待者要陪着超时）；
//  3. 只缓存"确实还没进入续期窗口"的结果——TTL=0/已过期的响应进缓存等于每次都重解。
func (c *Client) solveShared(ctx context.Context, target string, force bool) (*Clearance, error) {
	key := c.cacheKey(target)

	c.mu.Lock()
	if f, ok := c.flights[key]; ok {
		c.mu.Unlock()
		select {
		case <-f.done:
			if f.err != nil {
				return nil, f.err
			}
			hit := *f.cl
			return &hit, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	f := &flight{done: make(chan struct{})}
	c.flights[key] = f
	c.mu.Unlock()

	// leader：结果写回 flight，然后**无论成败**都收尾
	f.cl, f.err = c.solve(ctx, target, force)

	c.mu.Lock()
	if f.err == nil && f.cl != nil && time.Now().Add(c.renewBefore).Before(f.cl.expiresAt()) {
		c.rememberLocked(key, f.cl)
	}
	delete(c.flights, key)
	c.mu.Unlock()
	close(f.done)

	if f.err != nil {
		return nil, f.err
	}
	hit := *f.cl
	return &hit, nil
}

// invalidate 删掉某 key 的本地缓存（凭证失效时用）。
func (c *Client) invalidate(key string) {
	c.mu.Lock()
	delete(c.cache, key)
	c.mu.Unlock()
}

// revalidateOnce 失效缓存并强制重解一次。
//
// 它复用 solveShared 的单飞：同一个域上 5 个并发请求同时遇 403 时，只会真的重解 1 次。
// 没有这层收敛，一次"全站凭证同时过期"就会变成 N 倍求解风暴，把浏览器打满。
func (c *Client) revalidateOnce(ctx context.Context, target string) (*Clearance, error) {
	c.invalidate(c.cacheKey(target))
	return c.solveShared(ctx, target, true)
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

	resp, err := c.probe.Do(req)
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
	// 缓存写入统一由 solveShared 负责（它在同一把锁里同时处理"是否值得缓存 + 收尾 flight"）。
	// 这里不再写一次：两处写会出现"TTL=0 的响应被一处写进、另一处判过期"的不一致。
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
//
// 自愈语义（T-10，默认开，`WithRevalidate(false)` 可关）：
//
//	回放拿到 403 / `cf-mitigated` 头 / 挑战页正文 ⇒ 判定凭证已失效
//	→ 失效本地缓存 → **重解一次** → 用新凭证重放一次 → 返回新响应。
//	重解仍失败/仍是 403 时，**原样返回第二次的响应**（绝不循环重解烧求解）。
//
// 不做重试的两种情况（都会导致"更坏"的结果，必须显式排除）：
//  1. 请求带 body 且 `GetBody == nil`（流式/不可重放）：重试会丢 body 或重复发送副作用请求；
//  2. `WithRevalidate(false)`：应急开关，行为退回 v3 的透传。
func (c *Client) DoWith(ctx context.Context, req *http.Request, cl *Clearance) (*http.Response, error) {
	resp, err := c.doOnce(ctx, req, cl)
	if err != nil {
		return nil, err
	}
	if !c.revalidate || !replayable(req) {
		return resp, nil
	}
	if !looksBlocked(resp) {
		return resp, nil
	}

	// 凭证失效：先拿全局重解额度（跨域限流），拿不到就原样透传
	if !c.tryAcquireRevalidate() {
		return resp, nil
	}
	defer c.releaseRevalidate()

	// 重解一次（同域并发会由单飞收敛成 1 次）
	fresh, rerr := c.revalidateOnce(ctx, req.URL.String())
	if rerr != nil || fresh == nil {
		// 重解不出来就把**原始 403** 交回给调用方，与关闭自愈时的行为一致，
		// 但 body 已被 looksBlocked 还原，调用方仍能正常读取。
		return resp, nil
	}

	// 重放前必须把旧响应体关掉，否则连接泄漏；这条路径很少走，不做 drain（CF 的 403 本来也会断连）
	drainAndClose(resp)

	req2 := req.Clone(ctx)
	if req.GetBody != nil && req.Body != nil {
		b, berr := req.GetBody()
		if berr != nil {
			return nil, fmt.Errorf("重放时重建请求体失败: %w", berr)
		}
		req2.Body = b
	}
	return c.doOnce(ctx, req2, fresh)
}

// doOnce 套上 UA/Cookie 并真正发出请求。
func (c *Client) doOnce(ctx context.Context, req *http.Request, cl *Clearance) (*http.Response, error) {
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
	hc, err := c.replayClientFor(cl.EgressID)
	if err != nil {
		return nil, err
	}
	return hc.Do(req.WithContext(ctx))
}

// replayClientFor 选回放用的 HTTP 客户端：按 clearance 的出口标识匹配已登记的代理。
//
// 三种情况：
//   - 调用方显式注入了 replay（WithReplayClient）→ 用它，不再干预；
//   - 没登记任何出口代理 → 用默认客户端（单出口部署，行为与 v3 一致）；
//   - 登记了出口代理 → **必须**匹配到同一个出口，匹配不到就 fail closed。
func (c *Client) replayClientFor(egressID string) (*http.Client, error) {
	if c.replayExplicit {
		return c.replay, nil
	}
	if len(c.proxies) == 0 {
		return c.replay, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if hc, ok := c.replayClients[egressID]; ok {
		return hc, nil
	}
	u, ok := c.proxies[egressID]
	if !ok {
		return nil, fmt.Errorf(
			"%w：出口 %q（WithEgressProxy 未登记）；"+
				"从别的出口回放会让 cf_clearance 立即失效，因此拒绝发送", ErrNoEgressProxy, egressID)
	}
	tr := defaultReplayClient().Transport.(*http.Transport).Clone()
	tr.Proxy = http.ProxyURL(u)
	hc := &http.Client{Timeout: 0, Transport: tr}
	c.replayClients[egressID] = hc
	return hc, nil
}

// tryAcquireRevalidate 拿一个"允许重解"的全局额度；拿不到就返回 false（调用方原样透传）。
//
// 为什么不是阻塞等待：重解只是自愈手段，不是请求的必经步骤。为了重解让调用方排队，
// 在"大面积失效"时等于把所有请求都拖慢；宁可让这次请求看到 403，也不要制造风暴。
func (c *Client) tryAcquireRevalidate() bool {
	if c.revalidateSem == nil {
		return true
	}
	select {
	case c.revalidateSem <- struct{}{}:
		return true
	default:
		return false
	}
}

func (c *Client) releaseRevalidate() {
	if c.revalidateSem == nil {
		return
	}
	select {
	case <-c.revalidateSem:
	default:
	}
}

// replayable 判断这个请求能不能被安全地重放一次。
func replayable(req *http.Request) bool {
	if req.Body == nil || req.Body == http.NoBody {
		return true
	}
	return req.GetBody != nil
}

// looksBlocked 判断响应是不是"被 CF 拦了"。
//
// 判据优先级：`cf-mitigated` 头 > 403 > 200 正文里的挑战页特征。
// 对 200 会**偷看**正文前缀，看完必须还原（见 peekBody），否则调用方拿到残缺页面。
func looksBlocked(resp *http.Response) bool {
	if resp == nil {
		return false
	}
	if v := strings.ToLower(resp.Header.Get("cf-mitigated")); v != "" {
		return true
	}
	if resp.StatusCode == http.StatusForbidden {
		return true
	}
	if resp.StatusCode != http.StatusOK || resp.Body == nil {
		return false
	}
	ct := strings.ToLower(resp.Header.Get("Content-Type"))
	if ct != "" && !strings.Contains(ct, "html") {
		// JSON/图片/二进制不可能是 CF 挑战页，别白读
		return false
	}
	prefix, err := peekBody(resp, challengePeekBytes)
	if err != nil || prefix == "" {
		return false
	}
	low := strings.ToLower(prefix)
	for _, m := range challengeMarkers {
		if strings.Contains(low, m) {
			return true
		}
	}
	return false
}

// peekBody 读取正文前缀并把 body **还原成完整流**。
//
// 这是整个自愈逻辑里最容易造成"静默错数据"的一步：如果只读不还原，
// 业务方拿到的页面会缺掉开头若干字节，而且不会有任何报错。
func peekBody(resp *http.Response, n int) (string, error) {
	buf := make([]byte, n)
	m, err := io.ReadFull(resp.Body, buf)
	buf = buf[:m]
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		return "", err
	}
	resp.Body = struct {
		io.Reader
		io.Closer
	}{io.MultiReader(bytes.NewReader(buf), resp.Body), resp.Body}
	return string(buf), nil
}

// drainAndClose 尽量把响应体读空再关闭，让底层连接能复用；读不动就直接关。
func drainAndClose(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	_, _ = io.CopyN(io.Discard, resp.Body, 4096)
	_ = resp.Body.Close()
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
	resp, err := c.probe.Do(req)
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
	resp, err := c.probe.Do(req)
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
