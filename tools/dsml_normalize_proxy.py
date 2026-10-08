#!/usr/bin/env python3
"""DeepSeek DSML-normalizing local proxy.

Repairs the relay-side gap where DeepSeek DSML tool-call markup is not
normalized into standard Responses function_call items, which makes tools
silently not run and ends the turn. Streaming and non-streaming supported.
Listens on 127.0.0.1 only; never logs credentials.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("DSML_UPSTREAM", "https://your-gateway.example.com/v1")
HOST = os.environ.get("DSML_LISTEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("DSML_LISTEN_PORT", "8899"))
LOG_PATH = os.path.expanduser(os.environ.get("DSML_PROXY_LOG", "~/.codex/proxy/dsml-proxy.log"))

FW = "\uff5c"
P = rf"(?:{FW}|\|)\s*DSML\s*(?:{FW}|\|)?"
INVOKE_RE = re.compile(rf"<{P}\s*invoke\s+name\s*=\s*[\"']([^\"'>]+?)[\"']?\s*>", re.S)
PARAM_RE = re.compile(
    rf"<{P}\s*parameter\s+name\s*=\s*[\"']([^\"'>]+?)[\"'](?:\s+string\s*=\s*[\"'][^\"']*[\"'])?\s*>(.*?)"
    rf"(?=<{P}\s*parameter\s+name|<{P}\s*/?\s*invoke|\Z)",
    re.S,
)


# Anthropic/Claude 风格的裸 XML（无 DSML 标记包裹）。
# 实测：被测模型在强制文本协议时都可能输出这种形状。
# 只认「块结构」强信号，避免把讨论文本误判成工具调用。
XML_INVOKE_RE = re.compile(r"<invoke\s+name\s*=\s*[\"']([^\"'>]+?)[\"']\s*>", re.S)
XML_PARAM_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']([^\"'>]+?)[\"'][^>]*>(.*?)"
    r"(?=<parameter\s+name|</parameter>|</invoke>|\Z)",
    re.S,
)



def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def has_dsml(text):
    return bool(re.search(P, text))


def has_text_protocol(text):
    """是否含可解析的文本工具调用块（DSML 或裸 XML）。"""
    if has_dsml(text):
        return True
    if XML_INVOKE_RE.search(text) and ('</invoke>' in text or XML_PARAM_RE.search(text)):
        return True
    return False


def strip_markers(text):
    return re.sub(rf"</?{P}\s*/?\s*(?:parameter|invoke|calls|tool_?calls?)\s*>", "", text)


def parse_calls(text):
    calls = _parse_dsml(text)
    if calls:
        return calls
    return _parse_xml(text)


def _parse_xml(text):
    """解析 Anthropic 风格裸 XML。无法确定时返回空（fail-closed，不猜测）。"""
    calls = []
    for inv in XML_INVOKE_RE.finditer(text):
        name = inv.group(1).strip()
        if not name:
            continue
        start = inv.end()
        nxt = XML_INVOKE_RE.search(text, start)
        seg = text[start:nxt.start()] if nxt else text[start:]
        args = {}
        for pm in XML_PARAM_RE.finditer(seg):
            args[pm.group(1).strip()] = pm.group(2).strip()
        calls.append({"name": name, "arguments": json.dumps(args, ensure_ascii=False)})
    return calls


def _parse_dsml(text):
    calls = []
    for inv in INVOKE_RE.finditer(text):
        name = inv.group(1).strip()
        if not name:
            continue
        start = inv.end()
        nxt = INVOKE_RE.search(text, start)
        seg = text[start:nxt.start()] if nxt else text[start:]
        args = {}
        for pm in PARAM_RE.finditer(seg):
            args[pm.group(1).strip()] = strip_markers(pm.group(2)).strip()
        calls.append({"name": name, "arguments": json.dumps(args, ensure_ascii=False)})
    return calls


def ssl_ctx():
    return ssl.create_default_context()


def upstream_url(path: str) -> str:
    """拼接上游 URL，避免 base 与 path 中的 /v1 重复。

    UPSTREAM 可能形如 https://host/v1，而客户端传来的 path 也可能是 /v1/responses；
    两者直接相加会得到 /v1/v1/responses（实测 404）。这里做一次归一化：
    如果 UPSTREAM 已含 /v1 且 path 以 /v1 开头，则去掉 path 的前缀。
    """
    base = UPSTREAM.rstrip("/")
    p = path or "/"
    if base.endswith("/v1") and p.startswith("/v1"):
        p = p[3:] or "/"
    if not p.startswith("/"):
        p = "/" + p
    return base + p


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        return

    def _hdr(self, code, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if extra:
            for k, v in extra:
                self.send_header(k, v)

    def _write_json(self, code, payload):
        self._hdr(code, "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _fwd_headers(self):
        # 丢弃 accept-encoding：代理需要对上游响应做 JSON 解析与改写，
        # 若上游返回压缩内容会解析失败。此处强制请求未压缩响应，
        # 同时避免把客户端的编码偏好透传造成不一致。
        # 另外丢弃 host / content-length / connection，由 urllib 重新计算。
        drop = {"host", "content-length", "connection", "accept-encoding"}
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in drop}
        hdrs["Accept-Encoding"] = "identity"
        return hdrs

    def do_GET(self):
        # 仅根路径作为本地健康检查；其他路径（如 /v1/models）必须转发到上游，
        # 否则客户端会拿到错误的响应内容（实测 /v1/models 被误答为 {"status":"ok"}）。
        if self.path in ("/", ""):
            self._write_json(200, json.dumps({"status": "ok"}).encode())
            return
        req = urllib.request.Request(upstream_url(self.path), headers=self._fwd_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx()) as r:
                raw, code = r.read(), r.status
                ctype = r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
            ctype = "application/json"
        except Exception as e:
            log("GET upstream EXC %s: %r" % (type(e).__name__, e))
            self._write_json(502, json.dumps({"error": {"message": str(e)}}).encode())
            return
        self._hdr(code, ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        log("POST %s len=%d ua=%s" % (self.path, n, (self.headers.get("User-Agent") or "-")[:40]))
        stream = False
        try:
            stream = json.loads(body).get("stream") is True
        except Exception:
            stream = False
        if stream:
            self._stream(body)
        else:
            self._nonstream(body)

    def _nonstream(self, body):
        req = urllib.request.Request(upstream_url(self.path), data=body,
                                     headers=self._fwd_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300, context=ssl_ctx()) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            detail = e.read()
            log("nonstream upstream HTTPError %d: %s" % (e.code, detail[:300].decode("utf-8", "ignore")))
            self._write_json(e.code, detail)
            return
        except Exception as e:
            log("nonstream upstream EXC %s: %r" % (type(e).__name__, e))
            self._write_json(502, json.dumps({"error": {"message": str(e)}}).encode())
            return
        try:
            d = json.loads(raw)
        except Exception:
            self._write_json(code, raw)
            return
        out = d.get("output")
        if isinstance(out, list):
            new_items, changed = [], False
            for it in out:
                if not isinstance(it, dict) or it.get("type") != "message":
                    new_items.append(it)
                    continue
                text = "".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                if not has_dsml(text):
                    new_items.append(it)
                    continue
                calls = parse_calls(text)
                if not calls:
                    new_items.append(it)
                    continue
                changed = True
                for c in calls:
                    new_items.append({
                        "type": "function_call",
                        "id": "fc_" + uuid.uuid4().hex[:20],
                        "call_id": "call_" + uuid.uuid4().hex[:20],
                        "name": c["name"], "arguments": c["arguments"],
                        "status": "completed",
                    })
            if changed:
                d = dict(d)
                d["output"] = new_items
                log("nonstream: normalized DSML -> function_call")
        self._write_json(code, json.dumps(d, ensure_ascii=False).encode())

    def _stream(self, body):
        req = urllib.request.Request(upstream_url(self.path), data=body,
                                     headers=self._fwd_headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=300, context=ssl_ctx())
        except urllib.error.HTTPError as e:
            detail = e.read()
            log("stream upstream HTTPError %d: %s" % (e.code, detail[:300].decode("utf-8", "ignore")))
            self._write_json(e.code, detail)
            return
        except Exception as e:
            log("stream upstream EXC %s: %r" % (type(e).__name__, e))
            self._write_json(502, json.dumps({"error": {"message": str(e)}}).encode())
            return

        # 流式响应不使用 Content-Length，靠连接关闭标记正文结束。
        # 必须同时把 close_connection 设为 True，否则 HTTP/1.1 默认 keep-alive，
        # 客户端会一直等后续数据并最终报 502（实测）。
        self._hdr(200, "text/event-stream",
                  [("Cache-Control", "no-cache"), ("Connection", "close")])
        self.close_connection = True
        self.end_headers()

        buf = ""
        msg_item = None
        pending = []
        saw = False
        replaced_items = None

        def emit(ev, data):
            self.wfile.write(("event: " + ev + "\n").encode())
            self.wfile.write(("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())

        for raw in resp:
            line = raw.decode("utf-8", "ignore")
            s = line.strip()
            if not s.startswith("data:"):
                # 丢弃上游的 event: 行与空行：事件名统一由 emit() 输出，
                # 否则会出现 "event: X\nevent: X\ndata:{...}" 的重复前缀，
                # 客户端无法解析并直接报 502（实测）。
                continue
            try:
                d = json.loads(s[5:].strip())
            except Exception:
                if msg_item is None:
                    self.wfile.write(raw)
                continue
            t = d.get("type", "")

            # response.completed 的 output 必须与前面已发出的事件一致，
            # 否则客户端会以 completed 为准而忽略转换结果。
            if t == "response.completed" and replaced_items is not None:
                r = d.get("response")
                if isinstance(r, dict):
                    r = dict(r)
                    r["output"] = replaced_items
                    d = dict(d)
                    d["response"] = r
                emit(t, d)
                continue

            if t == "response.output_item.added" and isinstance(d.get("item"), dict) \
                    and d["item"].get("type") == "message":
                msg_item = d
                continue

            if msg_item is not None:
                if t == "response.output_text.delta":
                    buf += d.get("delta", "")
                    if has_text_protocol(buf):
                        saw = True
                    continue
                if t in ("response.output_text.done", "response.content_part.added",
                         "response.content_part.done"):
                    continue
                if t == "response.output_item.done" and isinstance(d.get("item"), dict) \
                        and d["item"].get("type") == "message":
                    mid = msg_item["item"]["id"]
                    if saw and has_text_protocol(buf):
                        calls = parse_calls(buf)
                        if calls:
                            log("stream: normalized %d DSML call(s)" % len(calls))
                            replaced_items = []
                            for c in calls:
                                cid = "call_" + uuid.uuid4().hex[:20]
                                fid = "fc_" + uuid.uuid4().hex[:20]
                                replaced_items.append({
                                    "type": "function_call", "id": fid, "call_id": cid,
                                    "name": c["name"], "arguments": c["arguments"],
                                    "status": "completed",
                                })
                                emit("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                     "item": {"type": "function_call", "id": fid, "call_id": cid,
                                              "name": c["name"], "arguments": "", "status": "in_progress"}})
                                emit("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                     "item_id": fid, "output_index": 0, "delta": c["arguments"]})
                                emit("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                                     "item_id": fid, "output_index": 0, "arguments": c["arguments"]})
                                emit("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                     "item": {"type": "function_call", "id": fid, "call_id": cid,
                                              "name": c["name"], "arguments": c["arguments"], "status": "completed"}})
                        else:
                            emit("response.output_item.added", msg_item)
                            emit("response.output_text.delta", {"type": "response.output_text.delta",
                                 "item_id": mid, "output_index": 0, "delta": strip_markers(buf)})
                            emit("response.output_item.done", d)
                    else:
                        for ev in pending:
                            self.wfile.write(ev.encode())
                        emit("response.output_item.added", msg_item)
                        if buf:
                            emit("response.output_text.delta", {"type": "response.output_text.delta",
                                 "item_id": mid, "output_index": 0, "delta": buf})
                        emit("response.output_item.done", d)
                    pending = []
                    msg_item = None
                    buf = ""
                    saw = False
                    continue
                pending.append("event: " + t + "\ndata: " + json.dumps(d, ensure_ascii=False) + "\n\n")
                continue

            emit(t, d)

        self.wfile.flush()


def main():
    # 允许快速重绑定：KeepAlive 重启时旧 socket 可能仍在 TIME_WAIT，
    # 不加 SO_REUSEADDR 会导致新进程绑定失败，代理长时间不可用
    # （此刻所有模型请求都要经过本代理，绑定失败等于全局中断）。
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log("listening %s:%d -> %s" % (HOST, PORT, UPSTREAM))
    print("dsml-normalize-proxy on %s:%d -> %s" % (HOST, PORT, UPSTREAM))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
