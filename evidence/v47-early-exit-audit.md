# N-14：畸形 body 打穿了日志本身（v4.7 实测→修复→复验）

> 发现方式：`scripts/e2e_local.py`（新写的**真进程** E2E）在 A16「服务日志里不许出现 Traceback」上变红。
> in-process 的 27 条用例和 `run.ps1 smoke` 都没抓到它 —— 因为**响应状态码是对的**。

## 1. 现场

```
[FAIL] A16_no_traceback_in_log — Traceback (most recent call last):
  File "solver/service.py", line 2048, in _parse_json
    payload = json.loads(raw.decode("utf-8"))
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)

During handling of the above exception, another exception occurred:
  File "solver/service.py", line 1746, in do_POST
    req = self._parse_json(raw)
errors.SolveError: [invalid-request/validate] 请求体不是合法 JSON：...

During handling of the above exception, another exception occurred:
  File "solver/service.py", line 2088, in wrapper
    return method(self, *args, **kwargs)
  File "solver/service.py", line 1866, in do_POST
    self._log(event="solve", req_id=req_id, host=_host_of(url), ok=False,
UnboundLocalError: cannot access local variable 'url' where it is not associated with a value
```

## 2. 根因

`do_POST` 把 `url` 的赋值放在 try 里：

```python
try:
    if body_error:                       # ← 空 body / 超大 body 在这里就抛
        raise SolveError(...)
    req = self._parse_json(raw)          # ← 畸形 JSON 在这里就抛
    url = req.get("url")                 # ← 赋值在这之后
```

而 `except SolveError` 分支无条件读 `url`：

```python
except SolveError as e:
    self._send(e.http_status, e.payload())
    self._log(event="solve", req_id=req_id, host=_host_of(url), ok=False, ...)   # 💥
```

于是**三条早退路径**（空 body / 畸形 JSON / 超大 body）全部走到 `UnboundLocalError`。

## 3. 为什么这么隐蔽

| 视角 | 看到的现象 |
|---|---|
| 调用方 | `400` + 正确的 `error_code=invalid-request` ✅（**完全正常**） |
| 只断言状态码的用例 | 全绿 ✅ |
| 审计文件 / 事件流 | **这条请求完全不存在** ❌ |
| 服务 stdout | 每个这样的请求打一整段 3 层嵌套 traceback ❌（畸形输入 = 日志洪水放大器） |

也就是说：**响应正确 + 审计缺失**。这类缺陷只有"进程级 + 看日志 + 看审计"的验收才能抓到。

## 4. 修复

`url` 在 try 之前绑定（一行），并让 `_host_of` 拿到空串（它本来就安全处理 `None`/`""`）：

```python
routed_by = None
url = ""          # 早退路径在 url 赋值之前就抛，except 里要能安全取 host
try:
```

## 5. 改前必红 / 改后必绿

```
# 修复前（两条用例都是红的，且 pytest 输出里能看到同一段 UnboundLocalError）
$ python -m pytest tests/test_governance.py -q -k "malformed_json or empty_body"
2 failed, 33 deselected

# 修复后
$ python -m pytest tests/test_governance.py -q -k "malformed_json or empty_body"
2 passed, 33 deselected
```

用例：
- `test_malformed_json_is_rejected_and_still_audited`
- `test_empty_body_is_rejected_and_still_audited`

断言的是**审计里必须有这一行**（`event=solve`、`error_code=invalid-request`），
—— 而不是"状态码是 400"，因为后者在缺陷存在时也是绿的。

## 6. 容器内复核（`evidence/v47-container.md` §4 第 5 项）

修复后的镜像里跑同一请求：

```
$ curl -X POST -d '{not json' .../solve       →  HTTP 400
$ docker logs cf-solver-1 | grep -c Traceback →  0
$ docker logs cf-solver-1 | grep '"event"'    →  {"event":"solve", ..., "error_code":"invalid-request", ...}
```

## 7. 残余风险

- `except Exception` 兜底分支里的 `traceback.print_exc()` 是**故意**保留的（未知异常要留现场），
  所以"日志里有 traceback"仍是**已知异常**的信号 —— 这条断言会持续有意义。
- 未改 `_read_body_raw` 对超大 body 的状态码（当前 `400 invalid-request`，未定义为 `413`）；
  文档没有承诺 413，故本轮不动，避免扩大变更面。
