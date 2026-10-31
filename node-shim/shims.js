/**
 * shims.js — 为 jsdom 补齐 Cloudflare 挑战脚本需要的浏览器能力。
 *
 * 两种实现策略并列（用于 A/B）：
 *   真实后端：WebGL 用 headless-gl（若装成功）、Canvas 用 node-canvas
 *   伪造层  ：AudioContext / RTCPeerConnection / Worker / fonts / timing 用确定性伪造
 *
 * 设计原则：所有伪造值必须"互相自洽"（WebGL vendor 与 renderer 匹配、UA 与 platform 匹配），
 * 否则 CF 的横向校验会直接判定环境异常。
 */

function injectShims(window, opts = {}) {
  const GL = opts.glModule || null; // headless-gl（可选）
  const report = { installed: [], failed: [] };
  const ok = (n) => report.installed.push(n);
  const bad = (n, e) => report.failed.push(`${n}: ${e}`);

  // ── 1. navigator 补齐 ─────────────────────────────
  try {
    const nav = window.navigator;
    const def = (obj, k, v) =>
      Object.defineProperty(obj, k, { get: () => v, configurable: true });
    def(nav, "deviceMemory", 8);
    def(nav, "hardwareConcurrency", 8);
    def(nav, "maxTouchPoints", 0);
    def(nav, "webdriver", undefined); // 真实浏览器里该属性不存在
    def(nav, "platform", "Win32");
    def(nav, "vendor", "Google Inc.");
    def(nav, "appVersion", "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");
    def(nav, "connection", {
      effectiveType: "4g", rtt: 100, downlink: 10, saveData: false,
      addEventListener() {}, removeEventListener() {},
    });
    ok("navigator.*");
  } catch (e) {
    bad("navigator", e.message);
  }

  // ── 2. performance.timing（新版是 PerformanceNavigationTiming，旧脚本仍读 timing）──
  try {
    const t0 = Date.now() - 4200;
    window.performance.timing = {
      navigationStart: t0, fetchStart: t0 + 2, domainLookupStart: t0 + 3,
      domainLookupEnd: t0 + 24, connectStart: t0 + 24, connectEnd: t0 + 71,
      secureConnectionStart: t0 + 40, requestStart: t0 + 72, responseStart: t0 + 240,
      responseEnd: t0 + 268, domLoading: t0 + 270, domInteractive: t0 + 1500,
      domContentLoadedEventStart: t0 + 1500, domContentLoadedEventEnd: t0 + 1520,
      domComplete: t0 + 4100, loadEventStart: t0 + 4100, loadEventEnd: t0 + 4210,
    };
    ok("performance.timing");
  } catch (e) {
    bad("performance.timing", e.message);
  }

  // ── 3. WebGL / WebGL2 伪造（无需 GPU，可自洽）───────
  try {
    const UNMASKED_VENDOR = 0x9245;
    const UNMASKED_RENDERER = 0x9246;
    const RENDERER_STR =
      "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)";
    const VENDOR_STR = "Google Inc. (Intel)";

    const PARAMS = {
      0x1f00: "WebKit",                                   // VENDOR
      0x1f01: "WebKit WebGL",                             // RENDERER
      0x1f02: "WebGL 1.0 (OpenGL ES 2.0 Chromium)",        // VERSION
      0x8b8c: "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)", // SHADING_LANGUAGE_VERSION
      [UNMASKED_VENDOR]: VENDOR_STR,
      [UNMASKED_RENDERER]: RENDERER_STR,
      0x0d33: 16384,          // MAX_TEXTURE_SIZE
      0x851c: 16384,          // MAX_CUBE_MAP_TEXTURE_SIZE
      0x8872: 16,             // MAX_TEXTURE_IMAGE_UNITS
      0x8b4c: 1024,           // MAX_VERTEX_UNIFORM_VECTORS
      0x8b4d: 16,             // MAX_COMBINED_UNIFORM_BLOCKS
      0x8dfb: 1024,           // MAX_VARYING_VECTORS
      0x8dfc: 32,             // MAX_COMBINED_TEXTURE_IMAGE_UNITS
      0x8dfd: 1024,           // MAX_FRAGMENT_UNIFORM_VECTORS
      0x8869: 16,             // MAX_VERTEX_ATTRIBS
      0x8d57: 8,              // MAX_SAMPLES
      0x846d: [1024, 1024],   // ALIASED_POINT_SIZE_RANGE
      0x846e: [1, 1],         // ALIASED_LINE_WIDTH_RANGE
      0x0d3a: [16384, 16384], // MAX_VIEWPORT_DIMS
      0x8b49: 4096,           // MAX_VERTEX_UNIFORM_COMPONENTS
      0x8b4a: 65536,          // MAX_FRAGMENT_UNIFORM_COMPONENTS? (approx)
      0x80e8: 4096,
      0x80e9: 4096,
      0x8b4f: 32,
      0x8b50: 4096,
      0x8b51: 16,
      0x8b52: 4096,
      0x8b53: 16,
      0x8b54: 4096,
      0x8b55: 16,
      0x8b56: 4096,
      0x8b57: 16,
      0x8b58: 4096,
      0x8b59: 16,
      0x8b5a: 4096,
      0x8b5b: 16,
      0x8b5c: 4096,
    };

    const EXTENSIONS = [
      "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
      "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth",
      "EXT_shader_texture_lod", "EXT_sRGB", "EXT_texture_compression_bptc",
      "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
      "EXT_texture_norm16", "KHR_parallel_shader_compile", "OES_element_index_uint",
      "OES_fbo_render_mipmap", "OES_standard_derivatives", "OES_texture_float",
      "OES_texture_float_linear", "OES_texture_half_float", "OES_texture_half_float_linear",
      "OES_vertex_array_object", "WEBGL_blend_func_extended", "WEBGL_color_buffer_float",
      "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
      "WEBGL_debug_renderer_info", "WEBGL_debug_shaders", "WEBGL_depth_texture",
      "WEBGL_draw_buffers", "WEBGL_lose_context", "WEBGL_multi_draw", "WEBGL_polygon_mode",
    ];

    function makeGL(canvas, isGL2) {
      const gl = {
        canvas,
        drawingBufferWidth: canvas.width || 300,
        drawingBufferHeight: canvas.height || 150,
        getContextAttributes: () => ({
          alpha: true, antialias: true, depth: true, desynchronized: false,
          failIfMajorPerformanceCaveat: false, powerPreference: "default",
          premultipliedAlpha: true, preserveDrawingBuffer: false,
          stencil: true, xrCompatible: false,
        }),
        getParameter: (p) => (p in PARAMS ? PARAMS[p] : 0),
        getExtension: (name) => {
          if (name === "WEBGL_debug_renderer_info") {
            return { UNMASKED_VENDOR_WEBGL: UNMASKED_VENDOR, UNMASKED_RENDERER_WEBGL: UNMASKED_RENDERER };
          }
          if (EXTENSIONS.includes(name)) return {};
          return null;
        },
        getSupportedExtensions: () => (isGL2 ? [...EXTENSIONS, "EXT_color_buffer_float", "OVR_multiview2"] : EXTENSIONS.slice()),
        getShaderPrecisionFormat: () => ({ rangeMin: 127, rangeMax: 127, precision: 23 }),
        getProgramParameter: () => true,
        getShaderParameter: () => true,
        getProgramInfoLog: () => "",
        getShaderInfoLog: () => "",
        getError: () => 0,
        isContextLost: () => false,
        readPixels: () => {},
        // 其余方法一律 no-op，返回合理默认值
        getUniformLocation: () => ({}),
        getAttribLocation: () => 0,
      };
      const NOOP = () => {};
      const RET0 = () => 0;
      ["activeTexture", "attachShader", "bindBuffer", "bindFramebuffer", "bindRenderbuffer",
        "bindTexture", "blendColor", "blendEquation", "blendFunc", "bufferData", "bufferSubData",
        "clear", "clearColor", "clearDepth", "clearStencil", "colorMask", "compileShader",
        "copyTexImage2D", "createBuffer", "createFramebuffer", "createProgram", "createRenderbuffer",
        "createShader", "createTexture", "cullFace", "deleteBuffer", "depthFunc", "depthMask",
        "disable", "disableVertexAttribArray", "drawArrays", "drawElements", "enable",
        "enableVertexAttribArray", "finish", "flush", "framebufferRenderbuffer", "frontFace",
        "generateMipmap", "hint", "linkProgram", "pixelStorei", "polygonOffset", "renderbufferStorage",
        "sampleCoverage", "scissor", "shaderSource", "stencilFunc", "stencilMask", "stencilOp",
        "texImage2D", "texParameterf", "texParameteri", "uniform1f", "uniform1i", "uniform2f",
        "uniform3f", "uniform4f", "uniformMatrix4fv", "useProgram", "validateProgram",
        "vertexAttrib1f", "vertexAttribPointer", "viewport", "deleteProgram", "deleteShader",
        "deleteTexture", "deleteFramebuffer", "readBuffer", "bindVertexArray",
      ].forEach((m) => { if (!gl[m]) gl[m] = NOOP; });
      ["createVertexArray", "getUniformIndices", "getActiveUniformBlockParameter"].forEach(
        (m) => { if (!gl[m]) gl[m] = () => null; }
      );
      gl.getError = RET0;
      // 万能兜底：CF 会探测大量不常用方法，缺一个就抛 "is not a function"。
      // 用 Proxy 让任何未知成员都可用（方法→no-op，常量→数值）。
      return new Proxy(gl, {
        get(t, prop) {
          if (prop in t) return t[prop];
          if (typeof prop === "symbol") return undefined;
          if (/^[A-Z0-9_]+$/.test(prop)) return 0;      // GL 常量
          if (/^(then|toJSON|inspect)$/.test(prop)) return undefined;
          return () => null;                            // 未知方法
        },
        has() { return true; },
      });
    }

    const origGetContext = window.HTMLCanvasElement.prototype.getContext;
    window.HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
      const t = String(type).toLowerCase();
      if (t === "webgl" || t === "experimental-webgl") return makeGL(this, false);
      if (t === "webgl2") return makeGL(this, true);
      return origGetContext.call(this, type, ...rest);
    };
    window.WebGLRenderingContext = function WebGLRenderingContext() {};
    window.WebGL2RenderingContext = function WebGL2RenderingContext() {};
    ok("WebGL/WebGL2 (伪造, 自洽 Intel UHD 630)");
  } catch (e) {
    bad("WebGL", e.message);
  }

  // ── 4. AudioContext / OfflineAudioContext（确定性指纹）────
  try {
    const SR = 44100;
    function fakeChannelData(len) {
      // 用确定性伪随机（同一实现永远同一输出，符合"同一浏览器指纹稳定"的语义）
      const arr = new Float32Array(len);
      let s = 0x2f6e2b1;
      for (let i = 0; i < len; i++) {
        s ^= s << 13; s ^= s >>> 17; s ^= s << 5; s >>>= 0;
        arr[i] = ((s % 2000) - 1000) / 1e6; // 接近真实压缩器输出的微小波动
      }
      return arr;
    }
    class FakeAudioBuffer {
      constructor(ch, len, sr) {
        this.numberOfChannels = ch; this.length = len; this.sampleRate = sr;
        this.duration = len / sr; this._d = fakeChannelData(len);
      }
      getChannelData() { return this._d; }
      copyFromChannel() {} copyToChannel() {}
    }
    class FakeNode {
      constructor() { this.type = "sine"; this.frequency = { value: 0, setValueAtTime() {} }; this.gain = { value: 1, setValueAtTime() {} }; }
      connect() { return this; } disconnect() {} start() {} stop() {}
    }
    class FakeOfflineAudioContext {
      constructor(ch = 1, len = 5000, sr = SR) {
        this.sampleRate = sr; this.length = len; this.numberOfChannels = ch;
        this.destination = new FakeNode(); this.state = "suspended";
      }
      createOscillator() { return new FakeNode(); }
      createDynamicsCompressor() {
        const n = new FakeNode();
        n.threshold = { value: -24, setValueAtTime() {} };
        n.knee = { value: 30, setValueAtTime() {} };
        n.ratio = { value: 12, setValueAtTime() {} };
        n.attack = { value: 0.003, setValueAtTime() {} };
        n.release = { value: 0.25, setValueAtTime() {} };
        return n;
      }
      createAnalyser() { return new FakeNode(); }
      createGain() { return new FakeNode(); }
      startRendering() {
        this.state = "running";
        return Promise.resolve(new FakeAudioBuffer(this.numberOfChannels, this.length, this.sampleRate));
      }
    }
    class FakeAudioContext extends FakeOfflineAudioContext {
      constructor(o = {}) { super(2, 44100, o.sampleRate || SR); this.state = "running"; this.currentTime = 0; this.baseLatency = 0.01; }
      resume() { return Promise.resolve(); }
      close() { return Promise.resolve(); }
      getOutputTimestamp() { return { contextTime: 0, performanceTime: 0 }; }
    }
    window.AudioContext = FakeAudioContext;
    window.webkitAudioContext = FakeAudioContext;
    window.OfflineAudioContext = FakeOfflineAudioContext;
    window.webkitOfflineAudioContext = FakeOfflineAudioContext;
    ok("AudioContext/OfflineAudioContext (伪造确定性)");
  } catch (e) {
    bad("Audio", e.message);
  }

  // ── 5. Worker（CF 的 PoW 可能跑在 Worker 里）────────
  try {
    const Base = window.EventTarget || class {};
    class ShimWorker extends Base {
      constructor(url) {
        super();
        this.onmessage = null;
        this.onerror = null;
        this._url = String(url);
        setTimeout(() => {
          // 无法真实执行 worker 代码时，安静失败但不抛异常打断主流程
          this.dispatchEvent?.(new window.Event("error"));
        }, 50);
      }
      postMessage() {}
      terminate() {}
      addEventListener(t, fn) { if (t === "message" && fn) this._cb = fn; }
      removeEventListener() {}
    }
    window.Worker = ShimWorker;
    ok("Worker (stub)");
  } catch (e) {
    bad("Worker", e.message);
  }

  // ── 6. RTCPeerConnection（真实 Chrome 的 SDP 形态）────
  try {
    const SDP =
      "v=0\r\no=- 4611731400430051336 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\n" +
      "a=extmap-allow-mixed\r\na=msid-semantic: WMS\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n" +
      "c=IN IP4 0.0.0.0\r\na=candidate:1 1 udp 2113937151 192.168.1.100 54321 typ host generation 0\r\n" +
      "a=ice-ufrag:4ZcD\r\na=ice-pwd:2/1muCWoOi3uLifh0NuRHlZw\r\na=ice-options:trickle\r\n" +
      "a=fingerprint:sha-256 7B:8B:F0:7C:5B:1F:3A:9E:0D:2C:4A:6E:8F:1B:3D:5C:7E:9A:0B:2D:4F:6A:8C:0E:1F:3A:5B:7C:9D:0E\r\n" +
      "a=setup:actpass\r\na=mid:0\r\na=sctp-port:5000\r\na=max-message-size:262144\r\n";
    class FakeRTC {
      constructor() { this.localDescription = null; this.iceConnectionState = "new"; this.iceGatheringState = "complete"; }
      createDataChannel() {
        const ch = { label: "x", readyState: "connecting", onopen: null, onmessage: null,
          send() {}, close() {} };
        setTimeout(() => { ch.readyState = "open"; ch.onopen && ch.onopen(new window.Event("open")); }, 10);
        return ch;
      }
      createOffer() { return Promise.resolve({ type: "offer", sdp: SDP }); }
      setLocalDescription(d) { this.localDescription = d || { type: "offer", sdp: SDP }; return Promise.resolve(); }
      close() {}
      addEventListener() {} removeEventListener() {}
    }
    window.RTCPeerConnection = FakeRTC;
    window.webkitRTCPeerConnection = FakeRTC;
    ok("RTCPeerConnection (伪造 SDP)");
  } catch (e) {
    bad("RTCPeerConnection", e.message);
  }

  // ── 7. Notification / fonts ──────────────────────
  try {
    window.Notification = class Notification {
      static permission = "default";
      static requestPermission() { return Promise.resolve("default"); }
      constructor() {}
    };
    ok("Notification");
  } catch (e) { bad("Notification", e.message); }

  try {
    window.document.fonts = {
      ready: Promise.resolve(),
      status: "loaded",
      check: () => true,
      add() {}, delete() {}, forEach() {}, addEventListener() {},
    };
    ok("document.fonts");
  } catch (e) { bad("document.fonts", e.message); }

  // ── 8. fetch polyfill（jsdom 无 fetch，CF 脚本要用）────
  try {
    if (typeof window.fetch !== "function") {
      window.fetch = async (input, init = {}) => {
        const url = typeof input === "string" ? input : input.url;
        const res = await fetch(url, {
          method: init.method || "GET",
          headers: init.headers || {},
          body: init.body,
          credentials: "include",
        });
        const text = await res.text();
        return {
          ok: res.ok, status: res.status, statusText: res.statusText,
          headers: { get: (k) => res.headers.get(k) },
          text: async () => text, json: async () => JSON.parse(text),
          url,
        };
      };
      ok("fetch (Node 全局 fetch 桥接)");
    } else ok("fetch (jsdom 已有)");
  } catch (e) {
    bad("fetch", e.message);
  }

  return report;
}

module.exports = { injectShims };
