package main

import (
	"fmt"
	"os"
	"time"

	"github.com/dop251/goja"
)

const dir = `D:\apk-analysis\cf-jsd-recon\`

func main() {
	script, err := os.ReadFile(dir + "orchestrate_chl_page.js")
	if err != nil {
		fmt.Println("读脚本失败:", err)
		return
	}
	opt, _ := os.ReadFile(os.Getenv("CF_OPT_PATH")) // 挑战页里的 window._cf_chl_opt 片段
	fmt.Printf("脚本 %d 字节，_cf_chl_opt 定义 %d 字节\n", len(script), len(opt))

	vm := goja.New()
	// 1) 先建立浏览器环境假体
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
			language:'en-US', languages:['en-US','en'], hardwareConcurrency:8, deviceMemory:8,
			platform:'Win32', vendor:'Google Inc.', plugins:[], mimeTypes:[], webdriver:false};
		var location = {href:'https://steamdb.info/', hostname:'steamdb.info', hash:'', search:'', protocol:'https:'};
		var screen = {width:1920, height:1080, colorDepth:24, availWidth:1920, availHeight:1040};
		var history = {replaceState:function(){}, pushState:function(){}};
		var performance = {now:function(){ return Date.now() - 1793444000000; }, timing:{navigationStart: Date.now()-4000}};
		var crypto = {getRandomValues:function(a){ for(var i=0;i<a.length;i++) a[i]=Math.floor(Math.random()*256); return a; }};
		var setTimeout = function(f, d){ return 0; };
		var setInterval = function(f, d){ return 0; };
		var requestAnimationFrame = function(f){ return 0; };
		var addEventListener = function(){};
		var XMLHttpRequest = function(){ return {open:function(){}, send:function(){}, setRequestHeader:function(){},
			addEventListener:function(){}, status:0, responseText:''}; };
		var fetch = function(u,o){ return {then:function(){return this;}, catch:function(){return this;}}; };
		var atob = function(s){
			var chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';
			s=String(s).replace(/=+$/,''); var out='', bc=0, bs=0, buffer, idx=0;
			while ((buffer = s.charAt(idx++))) {
				buffer = chars.indexOf(buffer);
				if (~buffer) { bs = bc % 4 ? bs*64+buffer : buffer;
					if (bc++ % 4) out += String.fromCharCode(255 & bs >> (-2*bc & 6)); }
			}
			return out;
		};
		var btoa = function(str){
			var chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';
			var out='', i=0; str=String(str);
			while (i < str.length) {
				var c1=str.charCodeAt(i++), c2=str.charCodeAt(i++), c3=str.charCodeAt(i++);
				var e1=c1>>2, e2=((c1&3)<<4)|(c2>>4), e3=isNaN(c2)?64:((c2&15)<<2)|(c3>>6), e4=isNaN(c3)?64:c3&63;
				out += chars.charAt(e1)+chars.charAt(e2)+(e3===64?'=':chars.charAt(e3))+(e4===64?'=':chars.charAt(e4));
			}
			return out;
		};
	`)
	if err != nil {
		fmt.Println("环境注入失败:", err)
		return
	}

	// 2) 注入页面里的 _cf_chl_opt（真实挑战页的值）
	if _, err = vm.RunString(string(opt)); err != nil {
		fmt.Println("_cf_chl_opt 注入失败:", err)
		return
	}

	// 3) 执行 575KB 混淆编排脚本
	t0 := time.Now()
	_, err = vm.RunString(string(script))
	ms := time.Since(t0).Milliseconds()
	if err != nil {
		fmt.Printf("❌ 执行失败（%dms）: %v\n", ms, err)
	} else {
		fmt.Printf("✅ 执行成功（%dms）\n", ms)
	}
	// 4) 看脚本改写了哪些 _cf_chl_opt 键
	v, _ := vm.RunString(`Object.keys(window._cf_chl_opt).length`)
	fmt.Println("执行后 _cf_chl_opt 键数:", v)
}
