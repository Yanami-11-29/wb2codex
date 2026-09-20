#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
responses_bridge.py —— OpenAI Responses -> Chat Completions 本地协议转换层。

用途：Codex 客户端只会说 Responses 协议，而 workbuddy2api 网关只提供
      POST /v1/chat/completions。本代理监听本地端口，接收 Responses 请求，
      转成 Chat Completions 转发给网关，再把结果翻译回 Responses 格式。

      Codex 侧无需任何改动（wire_api 继续是 responses）。

用法：
    python responses_bridge.py [--listen 127.0.0.1:7864]
                               [--upstream http://127.0.0.1:7863]
                               [--key <网关 api_key>]

仅使用标准库，无第三方依赖。
"""

import argparse
import hashlib
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request, error

DEFAULT_UPSTREAM = "http://127.0.0.1:7865"
DEFAULT_LISTEN = "127.0.0.1:7863"


# ---------------------------------------------------------------- 请求转换

def _text_of(content):
    """把 Responses 的 content（字符串或分块列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t in ("input_text", "text", "output_text"):
                out.append(p.get("text", ""))
            elif t == "input_image":
                # 网关侧不做多模态透传，保留占位说明以免上游收到空消息
                out.append("[image omitted]")
        return "".join(out)
    return ""


def _convert_tools(tools):
    """Responses tools -> Chat Completions tools。"""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                },
            })
    return out or None


def _merge_assistant(msgs):
    """合并相邻的 assistant 消息。

    Responses 里「先说话再调工具」是两个 item（message + function_call），
    直译会变成两条连续的 assistant 消息。部分上游对连续 assistant 敏感，
    且会把 tool_calls 与它前面的文本割裂，这里合成一条。
    """
    out = []
    for m in msgs:
        prev = out[-1] if out else None
        if m.get("role") == "assistant" and prev is not None and prev.get("role") == "assistant":
            c = (prev.get("content") or "") + (m.get("content") or "")
            if c:
                prev["content"] = c
            tcs = (prev.get("tool_calls") or []) + (m.get("tool_calls") or [])
            if tcs:
                prev["tool_calls"] = tcs
            continue
        out.append(m)
    return out


def _sanitize_tool_sequence(msgs):
    """强制 tool_calls 与 tool 结果严格配对。

    上游会校验整个 messages 数组，任一不成立即返回
    11148 tool_call_sequence_broken（HTTP 503）：
      1. role=tool 的 tool_call_id 必须在紧前的 assistant.tool_calls 里（无孤儿结果）；
      2. assistant.tool_calls 里的每个 id 都必须有结果（无悬空调用）。

    Codex 在会话被中断、历史重放或 compaction 后，很容易留下悬空的
    function_call（没有对应 function_call_output），本函数在出站前修补。
    """
    out = []
    pending = []          # 当前未闭合的 tool_call id
    pending_owner = None  # 承载它们的 assistant 消息

    def close_pending():
        nonlocal pending, pending_owner
        if pending and pending_owner is not None:
            kept = [tc for tc in (pending_owner.get("tool_calls") or [])
                    if tc.get("id") not in pending]
            if kept:
                pending_owner["tool_calls"] = kept
            else:
                pending_owner.pop("tool_calls", None)
                # 只剩工具调用的空壳消息 -> 整条删掉
                if not (pending_owner.get("content") or "").strip():
                    try:
                        out.remove(pending_owner)
                    except ValueError:
                        pass
        pending = []
        pending_owner = None

    for m in msgs:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            close_pending()
            ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            if not ids:
                m = dict(m)
                m.pop("tool_calls", None)
                if (m.get("content") or "").strip():
                    out.append(m)
                continue
            out.append(m)
            pending = list(ids)
            pending_owner = m
        elif role == "tool":
            tid = m.get("tool_call_id")
            if not tid or tid not in pending:
                continue      # 孤儿结果，丢弃
            out.append(m)
            pending = [i for i in pending if i != tid]
            if not pending:
                pending_owner = None
        else:
            if role in ("user", "system"):
                close_pending()
            out.append(m)

    close_pending()
    return out


