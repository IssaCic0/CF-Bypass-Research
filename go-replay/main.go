// Go 原生 net/http 回放 camoufox 取得的 cf_clearance，验证 Go 网关能否直接复用。
//
// 判据：HTTP 200 且返回真实页面（不是 "Just a moment..." 挑战页）。
//
// 用法（不依赖任何硬编码绝对路径）：
//   CF_COOKIE_DIR=/path/to/dir go run ./go-replay          # 目录内需 cookie.txt / ua.txt
//   go run ./go-replay -dir ./samples -target https://steamdb.info/
package main

import (
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func readTrim(p string) (string, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

func main() {
	dir := flag.String("dir", os.Getenv("CF_COOKIE_DIR"),
		"存放 cookie.txt 与 ua.txt 的目录（默认取环境变量 CF_COOKIE_DIR）")
	target := flag.String("target", "https://steamdb.info/", "要回放的目标地址")
	flag.Parse()

	if *dir == "" {
		fmt.Println("缺少目录：用 -dir 指定，或设置环境变量 CF_COOKIE_DIR")
		fmt.Println("目录内需要：cookie.txt（Cookie 头的值）与 ua.txt（求解时的 User-Agent）")
		os.Exit(2)
	}

	cookie, err := readTrim(filepath.Join(*dir, "cookie.txt"))
	if err != nil {
		fmt.Printf("读 cookie.txt 失败: %v\n", err)
		os.Exit(2)
	}
	ua, err := readTrim(filepath.Join(*dir, "ua.txt"))
	if err != nil {
		fmt.Printf("读 ua.txt 失败: %v\n", err)
		os.Exit(2)
	}

	client := &http.Client{Timeout: 40 * time.Second}

	req, err := http.NewRequest(http.MethodGet, *target, nil)
	if err != nil {
		fmt.Println("构造请求失败:", err)
		os.Exit(2)
	}
	req.Header.Set("User-Agent", ua)
	req.Header.Set("Cookie", cookie)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.5")

	t0 := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		fmt.Printf("%s -> ERR %v\n", *target, err)
		os.Exit(1)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 400000))
	resp.Body.Close()

	challenge := strings.Contains(string(body), "Just a moment") || strings.Contains(string(body), "cf-mitigated")
	fmt.Printf("%-30s status=%d len=%d challenge_page=%v elapsed=%dms\n",
		*target, resp.StatusCode, len(body), challenge, time.Since(t0).Milliseconds())

	if resp.StatusCode != http.StatusOK || challenge {
		os.Exit(1)
	}
}
