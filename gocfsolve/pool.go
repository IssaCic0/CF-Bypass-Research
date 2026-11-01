package cfsolve

// 多进程池客户端。
//
// 为什么需要：单实例内存 ≈700MB 且**串行**求解（Playwright sync API 绑定线程），
// 要提吞吐只能"多进程 + 每个进程一个端口"。但多进程会带来两个必须解决的问题：
//   1) 某个域打到哪个进程？——**按域一致性哈希**：同域固定同一节点（凭证按域绑定），
//      扩容只影响约 1/N 的域，而不是把全部缓存打散重来。
//   2) 节点挂了怎么办？——被动失败计数 + 冷却窗口（`DownDuration`），期间跳过该节点，
//      冷却结束自动回归（不做永久黑名单：可能只是重启）。

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// 每个物理节点在环上的虚拟节点数：越多分布越均匀，但环查找常数也略大。
	poolVirtualNodes = 64
)

// 终止性错误：换哪个节点结果都一样，且**不是节点的锅**。
// 与 solver/backends.py 的 TERMINAL_CODES 一一对应，改一边必须改另一边。
//
// 为什么必须有这条：池原来对任意 error 都 markFail。一个调用方把 url 写错（400），
// 或 token 配错（401），连续两次就会把**健康节点摘掉 30 秒**——池子会自我雪崩。
var terminalCodes = map[string]bool{
	"invalid-request": true,
	"unauthorized":    true,
}

// 过载类错误：换节点有意义，但节点本身没坏（它只是忙）。
var saturationCodes = map[string]bool{
	"busy":         true,
	"rate-limited": true,
}

// apiErrorCode 取出结构化错误码（非 *APIError 的网络错误返回 false）。
func apiErrorCode(err error) (string, bool) {
	var ae *APIError
	if errors.As(err, &ae) {
		return ae.Code, true
	}
	return "", false
}

func terminalError(err error) bool {
	code, ok := apiErrorCode(err)
	return ok && terminalCodes[code]
}

func saturationError(err error) bool {
	code, ok := apiErrorCode(err)
	return ok && saturationCodes[code]
}

type poolOptions struct {
	token          string
	maxFailures    int
	downDuration   time.Duration
	healthInterval time.Duration
	clientOptions  []Option
}

// PoolOption 池级配置。
type PoolOption func(*poolOptions)

// WithPoolToken 为所有节点设置 Bearer token。
func WithPoolToken(token string) PoolOption { return func(o *poolOptions) { o.token = token } }

// WithPoolMaxFailures 连续失败多少次把节点摘除（默认 2）。
func WithPoolMaxFailures(n int) PoolOption {
	return func(o *poolOptions) {
		if n > 0 {
			o.maxFailures = n
		}
	}
}

// WithPoolDownDuration 摘除后的冷却时长（默认 30s），到期自动回归。
func WithPoolDownDuration(d time.Duration) PoolOption {
	return func(o *poolOptions) {
		if d > 0 {
			o.downDuration = d
		}
	}
}

// WithPoolHealthProbe 开启主动探活（间隔 >0 时后台定期打 /health）。
func WithPoolHealthProbe(interval time.Duration) PoolOption {
	return func(o *poolOptions) { o.healthInterval = interval }
}

// WithPoolClientOptions 透传给每个节点的 Client（例如 WithEgressID）。
func WithPoolClientOptions(opts ...Option) PoolOption {
	return func(o *poolOptions) { o.clientOptions = append(o.clientOptions, opts...) }
}

// NodeHealth 单个节点的健康视图（对外只读，不含凭据）。
type NodeHealth struct {
	BaseURL   string    `json:"base_url"`
	Healthy   bool      `json:"healthy"`
	Failures  int       `json:"consecutive_failures"`
	LastError string    `json:"last_error"`
	LastOKAt  time.Time `json:"last_ok_at"`
	DownUntil time.Time `json:"down_until"`
}

type poolNode struct {
	client *Client
	base   string

	mu        sync.Mutex
	failures  int
	downUntil time.Time
	lastError string
	lastOK    time.Time
}

func (n *poolNode) healthy(now time.Time) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return !now.Before(n.downUntil)
}

func (n *poolNode) markOK() {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.failures = 0
	n.lastError = ""
	n.lastOK = time.Now()
}