def _shape(msgs):
    """消息序列的紧凑形状，用于日志诊断（不落盘正文）。"""
    parts = []
    for m in msgs:
        r = m.get("role", "?")
        if r == "assistant" and m.get("tool_calls"):
            ids = ",".join((tc.get("id") or "?") for tc in m["tool_calls"])
            parts.append("assistant[tc:%s]" % ids)
        elif r == "tool":
            parts.append("tool[%s]" % (m.get("tool_call_id") or "?"))
        else:
            parts.append(r)
    return " > ".join(parts)


def _conv_fingerprint(body):
    """为一次对话生成稳定的会话指纹。

    网关用 body 里的 conversation_id 作为两处关键键的哈希源：
      - prompt_cache_key（格式 wb2a-<uid8>-<convHex>）
      - X-Conversation-ID 头（空则不发）
    桥接此前从不发送 conversation_id，于是**所有 Codex 会话都塌缩成同一个
    cache key**：上游把不同对话的前缀缓存混在一起。一旦某次对话里留下未闭合的
    工具调用，污染会扩散到后续每一个新会话——这解释了为什么「开新会话重试」
    未必有效：新会话复用的仍是同一个键。

    指纹取「instructions + 首个 user 文本 + 工具名集合」，同一会话多轮稳定，
    不同会话几乎必然不同。
    """
    h = hashlib.sha256()
    instr = body.get("instructions") or body.get("system") or ""
    h.update(instr.encode("utf-8", "replace")[:4096])

    first_user = ""
    inp = body.get("input")
    if isinstance(inp, str):
        first_user = inp
    elif isinstance(inp, list):
        for it in inp:
            if not isinstance(it, dict):
                continue
            if it.get("type", "message") == "message" and it.get("role") == "user":
                first_user = _text_of(it.get("content"))
                break
    h.update(first_user.encode("utf-8", "replace")[:4096])

    names = sorted(t.get("name", "") for t in (body.get("tools") or [])
                   if isinstance(t, dict))
    h.update(("|".join(names)).encode("utf-8", "replace"))
    return "cx-" + h.hexdigest()[:24]


def _reorder_tool_results(msgs):
    """把连续的工具结果按紧前 assistant.tool_calls 的顺序排列。

    部分上游要求 role=tool 的出现顺序与 tool_calls 声明顺序一致，
    顺序错位同样会触发 11148。
    """
    out = []
    i = 0
    n = len(msgs)
    while i < n:
        if msgs[i].get("role") == "tool":
            j = i
            run = []
            while j < n and msgs[j].get("role") == "tool":
                run.append(msgs[j])
                j += 1
            order = []
            for k in range(len(out) - 1, -1, -1):
                if out[k].get("role") == "assistant" and out[k].get("tool_calls"):
                    order = [tc.get("id") for tc in out[k]["tool_calls"]]
                    break

            def _key(m):
                tid = m.get("tool_call_id")
                return order.index(tid) if tid in order else len(order) + 1

            out.extend(sorted(run, key=_key))
            i = j
        else:
            out.append(msgs[i])
            i += 1
    return out


