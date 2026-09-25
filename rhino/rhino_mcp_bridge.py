#! python3
# r: anthropic
"""
Rhino MCP Bridge — se ejecuta DENTRO de Rhino 8 (ScriptEditor, CPython 3).

Hace dos cosas:
  1. Levanta un servidor TCP en 127.0.0.1:<puerto> que recibe peticiones JSON
     (una por línea) del servidor MCP externo y las ejecuta en el hilo de UI de Rhino.
  2. Abre una ventana interna de Rhino ("Rhino MCP") con:
       - estado del puente (iniciar / detener, puerto)
       - log de cada herramienta ejecutada
       - un chat con Claude que usa exactamente las mismas herramientas,
         para iterar sin salir de Rhino.

Uso: en Rhino 8 → comando `ScriptEditor` → abrir este archivo → Run (F5).
     O bien: `_-RunPythonScript "ruta/a/rhino_mcp_bridge.py"`.
Ejecutarlo de nuevo solo vuelve a mostrar la ventana (el servidor no se duplica).
"""

import base64
import contextlib
import io
import json
import os
import socket
import threading
import time
import traceback

import System
import System.Drawing
import Rhino
import Rhino.Geometry as rg
import Rhino.DocObjects as rdo
import scriptcontext as sc
import rhinoscriptsyntax as rs
import Eto.Forms as forms
import Eto.Drawing as drawing
from System.Collections.Generic import List

HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("RHINO_MCP_PORT", "54321"))
UI_TIMEOUT_S = 120
DEFAULT_MODEL = "claude-opus-5"
STICKY_KEY = "rhino_mcp_bridge_state"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".rhinompc", "config.json")


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_config(data):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────────────────────

def _doc():
    return Rhino.RhinoDoc.ActiveDoc


def _pt(v):
    if v is None:
        return None
    z = float(v[2]) if len(v) > 2 else 0.0
    return rg.Point3d(float(v[0]), float(v[1]), z)


def _vec(v):
    z = float(v[2]) if len(v) > 2 else 0.0
    return rg.Vector3d(float(v[0]), float(v[1]), z)