func (n *poolNode) markFail(err error, maxFailures int, down time.Duration) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.failures++
	n.lastError = err.Error()
	if n.failures >= maxFailures {
		n.downUntil = time.Now().Add(down)
	}
}

func (n *poolNode) snapshot() NodeHealth {
	n.mu.Lock()
	defer n.mu.Unlock()
	return NodeHealth{
		BaseURL:   n.base,
		Healthy:   !time.Now().Before(n.downUntil),
		Failures:  n.failures,
		LastError: n.lastError,
		LastOKAt:  n.lastOK,
		DownUntil: n.downUntil,
	}
}

type ringEntry struct {
	hash uint64
	idx  int
}

// Pool 按域一致性哈希把请求分派到多个 solver 进程。
type Pool struct {
	nodes []*poolNode
	ring  []ringEntry
	opts  poolOptions

	stopOnce sync.Once
	stop     chan struct{}
	wg       sync.WaitGroup
}

// NewPool 创建池。nodeURLs 形如 ["http://127.0.0.1:8878", "http://127.0.0.1:8879"]。
func NewPool(nodeURLs []string, opts ...PoolOption) (*Pool, error) {
	if len(nodeURLs) == 0 {
		return nil, fmt.Errorf("节点列表不能为空")
	}
	o := poolOptions{maxFailures: 2, downDuration: 30 * time.Second}
	for _, fn := range opts {
		fn(&o)
	}

	p := &Pool{opts: o, stop: make(chan struct{})}
	seen := map[string]bool{}
	for _, raw := range nodeURLs {
		base := raw
		for len(base) > 0 && base[len(base)-1] == '/' {
			base = base[:len(base)-1]
		}
		if base == "" || seen[base] {
			continue
		}
		seen[base] = true
		clientOpts := append([]Option{}, o.clientOptions...)
		if o.token != "" {
			clientOpts = append(clientOpts, WithToken(o.token))
		}
		p.nodes = append(p.nodes, &poolNode{client: New(base, clientOpts...), base: base})
	}
	if len(p.nodes) == 0 {
		return nil, fmt.Errorf("没有有效节点")
	}

	for idx := range p.nodes {
		for v := 0; v < poolVirtualNodes; v++ {
			p.ring = append(p.ring, ringEntry{hash: poolHash(fmt.Sprintf("%s#%d", p.nodes[idx].base, v)), idx: idx})
		}
	}
	sort.Slice(p.ring, func(i, j int) bool { return p.ring[i].hash < p.ring[j].hash })

	if o.healthInterval > 0 {
		p.wg.Add(1)
		go p.healthLoop()
	}
	return p, nil
}

// Nodes 返回节点数与各自的健康视图。
func (p *Pool) Nodes() []NodeHealth {
	out := make([]NodeHealth, 0, len(p.nodes))
	for _, n := range p.nodes {
		out = append(out, n.snapshot())
	}
	return out
}

// pick 从 host 的环位置开始，按环序找第一个健康节点。
func (p *Pool) pick(host string) *poolNode {
	now := time.Now()
	if len(p.ring) == 0 {
		return nil
	}
	h := poolHash(host)
	start := sort.Search(len(p.ring), func(i int) bool { return p.ring[i].hash >= h })
	if start == len(p.ring) {
		start = 0
	}
	seen := make(map[int]bool, len(p.nodes))
	for step := 0; step < len(p.ring); step++ {
		idx := p.ring[(start+step)%len(p.ring)].idx
		if seen[idx] {
			continue
		}
		seen[idx] = true
		if p.nodes[idx].healthy(now) {
			return p.nodes[idx]
		}
	}
	return nil
}

// candidates 返回按环序排列的候选节点（用于 failover）。
func (p *Pool) candidates(host string) []*poolNode {
	if len(p.ring) == 0 {
		return nil
	}
	h := poolHash(host)
	start := sort.Search(len(p.ring), func(i int) bool { return p.ring[i].hash >= h })
	if start == len(p.ring) {
		start = 0
	}
	seen := make(map[int]bool, len(p.nodes))
	var out []*poolNode
	for step := 0; step < len(p.ring); step++ {
		idx := p.ring[(start+step)%len(p.ring)].idx
		if seen[idx] {
			continue
		}
		seen[idx] = true
		out = append(out, p.nodes[idx])
	}
	return out
}

