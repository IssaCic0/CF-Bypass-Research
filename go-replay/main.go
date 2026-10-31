// Go 原生 net/http 回放 camoufox 取得的 cf_clearance，验证 Go 网关能否直接复用。
//
// 判据：HTTP 200 且返回真实页面（不是 "Just a moment..." 挑战页）。
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

func readTrim(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		fmt.Println("读文件失败:", err)
		os.Exit(1)
	}
	return strings.TrimSpace(string(b))
}

func main() {
	dir := `D:\apk-analysis\cf-jsd-recon\gotest\`
	cookie := readTrim(dir + "cookie.txt")
	ua := readTrim(dir + "ua.txt")

	client := &http.Client{Timeout: 40 * time.Second}

	for _, target := range []string{"https://steamdb.info/", "https://www.glassdoor.com/"} {
		req, err := http.NewRequest("GET", target, nil)
		if err != nil {
			fmt.Println("构造请求失败:", err)
			continue
		}
		req.Header.Set("User-Agent", ua)
		req.Header.Set("Cookie", cookie)
		req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
		req.Header.Set("Accept-Language", "en-US,en;q=0.5")

		t0 := time.Now()
		resp, err := client.Do(req)
		if err != nil {
			fmt.Printf("%s -> ERR %v\n", target, err)
			continue
		}
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 400000))
		resp.Body.Close()
		challenge := strings.Contains(string(body), "Just a moment") || strings.Contains(string(body), "cf-mitigated")
		fmt.Printf("%-30s status=%d len=%d challenge_page=%v elapsed=%dms\n",
			target, resp.StatusCode, len(body), challenge, time.Since(t0).Milliseconds())
	}
}