def _color(c):
    if c is None:
        return None
    if isinstance(c, str):
        h = c.lstrip("#")
        return System.Drawing.Color.FromArgb(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return System.Drawing.Color.FromArgb(int(c[0]), int(c[1]), int(c[2]))


def _guid(s):
    return System.Guid(str(s))


def _find(obj_id):
    obj = _doc().Objects.FindId(_guid(obj_id))
    if obj is None:
        raise ValueError("Objeto no encontrado: %s" % obj_id)
    return obj


def _ensure_layer(full_path, color=None):
    """Crea (si hace falta) una capa por ruta completa 'Padre::Hijo' y devuelve su índice."""
    doc = _doc()
    idx = doc.Layers.FindByFullPath(full_path, -1)
    if idx >= 0:
        if color is not None:
            layer = doc.Layers[idx]
            layer.Color = _color(color)
        return idx
    parent_id = System.Guid.Empty
    path = ""
    for part in full_path.split("::"):
        path = part if not path else path + "::" + part
        idx = doc.Layers.FindByFullPath(path, -1)
        if idx < 0:
            layer = rdo.Layer()
            layer.Name = part
            if parent_id != System.Guid.Empty:
                layer.ParentLayerId = parent_id
            idx = doc.Layers.Add(layer)
        parent_id = doc.Layers[idx].Id
    if color is not None:
        doc.Layers[idx].Color = _color(color)
    return idx


def _attrs(p):
    doc = _doc()
    a = doc.CreateDefaultAttributes()
    if p.get("name"):
        a.Name = p["name"]
    if p.get("layer"):
        a.LayerIndex = _ensure_layer(p["layer"])
    if p.get("color") is not None:
        a.ObjectColor = _color(p["color"])
        a.ColorSource = rdo.ObjectColorSource.ColorFromObject
    return a


def _bbox_dict(bb):
    return {"min": [bb.Min.X, bb.Min.Y, bb.Min.Z], "max": [bb.Max.X, bb.Max.Y, bb.Max.Z]}


def _obj_summary(obj):
    doc = _doc()
    bb = obj.Geometry.GetBoundingBox(True)
    return {
        "id": str(obj.Id),
        "type": str(obj.ObjectType),
        "name": obj.Attributes.Name or "",
        "layer": doc.Layers[obj.Attributes.LayerIndex].FullPath,
        "bbox": _bbox_dict(bb) if bb.IsValid else None,
    }


def _added(guid, what):
    if guid == System.Guid.Empty:
        raise RuntimeError("Rhino no pudo crear: %s" % what)
    _doc().Views.Redraw()
    return {"id": str(guid), "created": what}


def _to_brep(obj):
    geo = obj.Geometry
    if isinstance(geo, rg.Brep):
        return geo
    brep = rg.Brep.TryConvertBrep(geo)
    if brep is None:
        raise ValueError("El objeto %s no es un sólido/superficie" % obj.Id)
    return brep


# ─────────────────────────────────────────────────────────────────────────────
# Herramientas (mismo nombre que en el servidor MCP)
# ─────────────────────────────────────────────────────────────────────────────

def t_get_document_info(p):
    doc = _doc()
    layers = [{"name": l.FullPath, "visible": l.IsVisible, "locked": l.IsLocked,
               "objects": len(doc.Objects.FindByLayer(l) or [])}
              for l in doc.Layers if not l.IsDeleted]
    return {
        "name": doc.Name,
        "path": doc.Path,
        "units": str(doc.ModelUnitSystem),
        "tolerance": doc.ModelAbsoluteTolerance,
        "object_count": doc.Objects.Count,
        "current_layer": doc.Layers.CurrentLayer.FullPath,
        "layers": layers,
        "views": [v.ActiveViewport.Name for v in doc.Views],
        "selected": [str(o.Id) for o in doc.Objects.GetSelectedObjects(False, False)],
    }


def t_list_objects(p):
    doc = _doc()
    layer = p.get("layer")
    otype = (p.get("type") or "").lower()
    limit = int(p.get("limit", 200))
    selected_only = bool(p.get("selected_only", False))
    out = []
    source = doc.Objects.GetSelectedObjects(False, False) if selected_only else doc.Objects
    for obj in source:
        if obj.IsDeleted:
            continue
        s = _obj_summary(obj)
        if layer and s["layer"] != layer:
            continue
        if otype and otype not in s["type"].lower():
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return {"count": len(out), "objects": out}


def t_get_object_info(p):
    result = []
    for oid in p["ids"]:
        obj = _find(oid)
        s = _obj_summary(obj)
        geo = obj.Geometry
        if isinstance(geo, rg.Curve):
            s["length"] = geo.GetLength()
            s["closed"] = geo.IsClosed
        if isinstance(geo, (rg.Brep, rg.Extrusion)):
            brep = _to_brep(obj)
            s["solid"] = brep.IsSolid
            vmp = rg.VolumeMassProperties.Compute(brep) if brep.IsSolid else None
            if vmp:
                s["volume"] = vmp.Volume
            amp = rg.AreaMassProperties.Compute(brep)
            if amp:
                s["area"] = amp.Area
        s["user_text"] = {k: obj.Attributes.GetUserString(k) for k in obj.Attributes.GetUserStrings().AllKeys}
        result.append(s)
    return {"objects": result}


def t_create_point(p):
    return _added(_doc().Objects.AddPoint(_pt(p["point"]), _attrs(p)), "point")


def t_create_line(p):
    return _added(_doc().Objects.AddLine(_pt(p["start"]), _pt(p["end"]), _attrs(p)), "line")


def t_create_polyline(p):
    pts = List[rg.Point3d]([_pt(v) for v in p["points"]])
    if p.get("closed") and pts.Count > 2:
        pts.Add(pts[0])
    return _added(_doc().Objects.AddPolyline(pts, _attrs(p)), "polyline")


def t_create_curve(p):
    pts = List[rg.Point3d]([_pt(v) for v in p["points"]])
    crv = rg.Curve.CreateInterpolatedCurve(pts, int(p.get("degree", 3)))
    if crv is None:
        raise RuntimeError("No se pudo interpolar la curva")
    return _added(_doc().Objects.AddCurve(crv, _attrs(p)), "curve")


def t_create_circle(p):
    normal = _vec(p.get("normal", [0, 0, 1]))
    circle = rg.Circle(rg.Plane(_pt(p["center"]), normal), float(p["radius"]))
    return _added(_doc().Objects.AddCircle(circle, _attrs(p)), "circle")


def t_create_box(p):
    a = _pt(p["corner"])
    s = p["size"]
    b = rg.Point3d(a.X + float(s[0]), a.Y + float(s[1]), a.Z + float(s[2]))
    box = rg.Box(rg.BoundingBox(a, b))
    return _added(_doc().Objects.AddBrep(box.ToBrep(), _attrs(p)), "box")


def t_create_sphere(p):
    sphere = rg.Sphere(_pt(p["center"]), float(p["radius"]))
    return _added(_doc().Objects.AddSphere(sphere, _attrs(p)), "sphere")


def t_create_cylinder(p):
    axis = _vec(p.get("axis", [0, 0, 1]))
    circle = rg.Circle(rg.Plane(_pt(p["base"]), axis), float(p["radius"]))
    cyl = rg.Cylinder(circle, float(p["height"]))
    return _added(_doc().Objects.AddBrep(cyl.ToBrep(True, True), _attrs(p)), "cylinder")


def t_create_text(p):
    plane = rg.Plane(_pt(p["point"]), rg.Vector3d.ZAxis)
    doc = _doc()
    guid = doc.Objects.AddText(p["text"], plane, float(p.get("height", 1.0)),
                               p.get("font", "Arial"), False, False)
    if guid != System.Guid.Empty:
        doc.Objects.ModifyAttributes(guid, _attrs(p), True)
    return _added(guid, "text")


def t_delete_objects(p):
    doc = _doc()
    n = 0
    for oid in p["ids"]:
        if doc.Objects.Delete(_guid(oid), True):
            n += 1
    doc.Views.Redraw()
    return {"deleted": n}


def t_transform_objects(p):
    doc = _doc()
    xf = rg.Transform.Identity
    if p.get("scale"):
        s = p["scale"]
        center = _pt(s.get("center", [0, 0, 0]))
        xf = rg.Transform.Scale(center, float(s["factor"])) * xf
    if p.get("rotate"):
        r = p["rotate"]
        import math
        xf = rg.Transform.Rotation(math.radians(float(r["angle"])), _vec(r.get("axis", [0, 0, 1])),
                                   _pt(r.get("center", [0, 0, 0]))) * xf
    if p.get("translate"):
        xf = rg.Transform.Translation(_vec(p["translate"])) * xf
    copy = bool(p.get("copy", False))
    new_ids = []
    for oid in p["ids"]:
        new_ids.append(str(doc.Objects.Transform(_guid(oid), xf, not copy)))
    doc.Views.Redraw()
    return {"ids": new_ids, "copied": copy}


def t_boolean_operation(p):
    doc = _doc()
    tol = doc.ModelAbsoluteTolerance
    op = p["operation"]
    a = List[rg.Brep]([_to_brep(_find(i)) for i in p["a_ids"]])
    b = List[rg.Brep]([_to_brep(_find(i)) for i in p.get("b_ids", [])])
    if op == "union":
        for x in b:
            a.Add(x)
        res = rg.Brep.CreateBooleanUnion(a, tol)
    elif op == "difference":
        res = rg.Brep.CreateBooleanDifference(a, b, tol)
    elif op == "intersection":
        res = rg.Brep.CreateBooleanIntersection(a, b, tol)
    else:
        raise ValueError("operation debe ser union | difference | intersection")
    if not res:
        raise RuntimeError("La operación booleana falló (revisa que los sólidos se toquen y estén cerrados)")
    attrs = _find(p["a_ids"][0]).Attributes.Duplicate()
    ids = [str(doc.Objects.AddBrep(r, attrs)) for r in res]
    if p.get("delete_inputs", True):
        for i in list(p["a_ids"]) + list(p.get("b_ids", [])):
            doc.Objects.Delete(_guid(i), True)
    doc.Views.Redraw()
    return {"ids": ids}


def t_set_object_attributes(p):
    doc = _doc()
    for oid in p["ids"]:
        obj = _find(oid)
        a = obj.Attributes.Duplicate()
        if "name" in p:
            a.Name = p["name"] or ""
        if p.get("layer"):
            a.LayerIndex = _ensure_layer(p["layer"])
        if p.get("color") is not None:
            a.ObjectColor = _color(p["color"])
            a.ColorSource = rdo.ObjectColorSource.ColorFromObject
        for k, v in (p.get("user_text") or {}).items():
            a.SetUserString(k, str(v))
        doc.Objects.ModifyAttributes(obj, a, True)
    doc.Views.Redraw()
    return {"updated": len(p["ids"])}


def t_create_layer(p):
    doc = _doc()
    idx = _ensure_layer(p["name"], p.get("color"))
    if p.get("make_current"):
        doc.Layers.SetCurrentLayerIndex(idx, True)
    return {"index": idx, "layer": doc.Layers[idx].FullPath}


def t_select_objects(p):
    doc = _doc()
    if p.get("clear", True):
        doc.Objects.UnselectAll()
    n = 0
    for oid in p.get("ids", []):
        if doc.Objects.Select(_guid(oid)):
            n += 1
    doc.Views.Redraw()
    return {"selected": n}


def t_run_command(p):
    doc = _doc()
    serial = rdo.RhinoObject.NextRuntimeSerialNumber
    ok = Rhino.RhinoApp.RunScript(p["command"], bool(p.get("echo", False)))
    new = [_obj_summary(o) for o in doc.Objects if o.RuntimeSerialNumber >= serial and not o.IsDeleted]
    doc.Views.Redraw()
    return {"success": bool(ok), "new_objects": new}


_PY_NAMESPACE = {}


def t_execute_python(p):
    ns = _PY_NAMESPACE
    ns.update({"Rhino": Rhino, "rg": rg, "rs": rs, "sc": sc, "System": System, "doc": _doc()})
    ns.pop("result", None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(p["code"], ns)
    _doc().Views.Redraw()
    res = ns.get("result")
    try:
        json.dumps(res)
    except (TypeError, ValueError):
        res = repr(res)
    return {"stdout": buf.getvalue()[-20000:], "result": res}


def t_capture_viewport(p):
    doc = _doc()
    view = doc.Views.ActiveView
    if p.get("view"):
        for v in doc.Views:
            if v.ActiveViewport.Name.lower() == p["view"].lower():
                view = v
                break
    if p.get("display_mode"):
        mode = Rhino.Display.DisplayModeDescription.FindByName(p["display_mode"])
        if mode:
            view.ActiveViewport.DisplayMode = mode
    if p.get("zoom_extents"):
        view.ActiveViewport.ZoomExtents()
    view.Redraw()
    w, h = int(p.get("width", 1024)), int(p.get("height", 768))
    bmp = view.CaptureToBitmap(System.Drawing.Size(w, h))
    ms = System.IO.MemoryStream()
    bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png)
    b64 = System.Convert.ToBase64String(ms.ToArray())
    ms.Dispose()
    bmp.Dispose()
    return {"view": view.ActiveViewport.Name, "width": w, "height": h, "image_png_base64": b64}


def t_undo(p):
    doc = _doc()
    steps = int(p.get("steps", 1))
    done = 0
    for _ in range(steps):
        if not doc.Undo():
            break
        done += 1
    doc.Views.Redraw()
    return {"undone": done}


TOOLS = {
    "get_document_info": t_get_document_info,
    "list_objects": t_list_objects,
    "get_object_info": t_get_object_info,
    "create_point": t_create_point,
    "create_line": t_create_line,
    "create_polyline": t_create_polyline,
    "create_curve": t_create_curve,
    "create_circle": t_create_circle,
    "create_box": t_create_box,
    "create_sphere": t_create_sphere,
    "create_cylinder": t_create_cylinder,
    "create_text": t_create_text,
    "delete_objects": t_delete_objects,
    "transform_objects": t_transform_objects,
    "boolean_operation": t_boolean_operation,
    "set_object_attributes": t_set_object_attributes,
    "create_layer": t_create_layer,
    "select_objects": t_select_objects,
    "run_command": t_run_command,
    "execute_python": t_execute_python,
    "capture_viewport": t_capture_viewport,
    "undo": t_undo,
}

# Herramientas que gestionan su propio registro de deshacer (o no modifican nada).
_NO_UNDO_WRAP = {"run_command", "undo", "get_document_info", "list_objects",
                 "get_object_info", "capture_viewport"}


def _run_tool_on_ui(name, params):
    """Ejecuta una herramienta en el hilo de UI de Rhino y espera el resultado."""
    if name not in TOOLS:
        raise ValueError("Herramienta desconocida: %s" % name)
    box = {}
    done = threading.Event()

    def work():
        doc = _doc()
        rec = None
        try:
            if name not in _NO_UNDO_WRAP:
                rec = doc.BeginUndoRecord("MCP: " + name)
            box["result"] = TOOLS[name](params or {})
        except Exception as e:  # noqa: BLE001 - se devuelve al cliente
            box["error"] = "%s: %s" % (type(e).__name__, e)
            box["trace"] = traceback.format_exc()
        finally:
            if rec is not None:
                doc.EndUndoRecord(rec)
            done.set()

    forms.Application.Instance.AsyncInvoke(System.Action(work))
    if not done.wait(UI_TIMEOUT_S):
        raise TimeoutError("Rhino no respondió en %ss (¿hay un comando interactivo abierto?)" % UI_TIMEOUT_S)
    if "error" in box:
        raise RuntimeError(box["error"])
    return box["result"]


# ─────────────────────────────────────────────────────────────────────────────
# Servidor TCP (JSON por líneas) para el servidor MCP externo
# ─────────────────────────────────────────────────────────────────────────────

class BridgeServer(object):
    def __init__(self, port, log):
        self.port = port
        self.log = log
        self._sock = None
        self._thread = None
        self.running = False

    def start(self):
        if self.running:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, self.port))
        s.listen(5)
        s.settimeout(1.0)
        self._sock = s
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="rhino-mcp-bridge", daemon=True)
        self._thread.start()
        self.log("Puente escuchando en %s:%d" % (HOST, self.port))

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.log("Puente detenido")

    def _loop(self):
        while self.running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            f = conn.makefile("rwb")
            for raw in f:
                try:
                    req = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                rid, name, params = req.get("id"), req.get("tool"), req.get("params") or {}
                t0 = time.time()
                try:
                    if name == "ping":
                        result = {"pong": True, "rhino": str(Rhino.RhinoApp.Version)}
                    else:
                        result = _run_tool_on_ui(name, params)
                    resp = {"id": rid, "ok": True, "result": result}
                    self.log("MCP ▸ %s (%.0f ms)" % (name, (time.time() - t0) * 1000))
                except Exception as e:  # noqa: BLE001
                    resp = {"id": rid, "ok": False, "error": str(e)}
                    self.log("MCP ✗ %s: %s" % (name, e))
                f.write((json.dumps(resp) + "\n").encode("utf-8"))
                f.flush()


