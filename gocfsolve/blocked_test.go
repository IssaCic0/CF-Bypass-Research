// 这些用例守的是 2026-11-04 真机实测暴露的一个**判据误报**：
// udemy 的真实页面（306KB）里含 "Just a moment" 字符串（站点自己的 JS 文案），
// 于是"在整个 body 里找子串"的判据把**成功**判成了**挑战页**。
// 结论：挑战页判据必须只看**前缀**、并且只在 200 + html 的情况下看正文。
package cfsolve

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

func fakeResp(status int, contentType string, body string, hdr map[string]string) *http.Response {
	h := http.Header{}
	if contentType != "" {
		h.Set("Content-Type", contentType)
	}
	for k, v := range hdr {
		h.Set(k, v)
	}
	return &http.Response{
		StatusCode: status,
		Header:     h,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

// 真实页面 + 深处含 marker：**不能**判成挑战页，且 body 必须原样还原。
func TestLooksBlockedIgnoresMarkerDeepInBody(t *testing.T) {
	body := "<html><head><title>Real Page</title></head><body>" +
		strings.Repeat("content ", 3000) + "Just a moment</body></html>"
	if len(body) < challengePeekBytes*2 {
		t.Fatalf("测试体不够长（%d），压不出'深处'的效果", len(body))
	}
	resp := fakeResp(200, "text/html; charset=utf-8", body, nil)
	if LooksBlocked(resp) {
		t.Fatal("误报：真实页面被判成了挑战页（marker 只在 2048 字节之后）")
	}
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读还原后的 body 失败：%v", err)
	}
	if string(got) != body {
		t.Fatalf("body 没有被还原：%d != %d", len(got), len(body))
	}
}

// 真正的挑战页（marker 在开头）：必须判成挑战页。
func TestLooksBlockedDetectsRealChallenge(t *testing.T) {
	for name, body := range map[string]string{
		"title":      `<html><head><title>Just a moment...</title></head><body>x</body></html>`,
		"chl_opt":    `<html><script>window._cf_chl_opt={cType:'managed'};</script></html>`,
		"challenge1": `<html><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1"></script></html>`,
	} {
		resp := fakeResp(200, "text/html", body, nil)
		if !LooksBlocked(resp) {
			t.Fatalf("%s：真挑战页没被识别出来", name)
		}
	}
}

// 403 与 cf-mitigated 头：优先级高于正文判断。
func TestLooksBlockedStatusAndHeader(t *testing.T) {
	if !LooksBlocked(fakeResp(403, "text/html", "anything", nil)) {
		t.Fatal("403 应当判成被拦")
	}
	if !LooksBlocked(fakeResp(200, "text/html", "clean page", map[string]string{"cf-mitigated": "challenge"})) {
		t.Fatal("cf-mitigated 头应当判成被拦")
	}
}

// 非 html（JSON/图片）不该去读正文，也不该误判。
func TestLooksBlockedSkipsNonHTML(t *testing.T) {
	resp := fakeResp(200, "application/json", `{"just a moment":true}`, nil)
	if LooksBlocked(resp) {
		t.Fatal("JSON 响应不该被判成挑战页")
	}
}

// nil 响应不能 panic。
func TestLooksBlockedNil(t *testing.T) {
	if LooksBlocked(nil) {
		t.Fatal("nil 应当是 false")
	}
}
