// A2 实验：用纯 Go 的 JS 引擎（goja）执行 Cloudflare 的混淆编排脚本。
//
// 这条实验的定位必须说清楚（避免被误读）：它证明的是**语法层可执行**——
// 575 KB 的现代混淆脚本能被 goja 解析并跑完，不等于"环境真实"、更不等于"能过盾"。
// 下面注入的 DOM/浏览器对象是最小假体，大量分支会因为 `querySelector` 返回 null 而走假路径。
//
// 用法（不依赖任何硬编码绝对路径）：
//
//	CF_SCRIPT_PATH=/path/to/orchestrate_chl_page.js \
//	CF_OPT_PATH=/path/to/chl_opt.js \
//	go run ./go-jseng
//
//	或显式传参：
//	go run ./go-jseng -script ./samples/orchestrate_chl_page.js -opt ./samples/chl_opt.js
package main

import (
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/dop251/goja"
)

func main() {
	scriptPath := flag.String("script", os.Getenv("CF_SCRIPT_PATH"),
		"CF 挑战编排脚本路径（默认取环境变量 CF_SCRIPT_PATH）")
	optPath := flag.String("opt", os.Getenv("CF_OPT_PATH"),
		"挑战页里 window._cf_chl_opt 片段路径（默认取环境变量 CF_OPT_PATH；可缺省）")
	maxRuntime := flag.Duration("max-runtime", 30*time.Second, "脚本执行上限（goja 无内存上限，必须靠 Interrupt 兜底）")
	flag.Parse()

	if *scriptPath == "" {
		fmt.Println("缺少脚本路径：用 -script 指定，或设置环境变量 CF_SCRIPT_PATH")
		fmt.Println("样本从哪来见 README「关于样本文件」一节（仓库不收录 CF 的混淆脚本）")
		os.Exit(2)
	}

	script, err := os.ReadFile(*scriptPath)
	if err != nil {
		fmt.Printf("读脚本失败(%s): %v\n", *scriptPath, err)
		os.Exit(2)
	}
	var opt []byte
	if *optPath != "" {
		opt, err = os.ReadFile(*optPath)
		if err != nil {
			fmt.Printf("读 _cf_chl_opt 失败(%s): %v（继续执行，仅缺少该片段）\n", *optPath, err)
		}
	}
	fmt.Printf("脚本 %d 字节，_cf_chl_opt 定义 %d 字节\n", len(script), len(opt))

	vm := goja.New()
	// 敌意/混淆脚本必须设上限：goja 没有内存上限 API，能做的只有调用栈与执行时长
	vm.SetMaxCallStackSize(10000)
	start := time.Now()
	done := make(chan struct{})
	go func() {
		<-time.After(*maxRuntime)
		select {
		case <-done:
		default:
			vm.Interrupt("执行超时")
		}
	}()

	// 0) 先把挑战页里的 window._cf_chl_opt 片段注入进去（原实现读了却没用，输出会自相矛盾）
	if len(opt) > 0 {
		if _, err := vm.RunString(string(opt)); err != nil {
			fmt.Printf("注入 _cf_chl_opt 失败: %v\n", err)
		}
	}

	// 1) 先建立浏览器环境假体（最小实现，只保证不抛异常）
	_, err = vm.RunString(`
		var window = this; var globalThis = this; var self = this;
		var document = {
			createElement: function(tag){ return {tagName:tag, style:{}, appendChild:function(){},
				setAttribute:function(){}, getAttribute:function(){return null;},
				addEventListener:function(){}, getContext:function(){ return null; },
				parentNode:null, children:[]}; },
			getElementById: function(){ return null; },
			querySelector: function(){ return null; },
			querySelectorAll: function(){ return []; },
			addEventListener: function(){}, removeEventListener: function(){},
			cookie:'', referrer:'https://steamdb.info/', readyState:'loading',
			documentElement:{style:{}, appendChild:function(){}},
			head:{appendChild:function(){}}, body:{appendChild:function(){}},
			createTextNode:function(t){return {text:t};}
		};
		var navigator = {userAgent:'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
			platform:'Win32', language:'en-US', languages:['en-US','en'], hardwareConcurrency:8};
		var location = {href:'https://steamdb.info/', protocol:'https:', host:'steamdb.info', pathname:'/', search:''};
		var setTimeout = function(fn, ms){ return 0; };
		var setInterval = function(fn, ms){ return 0; };
		var clearTimeout = function(){}; var clearInterval = function(){};
		var requestAnimationFrame = function(fn){ return 0; };
	`)
	if err != nil {
		fmt.Println("构建假体失败:", err)
		return
	}

	// 2) 再执行混淆脚本
	_, err = vm.RunString(string(script))
	elapsed := time.Since(start)
	if err != nil {
		fmt.Printf("执行失败（%v）: %v\n", elapsed.Round(time.Millisecond), err)
	} else {
		fmt.Printf("✅ 执行成功（%v）\n", elapsed.Round(time.Millisecond))
	}
	close(done)

	// 3) 打印脚本执行后 _cf_chl_opt 的键数，作为"跑到运行时注入阶段"的证据。
	//    注意：optPath 缺省时这里显示"未定义"是正常的——它只说明没喂 opt 片段，不说明 CF 没注入。
	if v := vm.Get("_cf_chl_opt"); v != nil && !goja.IsUndefined(v) && !goja.IsNull(v) {
		if obj, ok := v.Export().(map[string]interface{}); ok {
			fmt.Printf("执行后 _cf_chl_opt 键数: %d\n", len(obj))
		} else {
			fmt.Printf("执行后 _cf_chl_opt 类型: %T\n", v.Export())
		}
	} else if *optPath == "" {
		fmt.Println("执行后 _cf_chl_opt: 未定义（未提供 -opt/CF_OPT_PATH，属预期）")
	} else {
		fmt.Println("执行后 _cf_chl_opt: 未定义（已提供 opt 片段但脚本未使用，检查片段内容）")
	}
}
