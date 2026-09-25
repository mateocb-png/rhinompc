"""
Prueba offline (sin Rhino): carga el puente real con RhinoCommon/Eto simulados y
comprueba la cadena completa  cliente MCP → servidor MCP → puente TCP → hilo UI.
También prueba el bucle del chat con un cliente de Anthropic falso.

    uv run --with "mcp>=1.2,<2" python tests/test_offline.py
"""

import asyncio
import importlib.util
import json
import os
import queue
import socket
import sys
import threading
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 54399
os.environ["RHINO_MCP_PORT"] = str(PORT)

# ── Rhino / Eto simulados ────────────────────────────────────────────────────
ui_queue = queue.Queue()
ui_thread_ids = set()


def _ui_worker():
    ui_thread_ids.add(threading.get_ident())
    while True:
        ui_queue.get()()


threading.Thread(target=_ui_worker, daemon=True).start()

System = mock.MagicMock(name="System")
System.Action = lambda f: f
Rhino = mock.MagicMock(name="Rhino")
doc = Rhino.RhinoDoc.ActiveDoc
doc.BeginUndoRecord.return_value = 7
Eto = mock.MagicMock(name="Eto")
Eto.Forms.Application.Instance.AsyncInvoke = lambda f: ui_queue.put(f)
sticky = {}

sys.modules.update({
    "System": System, "System.Drawing": System.Drawing,
    "System.Collections": System.Collections, "System.Collections.Generic": System.Collections.Generic,
    "Rhino": Rhino, "Rhino.Geometry": Rhino.Geometry, "Rhino.DocObjects": Rhino.DocObjects,
    "scriptcontext": types.SimpleNamespace(sticky=sticky, doc=doc),
    "rhinoscriptsyntax": mock.MagicMock(name="rs"),
    "Eto": Eto, "Eto.Forms": Eto.Forms, "Eto.Drawing": Eto.Drawing,
})

spec = importlib.util.spec_from_file_location("bridge", os.path.join(ROOT, "rhino", "rhino_mcp_bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

results = []


def check(name, cond, detail=""):
    results.append(cond)
    print(("PASS " if cond else "FAIL ") + name + (("  → " + str(detail)) if detail and not cond else ""))


# Herramienta extra que registra en qué hilo corre.
seen = {}


def t_probe(p):
    seen["thread"] = threading.get_ident()
    return {"echo": p}


bridge.TOOLS["probe"] = t_probe

logs = []
server = bridge.BridgeServer(PORT, logs.append)
server.start()


def raw(tool, params=None):
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as s:
        s.sendall((json.dumps({"id": 1, "tool": tool, "params": params or {}}) + "\n").encode())
        return json.loads(s.makefile("rb").readline())


# ── 1. Protocolo del puente ──────────────────────────────────────────────────
r = raw("ping")
check("ping responde", r.get("ok") and r["result"]["pong"], r)

r = raw("probe", {"a": 1})
check("herramienta devuelve resultado", r.get("ok") and r["result"] == {"echo": {"a": 1}}, r)
check("se ejecuta en el hilo de UI", seen.get("thread") in ui_thread_ids)
check("abre y cierra registro de deshacer", doc.BeginUndoRecord.called and doc.EndUndoRecord.called)

r = raw("no_existe")
check("herramienta desconocida → error", not r["ok"] and "desconocida" in r["error"], r)

r = raw("execute_python", {"code": "x = 21\nprint('hola')\nresult = {'v': x * 2}"})
check("execute_python: result y stdout", r["ok"] and r["result"]["result"] == {"v": 42}
      and r["result"]["stdout"] == "hola\n", r)
r = raw("execute_python", {"code": "result = x + 1"})
check("execute_python: variables persisten", r["ok"] and r["result"]["result"] == 22, r)
r = raw("execute_python", {"code": "1/0"})
check("execute_python: excepción → error", not r["ok"] and "ZeroDivisionError" in r["error"], r)

# Varias peticiones en la misma conexión
with socket.create_connection(("127.0.0.1", PORT), timeout=10) as s:
    f = s.makefile("rwb")
    for i in range(3):
        f.write((json.dumps({"id": i, "tool": "ping"}) + "\n").encode())
    f.flush()
    ids = [json.loads(f.readline())["id"] for _ in range(3)]
check("varias peticiones por conexión", ids == [0, 1, 2], ids)


# ── 2. Servidor MCP real contra el puente ────────────────────────────────────
async def mcp_test():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(ROOT, "server", "rhino_mcp_server.py")],
                                   env={**os.environ, "RHINO_MCP_PORT": str(PORT)})
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            missing = (set(bridge.TOOLS) - {"probe"}) - tools
            check("MCP expone todas las herramientas del puente", not missing, missing)
            chat = {t["name"] for t in bridge.CHAT_TOOL_SCHEMAS}
            check("el chat interno expone las mismas herramientas", chat == set(bridge.TOOLS) - {"probe"},
                  set(bridge.TOOLS) ^ chat)
            r = await s.call_tool("ping", {})
            check("MCP ping → Rhino", not r.isError and "pong" in r.content[0].text, r)
            r = await s.call_tool("execute_python", {"code": "result = 'desde MCP'"})
            check("MCP execute_python", not r.isError and "desde MCP" in r.content[0].text, r)
            r = await s.call_tool("execute_python", {"code": "raise ValueError('boom')"})
            check("MCP propaga errores de Rhino", r.isError and "boom" in r.content[0].text, r)