def to_chat(body):
    """Responses 请求体 -> Chat Completions 请求体。"""
    msgs = []

    instr = body.get("instructions") or body.get("system")
    if instr:
        msgs.append({"role": "system", "content": instr})

    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for it in inp:
            if not isinstance(it, dict):
                continue
            kind = it.get("type", "message")
            if kind == "function_call":
                msgs.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": it.get("call_id") or it.get("id"),
                        "type": "function",
                        "function": {
                            "name": it.get("name"),
                            "arguments": it.get("arguments", ""),
                        },
                    }],
                })
            elif kind in ("function_call_output", "tool_result"):
                msgs.append({
                    "role": "tool",
                    "tool_call_id": it.get("call_id") or it.get("tool_call_id"),
                    "content": _text_of(it.get("output", "")),
                })
            else:
                role = it.get("role", "user")
                if role not in ("user", "assistant", "system", "developer", "tool"):
                    role = "user"
                txt = _text_of(it.get("content"))
                if txt:
                    msgs.append({"role": role, "content": txt})

    if not msgs:
        msgs.append({"role": "user", "content": ""})

    before = _shape(msgs)
    msgs = _reorder_tool_results(_sanitize_tool_sequence(_merge_assistant(msgs)))
    after = _shape(msgs)
    if before != after:
        print("[bridge] tool-seq repaired: %s  ==>  %s" % (before, after), flush=True)
    else:
        print("[bridge] seq: %s" % after, flush=True)

    if not msgs:
        msgs.append({"role": "user", "content": ""})

    chat = {
        "model": body.get("model"),
        "messages": msgs,
        "stream": True,          # 网关出站强制 stream，本层统一按流处理
    }
    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
        if body.get("tool_choice") is not None:
            chat["tool_choice"] = body.get("tool_choice")
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        chat["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        chat["top_p"] = body["top_p"]

    # 会话隔离：让每个 Codex 会话拿到独立的 prompt_cache_key / X-Conversation-ID
    chat["conversation_id"] = _conv_fingerprint(body)
    return chat


# ---------------------------------------------------------------- 响应构造

def _new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex[:24]


def make_response_shell(model, status="in_progress"):
    return {
        "id": _new_id("resp"),
        "object": "response",
        "created_at": _now(),
        "status": status,
        "model": model,
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _now():
    import time
    return int(time.time())


def _map_usage(u):
    """网关 usage -> Responses usage。"""
    if not isinstance(u, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    inp = u.get("prompt_tokens")
    if inp is None:
        inp = (u.get("prompt_cache_miss_tokens", 0)
               + u.get("prompt_cache_hit_tokens", 0)
               + u.get("cache_creation_input_tokens", 0)
               + u.get("cached_tokens", 0))
    out = u.get("completion_tokens", 0)
    return {
        "input_tokens": inp or 0,
        "output_tokens": out or 0,
        "total_tokens": (inp or 0) + (out or 0),
    }


# ---------------------------------------------------------------- 处理类

class BridgeHandler(BaseHTTPRequestHandler):
    upstream = DEFAULT_UPSTREAM
    api_key = ""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具 ----

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _upstream_headers(self):
        h = {"Content-Type": "application/json"}
        auth = self.headers.get("Authorization")
        if auth:
            h["Authorization"] = auth
        elif self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def _proxy_get(self, path):
        """GET 直接转发（如 /v1/models）。"""
        url = self.upstream.rstrip("/") + path
        req = request.Request(url, headers=self._upstream_headers(), method="GET")
        try:
            with request.urlopen(req, timeout=30) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = json.dumps({"error": {"message": str(e)}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    # ---- 路由 ----

    def do_GET(self):
        self._proxy_get(self.path)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.rstrip("/").endswith("/responses"):
            self._handle_responses()
        elif path.rstrip("/").endswith("/chat/completions"):
            # 已经是 Chat 协议，直接透传
            self._proxy_post(path)
        else:
            self._proxy_post(path)

    def _proxy_post(self, path):
        body = self._read_body()
        url = self.upstream.rstrip("/") + path
        data = json.dumps(body).encode("utf-8")
        req = request.Request(url, data=data, headers=self._upstream_headers(), method="POST")
        try:
            with request.urlopen(req, timeout=180) as r:
                payload = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    # ---- 核心：Responses <-> Chat ----

    def _handle_responses(self):
        body = self._read_body()
        model = body.get("model", "")
        want_stream = bool(body.get("stream", True))
        chat = to_chat(body)

        url = self.upstream.rstrip("/") + "/v1/chat/completions"
        data = json.dumps(chat).encode("utf-8")
        req = request.Request(url, data=data, headers=self._upstream_headers(), method="POST")

        try:
            resp = request.urlopen(req, timeout=300)
        except error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except Exception as e:
            msg = json.dumps({"error": {"message": "bridge upstream error: %s" % e}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        if want_stream:
            self._stream_responses(resp, model)
        else:
            self._collect_responses(resp, model)

    def _emit(self, event, obj):
        payload = ("event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False)))
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _stream_responses(self, resp, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        shell = make_response_shell(model)
        msg_id = _new_id("msg")
        item_id = _new_id("item")

        self._emit("response.created", shell)

        self._emit("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": msg_id, "status": "in_progress",
                     "role": "assistant", "content": []},
        })
        self._emit("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

        text_buf = []
        tool_acc = {}
        usage = None
        finish_reason = None

        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue

            if obj.get("usage"):
                usage = obj["usage"]
            ch = (obj.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

            piece = delta.get("content") or delta.get("reasoning_content")
            if piece:
                text_buf.append(piece)
                self._emit("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": msg_id, "output_index": 0, "content_index": 0,
                    "delta": piece,
                })

            # 累积工具调用（网关可能分片返回）
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                acc = tool_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["name"] = fn["name"]
                if fn.get("arguments"):
                    acc["args"] += fn["arguments"]

        full_text = "".join(text_buf)

        self._emit("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "text": full_text,
        })
        self._emit("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_id, "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": full_text, "annotations": []},
        })
        self._emit("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "message", "id": msg_id, "status": "completed",
                     "role": "assistant",
                     "content": [{"type": "output_text", "text": full_text,
                                  "annotations": []}]},
        })

        # 工具调用项（流式下在末尾整体给出，arguments 已完整）
        # call_id 必须只生成一次并复用：上游若不返回 id，逐处 _new_id() 会让
        # output_item 事件与 response.completed 携带不同的 call_id，
        # Codex 回填的 function_call_output 就会对不上。
        for _, acc in tool_acc.items():
            if not acc.get("call_id"):
                acc["call_id"] = acc["id"] or _new_id("call")
        idx = 1
        for _, acc in sorted(tool_acc.items()):
            call_id = acc["call_id"]
            self._emit("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {"type": "function_call", "id": item_id + str(idx),
                         "call_id": call_id, "name": acc["name"],
                         "arguments": acc["args"], "status": "in_progress"},
            })
            self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": {"type": "function_call", "id": item_id + str(idx),
                         "call_id": call_id, "name": acc["name"],
                         "arguments": acc["args"], "status": "completed"},
            })
            idx += 1

        done = make_response_shell(model, status="completed")
        done["output"] = [{"type": "message", "id": msg_id, "status": "completed",
                           "role": "assistant",
                           "content": [{"type": "output_text", "text": full_text,
                                        "annotations": []}]}]
        for i, (_, acc) in enumerate(sorted(tool_acc.items()), start=1):
            done["output"].append({
                "type": "function_call", "id": item_id + str(i),
                "call_id": acc.get("call_id") or _new_id("call"),
                "name": acc["name"], "arguments": acc["args"], "status": "completed",
            })
        done["usage"] = _map_usage(usage)
        if finish_reason:
            done["status_details"] = {"reason": finish_reason}
        self._emit("response.completed", {"type": "response.completed", "response": done})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def _collect_responses(self, resp, model):
        """非流式：读完整个流再拼成一次响应。"""
        text_buf = []
        tool_acc = {}
        usage = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            ch = (obj.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            msg = ch.get("message") or {}
            piece = delta.get("content") or msg.get("content")
            if piece:
                text_buf.append(piece)
            for tc in (msg.get("tool_calls") or delta.get("tool_calls") or []):
                i = tc.get("index", len(tool_acc))
                fn = tc.get("function") or {}
                tool_acc[i] = {"id": tc.get("id"), "name": fn.get("name"),
                               "args": fn.get("arguments", "")}

        full = "".join(text_buf)
        out = make_response_shell(model, status="completed")
        out["output"] = [{"type": "message", "id": _new_id("msg"), "status": "completed",
                          "role": "assistant",
                          "content": [{"type": "output_text", "text": full,
                                       "annotations": []}]}]
        for i, (_, acc) in enumerate(sorted(tool_acc.items())):
            out["output"].append({
                "type": "function_call", "id": _new_id("fc"),
                "call_id": acc["id"] or _new_id("call"),
                "name": acc["name"], "arguments": acc["args"], "status": "completed",
            })
        out["usage"] = _map_usage(usage)

        payload = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser(description="Responses -> Chat Completions bridge")
    ap.add_argument("--listen", default=DEFAULT_LISTEN, help="监听地址，默认 127.0.0.1:7863")
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="网关地址，默认 http://127.0.0.1:7863")
    ap.add_argument("--key", default="", help="网关 api_key（客户端未带 Authorization 时使用）")
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    host = host or "127.0.0.1"

    BridgeHandler.upstream = args.upstream
    BridgeHandler.api_key = args.key

    srv = ThreadingHTTPServer((host, int(port)), BridgeHandler)
    sys.stderr.write("[bridge] listening on %s -> upstream %s\n" % (args.listen, args.upstream))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