# ─────────────────────────────────────────────────────────────────────────────
# Chat con Claude dentro de Rhino
# ─────────────────────────────────────────────────────────────────────────────

def _S(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_P3 = {"type": "array", "items": {"type": "number"}, "description": "[x, y, z]"}
_IDS = {"type": "array", "items": {"type": "string"}, "description": "GUIDs de objetos"}
_COMMON = {
    "name": {"type": "string"},
    "layer": {"type": "string", "description": "Ruta de capa, p.ej. 'Estructura::Pilares'. Se crea si no existe."},
    "color": {"type": "string", "description": "Color hex '#rrggbb'"},
}


def _with_common(props):
    d = dict(props)
    d.update(_COMMON)
    return d


CHAT_TOOL_SCHEMAS = [
    {"name": "get_document_info", "description": "Info del documento: unidades, capas, vistas, selección.",
     "input_schema": _S({})},
    {"name": "list_objects", "description": "Lista objetos (id, tipo, capa, bbox). Filtros opcionales.",
     "input_schema": _S({"layer": {"type": "string"}, "type": {"type": "string"},
                         "selected_only": {"type": "boolean"}, "limit": {"type": "integer"}})},
    {"name": "get_object_info", "description": "Detalle de objetos: longitud, área, volumen, user text.",
     "input_schema": _S({"ids": _IDS}, ["ids"])},
    {"name": "create_point", "description": "Crea un punto.",
     "input_schema": _S(_with_common({"point": _P3}), ["point"])},
    {"name": "create_line", "description": "Crea una línea.",
     "input_schema": _S(_with_common({"start": _P3, "end": _P3}), ["start", "end"])},
    {"name": "create_polyline", "description": "Crea una polilínea (closed=true la cierra).",
     "input_schema": _S(_with_common({"points": {"type": "array", "items": _P3}, "closed": {"type": "boolean"}}),
                        ["points"])},
    {"name": "create_curve", "description": "Curva interpolada por puntos.",
     "input_schema": _S(_with_common({"points": {"type": "array", "items": _P3}, "degree": {"type": "integer"}}),
                        ["points"])},
    {"name": "create_circle", "description": "Crea un círculo.",
     "input_schema": _S(_with_common({"center": _P3, "radius": {"type": "number"}, "normal": _P3}),
                        ["center", "radius"])},
    {"name": "create_box", "description": "Caja alineada a ejes desde una esquina y tamaño [dx,dy,dz].",
     "input_schema": _S(_with_common({"corner": _P3, "size": _P3}), ["corner", "size"])},
    {"name": "create_sphere", "description": "Crea una esfera.",
     "input_schema": _S(_with_common({"center": _P3, "radius": {"type": "number"}}), ["center", "radius"])},
    {"name": "create_cylinder", "description": "Cilindro cerrado desde base, eje, radio y altura.",
     "input_schema": _S(_with_common({"base": _P3, "axis": _P3, "radius": {"type": "number"},
                                      "height": {"type": "number"}}), ["base", "radius", "height"])},
    {"name": "create_text", "description": "Texto en el plano XY.",
     "input_schema": _S(_with_common({"text": {"type": "string"}, "point": _P3, "height": {"type": "number"},
                                      "font": {"type": "string"}}), ["text", "point"])},
    {"name": "delete_objects", "description": "Borra objetos.", "input_schema": _S({"ids": _IDS}, ["ids"])},
    {"name": "transform_objects",
     "description": "Mueve/rota/escala objetos. Orden: escala → rotación → traslación. copy=true duplica.",
     "input_schema": _S({"ids": _IDS, "translate": _P3,
                         "rotate": {"type": "object", "properties": {"angle": {"type": "number", "description": "grados"},
                                                                     "axis": _P3, "center": _P3}},
                         "scale": {"type": "object", "properties": {"factor": {"type": "number"}, "center": _P3}},
                         "copy": {"type": "boolean"}}, ["ids"])},
    {"name": "boolean_operation", "description": "Booleana entre sólidos: union | difference | intersection.",
     "input_schema": _S({"operation": {"type": "string", "enum": ["union", "difference", "intersection"]},
                         "a_ids": _IDS, "b_ids": _IDS, "delete_inputs": {"type": "boolean"}},
                        ["operation", "a_ids"])},
    {"name": "set_object_attributes", "description": "Cambia nombre, capa, color o user text de objetos.",
     "input_schema": _S(_with_common({"ids": _IDS, "user_text": {"type": "object"}}), ["ids"])},
    {"name": "create_layer", "description": "Crea capa (ruta 'A::B'), opcionalmente color y actual.",
     "input_schema": _S({"name": {"type": "string"}, "color": {"type": "string"},
                         "make_current": {"type": "boolean"}}, ["name"])},
    {"name": "select_objects", "description": "Selecciona objetos (clear=true limpia antes).",
     "input_schema": _S({"ids": _IDS, "clear": {"type": "boolean"}})},
    {"name": "run_command",
     "description": "Ejecuta un comando de Rhino como script, p.ej. \"_-Loft _Pause\" o "
                    "\"_-Export \\\"C:\\\\tmp\\\\a.stl\\\" _Enter\". Usa la versión con guion para evitar diálogos. "
                    "Devuelve los objetos nuevos creados.",
     "input_schema": _S({"command": {"type": "string"}, "echo": {"type": "boolean"}}, ["command"])},
    {"name": "execute_python",
     "description": "Ejecuta Python 3 dentro de Rhino con Rhino, rg (Rhino.Geometry), rs (rhinoscriptsyntax), "
                    "sc, doc y System ya importados. Asigna a `result` lo que quieras devolver; print() también "
                    "se devuelve. Las variables persisten entre llamadas.",
     "input_schema": _S({"code": {"type": "string"}}, ["code"])},
    {"name": "capture_viewport", "description": "Captura una vista como imagen PNG para ver el resultado.",
     "input_schema": _S({"view": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"},
                         "zoom_extents": {"type": "boolean"}, "display_mode": {"type": "string"}})},
    {"name": "undo", "description": "Deshace N pasos.", "input_schema": _S({"steps": {"type": "integer"}})},
]

SYSTEM_PROMPT = (
    "Eres un asistente de modelado 3D que controla Rhino 8 mediante herramientas. "
    "Trabaja de forma iterativa: consulta el documento, crea o modifica geometría, y usa "
    "capture_viewport para comprobar visualmente el resultado antes de darlo por terminado. "
    "Usa las unidades del documento. Prefiere herramientas específicas; para lo que no cubran, "
    "usa execute_python con RhinoCommon o run_command. Responde en el idioma del usuario, de forma breve."
)


def _tool_result_content(name, result):
    """Convierte el resultado de una herramienta en bloques de contenido para la API."""
    if name == "capture_viewport" and isinstance(result, dict) and result.get("image_png_base64"):
        meta = {k: v for k, v in result.items() if k != "image_png_base64"}
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": result["image_png_base64"]}},
            {"type": "text", "text": json.dumps(meta)},
        ]
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > 50000:
        text = text[:50000] + "…(truncado)"
    return [{"type": "text", "text": text}]