asyncio.run(mcp_test())

server.stop()


async def mcp_down():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(ROOT, "server", "rhino_mcp_server.py")],
                                   env={**os.environ, "RHINO_MCP_PORT": str(PORT + 1)})
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            r = await s.call_tool("ping", {})
            check("Rhino cerrado → mensaje claro", r.isError and "rhino_mcp_bridge.py" in r.content[0].text, r)


asyncio.run(mcp_down())


# ── 3. Bucle del chat con API simulada ──────────────────────────────────────
class B:
    def __init__(self, **kw):
        self.__dict__.update(kw)


calls = []


class FakeMessages:
    def create(self, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return B(stop_reason="tool_use", content=[
                B(type="text", text="Creo una caja."),
                B(type="tool_use", id="tu_1", name="execute_python", input={"code": "result = 'caja'"}),
                B(type="tool_use", id="tu_2", name="no_existe", input={}),
            ])
        return B(stop_reason="end_turn", content=[B(type="text", text="Hecho.")])


fake_anthropic = types.SimpleNamespace(
    Anthropic=lambda **kw: types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages())))
sys.modules["anthropic"] = fake_anthropic

texts, statuses = [], []
agent = bridge.ChatAgent(lambda who, t: texts.append((who, t)), statuses.append)
agent.send("haz una caja", "sk-test", "claude-opus-5")
for _ in range(100):
    if not agent.busy and statuses and statuses[-1] in ("Listo", "Error"):
        break
    threading.Event().wait(0.05)

check("chat: termina en 'Listo'", statuses[-1] == "Listo", (statuses, texts))
check("chat: dos llamadas a la API", len(calls) == 2, len(calls))
tool_msg = agent.messages[2]["content"]
check("chat: resultados de herramientas en un solo mensaje",
      [b["tool_use_id"] for b in tool_msg] == ["tu_1", "tu_2"], tool_msg)
check("chat: resultado OK", "caja" in tool_msg[0]["content"][0]["text"], tool_msg[0])
check("chat: error marcado is_error", tool_msg[1].get("is_error") is True, tool_msg[1])
check("chat: muestra texto de Claude", ("Claude", "Hecho.") in texts, texts)
check("chat: usa modelo y herramientas", calls[0]["model"] == "claude-opus-5" and len(calls[0]["tools"]) == 22)

img = bridge._tool_result_content("capture_viewport", {"view": "Top", "image_png_base64": "AAA="})
check("captura → bloque de imagen", img[0]["type"] == "image" and img[0]["source"]["data"] == "AAA=")

print("\n%d/%d pruebas OK" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