// Get 取 clearance：按域粘性挑节点，失败则顺延到下一个候选节点。
func (p *Pool) Get(ctx context.Context, target string) (*Clearance, error) {
	cl, _, err := p.getWithNode(ctx, target)
	return cl, err
}

// getWithNode 与 Get 相同，但同时返回真正给出凭证的节点（回放时用同一个客户端）。
func (p *Pool) getWithNode(ctx context.Context, target string) (*Clearance, *poolNode, error) {
	host := hostOf(target)
	if host == "" {
		return nil, nil, fmt.Errorf("无法解析域名: %s", target)
	}
	cands := p.candidates(host)
	if len(cands) == 0 {
		return nil, nil, fmt.Errorf("池内没有可用节点")
	}

	var lastErr error
	tried := 0
	for _, node := range cands {
		if !node.healthy(time.Now()) {
			continue
		}
		tried++
		cl, err := node.client.Get(ctx, target)
		if err == nil {
			node.markOK()
			return cl, node, nil
		}
		lastErr = err
		// 终止性错误：立刻上抛，既不 failover（换谁都一样），也不记节点故障
		if terminalError(err) {
			return nil, nil, err
		}
		// 过载（busy / rate-limited）：要 failover，但不是节点的锅
		if !saturationError(err) {
			node.markFail(err, p.opts.maxFailures, p.opts.downDuration)
		}
		// ctx 已取消/超时就没必要再换节点了
		if ctx.Err() != nil {
			return nil, nil, err
		}
	}
	if lastErr == nil {
		return nil, nil, fmt.Errorf("池内所有节点都在冷却中（%d 个）", len(cands))
	}
	return nil, nil, fmt.Errorf("池内 %d 个节点均失败，最后一个错误: %w", tried, lastErr)
}

// Do 用池取凭证并回放请求。
//
// 回放本身不依赖节点（cookie 拿到后是本地 net/http 发出），但用"给出凭证的那个节点"
// 的客户端可以保证其 replay 配置（连接池/TLS/UA）与求解侧一致。
func (p *Pool) Do(ctx context.Context, req *http.Request) (*http.Response, error) {
	cl, node, err := p.getWithNode(ctx, req.URL.String())
	if err != nil {
		return nil, err
	}
	resp, err := node.client.DoWith(ctx, req, cl)
	if err != nil {
		node.markFail(err, p.opts.maxFailures, p.opts.downDuration)
		return nil, err
	}
	// 回放被拦（403 / cf-mitigated）= 这个节点**给出的凭证**失效了，**不是节点坏了**。
	// 所以正确的动作是"丢掉这份凭证，让下次请求重解"，而不是把节点摘除 30 秒：
	// 目标站的业务性 403（地域封锁、需要登录、权限不足）也会命中这里，
	// 一旦记成节点故障，两次这样的请求就能把一组健康的求解器全部摘掉 —— 自己制造故障。
	// （对比：原来这里无条件 markOK，"凭证死了"的节点永远显示健康，那又是另一个极端。）
	if resp.StatusCode == http.StatusForbidden ||
		strings.Contains(strings.ToLower(resp.Header.Get("cf-mitigated")), "challenge") {
		node.client.invalidate(node.client.cacheKey(req.URL.String()))
		return resp, nil
	}
	node.markOK()
	return resp, nil
}

// Close 停止后台探活。
func (p *Pool) Close() {
	p.stopOnce.Do(func() { close(p.stop) })
	p.wg.Wait()
}

func (p *Pool) healthLoop() {
	defer p.wg.Done()
	ticker := time.NewTicker(p.opts.healthInterval)
	defer ticker.Stop()
	for {
		select {
		case <-p.stop:
			return
		case <-ticker.C:
			for _, n := range p.nodes {
				ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				_, ready, err := n.client.Health(ctx)
				cancel()
				switch {
				case err != nil:
					n.markFail(err, p.opts.maxFailures, p.opts.downDuration)
				case ready:
					n.markOK()
				default:
					// HTTP 层成功但 ready=false —— 这正是"浏览器死了"在服务端的表现
					// （503 + 合法 JSON，见 solver/service.py 的 /health 三态）。
					// 原实现对这个分支什么都不做，等于探活永远看不见最该发现的故障。
					n.markFail(errors.New("探活：服务端返回未就绪（ready=false）"),
						p.opts.maxFailures, p.opts.downDuration)
				}
			}
		}
	}
}

func poolHash(s string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(s))
	return h.Sum64()
}
