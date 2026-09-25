"""
Prueba de humo contra Rhino REAL. Solo usa la biblioteca estándar de Python.

1. Abre Rhino 8 y ejecuta rhino/rhino_mcp_bridge.py (ScriptEditor → Run).
2. En una terminal:   python tests/smoke_test_rhino.py
   (usa un documento nuevo o de pruebas: crea objetos en la capa 'MCP_Test' y luego los borra)

Guarda la captura de la vista en tests/smoke_capture.png.
"""

import base64
import json
import os
import socket
import sys
import traceback

HOST = os.environ.get("RHINO_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("RHINO_MCP_PORT", "54321"))
LAYER = "MCP_Test"
HERE = os.path.dirname(os.path.abspath(__file__))


def call(tool, **params):
    with socket.create_connection((HOST, PORT), timeout=150) as s:
        s.sendall((json.dumps({"id": 1, "tool": tool, "params": params}) + "\n").encode("utf-8"))
        resp = json.loads(s.makefile("rb").readline())
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    return resp["result"]


passed, failed = [], []
ids = {}


def step(name, fn):
    try:
        out = fn()
        passed.append(name)
        print("PASS", name, ("→ " + json.dumps(out, default=str)[:140]) if out is not None else "")
        return out
    except Exception as e:  # noqa: BLE001
        failed.append((name, str(e)))
        print("FAIL", name, "→", e)
        if os.environ.get("VERBOSE"):
            traceback.print_exc()
        return None


def main():
    try:
        info = call("ping")
    except OSError as e:
        print("No hay conexión con Rhino en %s:%d (%s).\nEjecuta rhino/rhino_mcp_bridge.py en Rhino primero." % (HOST, PORT, e))
        return 2
    print("Conectado a Rhino", info.get("rhino"), "\n")

    common = {"layer": LAYER}
    step("get_document_info", lambda: {k: v for k, v in call("get_document_info").items() if k != "layers"})
    step("create_layer", lambda: call("create_layer", name=LAYER + "::Sub", color="#ff8800"))

    def create(tool, key, **p):
        r = call(tool, **p, **common)
        ids[key] = r["id"]
        return r

    step("create_point", lambda: create("create_point", "pt", point=[0, 0, 0], name="p0"))
    step("create_line", lambda: create("create_line", "ln", start=[0, 0, 0], end=[10, 0, 0], color="#ff0000"))
    step("create_polyline", lambda: create("create_polyline", "pl", points=[[0, 0, 0], [5, 0, 0], [5, 5, 0]], closed=True))
    step("create_curve", lambda: create("create_curve", "cv", points=[[0, 0, 0], [3, 4, 0], [6, 0, 2], [9, 3, 0]]))
    step("create_circle", lambda: create("create_circle", "ci", center=[0, 0, 5], radius=2))
    step("create_box", lambda: create("create_box", "bx", corner=[20, 0, 0], size=[10, 10, 10]))
    step("create_sphere", lambda: create("create_sphere", "sp", center=[25, 5, 10], radius=4))
    step("create_cylinder", lambda: create("create_cylinder", "cy", base=[25, 5, -5], radius=2, height=20))
    step("create_text", lambda: create("create_text", "tx", text="MCP OK", point=[0, -5, 0], height=2))

    step("list_objects(layer)", lambda: {"count": call("list_objects", layer=LAYER)["count"]})
    step("get_object_info", lambda: [
        {k: o.get(k) for k in ("type", "length", "volume", "solid")}
        for o in call("get_object_info", ids=[i for i in (ids.get("ln"), ids.get("bx")) if i])["objects"]])
    step("set_object_attributes", lambda: call("set_object_attributes", ids=[ids["ln"]], name="linea",
                                               color="#00aa00", user_text={"origen": "smoke"}))
    step("select_objects", lambda: call("select_objects", ids=[ids["ln"], ids["ci"]]))
    step("list_objects(selected_only)", lambda: {"count": call("list_objects", selected_only=True)["count"]})
    step("transform translate+copy", lambda: call("transform_objects", ids=[ids["pt"]], translate=[0, 0, 3], copy=True))
    step("transform rotate+scale", lambda: call("transform_objects", ids=[ids["ci"]],
                                                rotate={"angle": 45, "axis": [1, 0, 0], "center": [0, 0, 5]},
                                                scale={"factor": 1.5, "center": [0, 0, 5]}))

    def boolean():
        r = call("boolean_operation", operation="difference", a_ids=[ids["bx"]], b_ids=[ids["cy"]])
        ids["bool"] = r["ids"][0]
        return r

    step("boolean_operation difference", boolean)
    step("run_command", lambda: call("run_command", command="_-Sphere 40,0,0 3 _Enter"))
    step("execute_python", lambda: call("execute_python", code=(
        "import Rhino.Geometry as G\n"
        "gid = doc.Objects.AddSphere(G.Sphere(G.Point3d(50,0,0), 2))\n"
        "print('ok')\nresult = str(gid)")))

    def capture():
        r = call("capture_viewport", view="Perspective", width=800, height=600, zoom_extents=True)
        path = os.path.join(HERE, "smoke_capture.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r.pop("image_png_base64")))
        r["saved"] = path
        return r

    step("capture_viewport", capture)
    step("undo (deshace el execute_python)", lambda: call("undo", steps=1))

    def cleanup():
        objs = call("list_objects", layer=LAYER, limit=1000)["objects"]
        objs += call("list_objects", layer=LAYER + "::Sub", limit=1000)["objects"]
        return call("delete_objects", ids=[o["id"] for o in objs])

    step("limpieza (delete_objects)", cleanup)
    print("\nNota: la esfera de run_command en (40,0,0) queda en la capa actual; bórrala a mano.")

    print("\n%d OK, %d fallos" % (len(passed), len(failed)))
    for name, err in failed:
        print("  ✗ %s: %s" % (name, err))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