class ChatAgent(object):
    def __init__(self, on_text, on_status):
        self.on_text = on_text
        self.on_status = on_status
        self.messages = []
        self.busy = False
        self.cancel = False

    def reset(self):
        self.messages = []

    def send(self, user_text, api_key, model):
        if self.busy:
            return
        self.busy = True
        self.cancel = False
        threading.Thread(target=self._run, args=(user_text, api_key, model), daemon=True).start()

    def _run(self, user_text, api_key, model):
        checkpoint = len(self.messages)
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
            self.messages.append({"role": "user", "content": user_text})
            for _ in range(40):  # tope de iteraciones de herramientas por turno
                if self.cancel:
                    self.on_status("Cancelado")
                    break
                self.on_status("Pensando…")
                resp = client.beta.messages.create(
                    model=model,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    tools=CHAT_TOOL_SCHEMAS,
                    messages=self.messages,
                    thinking={"type": "adaptive"},
                    # Si Claude declina, la API reintenta con un modelo alternativo.
                    betas=["server-side-fallback-2026-07-01"],
                    extra_body={"fallbacks": "default"},
                )
                # Guardamos el contenido completo (incluye bloques thinking/fallback).
                self.messages.append({"role": "assistant", "content": resp.content})
                for block in resp.content:
                    if block.type == "text" and block.text.strip():
                        self.on_text("Claude", block.text)
                if resp.stop_reason == "refusal":
                    self.on_text("Sistema", "Claude declinó esta petición.")
                    break
                if resp.stop_reason != "tool_use":
                    if resp.stop_reason == "max_tokens":
                        self.on_text("Sistema", "Respuesta cortada por límite de tokens.")
                    break
                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    self.on_status("Ejecutando %s…" % block.name)
                    self.on_text("herramienta", "%s %s" % (block.name, json.dumps(block.input, ensure_ascii=False)[:300]))
                    try:
                        out = _run_tool_on_ui(block.name, block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": _tool_result_content(block.name, out)})
                    except Exception as e:  # noqa: BLE001
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": str(e), "is_error": True})
                        self.on_text("herramienta", "✗ %s" % e)
                self.messages.append({"role": "user", "content": results})
            self.on_status("Listo")
        except Exception as e:  # noqa: BLE001
            # Si el turno falló a medias, volvemos al historial válido anterior.
            del self.messages[checkpoint:]
            self.on_text("Sistema", "Error: %s" % e)
            self.on_status("Error")
        finally:
            self.busy = False


# ─────────────────────────────────────────────────────────────────────────────
# Ventana interna de Rhino (Eto)
# ─────────────────────────────────────────────────────────────────────────────

def _ui(fn):
    forms.Application.Instance.AsyncInvoke(System.Action(fn))


class BridgeWindow(object):
    def __init__(self, state):
        self.state = state
        f = forms.Form()
        f.Title = "Rhino MCP"
        f.ClientSize = drawing.Size(480, 700)
        f.Resizable = True
        f.Owner = Rhino.UI.RhinoEtoApp.MainWindow
        self.form = f

        # — Puente —
        self.status = forms.Label()
        self.port_box = forms.NumericStepper()
        self.port_box.MinValue = 1024
        self.port_box.MaxValue = 65535
        self.port_box.Value = state["server"].port
        self.toggle_btn = forms.Button()
        self.toggle_btn.Click += self._on_toggle

        self.log_area = forms.TextArea()
        self.log_area.ReadOnly = True
        self.log_area.Height = 120
        self.log_area.Font = drawing.Fonts.Monospace(9)

        # — Chat —
        self.chat_area = forms.TextArea()
        self.chat_area.ReadOnly = True
        self.chat_area.Wrap = True
        self.input_box = forms.TextArea()
        self.input_box.Height = 70
        self.input_box.Wrap = True
        self.input_box.KeyDown += self._on_key
        self.send_btn = forms.Button()
        self.send_btn.Text = "Enviar (Ctrl+Enter)"
        self.send_btn.Click += self._on_send
        self.stop_btn = forms.Button()
        self.stop_btn.Text = "Parar"
        self.stop_btn.Click += self._on_cancel
        self.reset_btn = forms.Button()
        self.reset_btn.Text = "Nueva conversación"
        self.reset_btn.Click += self._on_reset
        self.chat_status = forms.Label()
        self.chat_status.Text = "Listo"

        cfg = _load_config()
        self.key_box = forms.PasswordBox()
        self.key_box.Text = state.get("api_key") or cfg.get("api_key", "")
        self.remember_box = forms.CheckBox()
        self.remember_box.Text = "Recordar en este equipo"
        self.remember_box.Checked = bool(cfg.get("api_key"))
        self.model_box = forms.TextBox()
        self.model_box.Text = state.get("model") or cfg.get("model", DEFAULT_MODEL)

        f.Content = self._layout()
        f.Closed += self._on_closed
        self._refresh_status()
        for line in state["log_lines"][-200:]:
            self.log_area.Append(line + "\n", True)
        for line in state["chat_lines"]:
            self.chat_area.Append(line, True)

    def _row(self, *ctrls, **kw):
        s = forms.StackLayout()
        s.Orientation = forms.Orientation.Horizontal
        s.Spacing = 6
        s.VerticalContentAlignment = forms.VerticalAlignment.Center
        expand = kw.get("expand")
        for c in ctrls:
            s.Items.Add(forms.StackLayoutItem(c, c is expand))
        return s

    def _label(self, text, bold=False):
        l = forms.Label()
        l.Text = text
        if bold:
            l.Font = drawing.SystemFonts.Bold()
        return l

    def _layout(self):
        root = forms.StackLayout()
        root.Orientation = forms.Orientation.Vertical
        root.Padding = drawing.Padding(10)
        root.Spacing = 6
        root.HorizontalContentAlignment = forms.HorizontalAlignment.Stretch

        def add(c, expand=False):
            root.Items.Add(forms.StackLayoutItem(c, expand))

        add(self._label("Puente MCP (Claude Code / Claude Desktop)", True))
        add(self._row(self._label("Puerto"), self.port_box, self.toggle_btn, self.status))
        add(self.log_area)
        add(self._label("Chat con Claude dentro de Rhino", True))
        add(self._row(self._label("Modelo"), self.model_box, expand=self.model_box))
        add(self._row(self._label("API key"), self.key_box, self.remember_box, expand=self.key_box))
        add(self.chat_area, True)
        add(self.input_box)
        add(self._row(self.send_btn, self.stop_btn, self.reset_btn, self.chat_status))
        return root

    # — eventos —
    def _refresh_status(self):
        srv = self.state["server"]
        self.status.Text = "● activo" if srv.running else "○ detenido"
        self.status.TextColor = drawing.Colors.Green if srv.running else drawing.Colors.Gray
        self.toggle_btn.Text = "Detener" if srv.running else "Iniciar"
        self.port_box.Enabled = not srv.running

    def _on_toggle(self, sender, e):
        srv = self.state["server"]
        try:
            if srv.running:
                srv.stop()
            else:
                srv.port = int(self.port_box.Value)
                srv.start()
        except OSError as ex:
            self.append_log("No se pudo abrir el puerto: %s" % ex)
        self._refresh_status()

    def _on_key(self, sender, e):
        if e.Key == forms.Keys.Enter and e.Control:
            e.Handled = True
            self._on_send(sender, e)

    def _on_send(self, sender, e):
        text = (self.input_box.Text or "").strip()
        agent = self.state["agent"]
        if not text or agent.busy:
            return
        self.state["api_key"] = (self.key_box.Text or "").strip()
        self.state["model"] = (self.model_box.Text or DEFAULT_MODEL).strip()
        if not self.state["api_key"] and not os.environ.get("ANTHROPIC_API_KEY"):
            self.append_chat("Sistema",
                             "Falta la API key. Crea una en https://platform.claude.com/settings/keys "
                             "(empieza por sk-ant-), pégala en el campo 'API key' y vuelve a enviar.\n"
                             "Nota: el chat interno usa la API de Anthropic (se factura aparte). "
                             "Si usas Claude Code o Claude Desktop con el servidor MCP, no necesitas clave.")
            return
        try:
            cfg = _load_config()
            cfg["model"] = self.state["model"]
            if self.remember_box.Checked:
                cfg["api_key"] = self.state["api_key"]
            else:
                cfg.pop("api_key", None)
            _save_config(cfg)
        except OSError as ex:
            self.append_log("No se pudo guardar la configuración: %s" % ex)
        self.input_box.Text = ""
        self.append_chat("Tú", text)
        agent.send(text, self.state["api_key"] or None, self.state["model"])

    def _on_cancel(self, sender, e):
        self.state["agent"].cancel = True

    def _on_reset(self, sender, e):
        self.state["agent"].reset()
        self.state["chat_lines"] = []
        self.chat_area.Text = ""
        self.chat_status.Text = "Nueva conversación"

    def _on_closed(self, sender, e):
        self.state["window"] = None

    # — llamadas thread-safe —
    def append_log(self, line):
        self.log_area.Append(line + "\n", True)

    def append_chat(self, who, text):
        line = "%s: %s\n\n" % (who, text)
        self.state["chat_lines"].append(line)
        self.chat_area.Append(line, True)

    def set_chat_status(self, text):
        self.chat_status.Text = text


def _get_state():
    state = sc.sticky.get(STICKY_KEY)
    if state:
        return state
    state = {"log_lines": [], "chat_lines": [], "window": None, "model": DEFAULT_MODEL}

    def log(msg):
        line = time.strftime("%H:%M:%S ") + msg
        state["log_lines"].append(line)
        del state["log_lines"][:-500]
        w = state.get("window")
        if w:
            _ui(lambda: w.append_log(line))

    def on_text(who, text):
        w = state.get("window")
        if w:
            _ui(lambda: w.append_chat(who, text))
        else:
            state["chat_lines"].append("%s: %s\n\n" % (who, text))

    def on_status(text):
        w = state.get("window")
        if w:
            _ui(lambda: w.set_chat_status(text))

    state["server"] = BridgeServer(DEFAULT_PORT, log)
    state["agent"] = ChatAgent(on_text, on_status)
    sc.sticky[STICKY_KEY] = state
    return state


def main():
    state = _get_state()
    if not state["server"].running:
        try:
            state["server"].start()
        except OSError as ex:
            Rhino.RhinoApp.WriteLine("Rhino MCP: no se pudo abrir el puerto %d: %s" % (state["server"].port, ex))
    w = state.get("window")
    if w is None:
        w = BridgeWindow(state)
        state["window"] = w
        w.form.Show()
    else:
        w.form.BringToFront()
    Rhino.RhinoApp.WriteLine("Rhino MCP listo en %s:%d" % (HOST, state["server"].port))


if __name__ == "__main__":
    main()
