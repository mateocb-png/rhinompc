# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""
Servidor MCP para Rhino.

Se ejecuta en tu máquina (no dentro de Rhino) y habla por stdio con Claude Code /
Claude Desktop. Cada herramienta se reenvía al puente que corre dentro de Rhino
(rhino/rhino_mcp_bridge.py) por TCP en 127.0.0.1:54321.

    uv run rhino_mcp_server.py
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP, Image

HOST = os.environ.get("RHINO_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("RHINO_MCP_PORT", "54321"))
TIMEOUT_S = float(os.environ.get("RHINO_MCP_TIMEOUT", "150"))

mcp = FastMCP(
    "rhino",
    instructions=(
        "Controla Rhino 8 en vivo. Trabaja de forma iterativa: get_document_info / list_objects "
        "para entender la escena, crea o modifica geometría, y usa capture_viewport para verificar "
        "visualmente. Para lo que no cubran las herramientas específicas usa execute_python "
        "(RhinoCommon) o run_command (comandos de Rhino con guion, p.ej. '_-Loft')."
    ),
)

_ids = itertools.count(1)

Vec3 = list[float]


def call_rhino(tool: str, **params: Any) -> Any:
    """Envía una petición al puente de Rhino y devuelve el resultado."""
    params = {k: v for k, v in params.items() if v is not None}
    req = {"id": next(_ids), "tool": tool, "params": params}
    try:
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT_S) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            f = s.makefile("rb")
            line = f.readline()
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise RuntimeError(
            f"No se pudo conectar con Rhino en {HOST}:{PORT} ({e}). "
            "Abre Rhino 8 y ejecuta rhino/rhino_mcp_bridge.py desde el ScriptEditor."
        ) from e
    if not line:
        raise RuntimeError("Rhino cerró la conexión sin responder")
    resp = json.loads(line.decode("utf-8"))
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "Error desconocido en Rhino"))
    return resp["result"]


# ── Consulta ────────────────────────────────────────────────────────────────

@mcp.tool()
def get_document_info() -> dict:
    """Información del documento abierto: unidades, tolerancia, capas, vistas y selección actual."""
    return call_rhino("get_document_info")


@mcp.tool()
def list_objects(layer: Optional[str] = None, type: Optional[str] = None,
                 selected_only: bool = False, limit: int = 200) -> dict:
    """Lista objetos (id, tipo, nombre, capa, bounding box). Filtra por capa exacta, tipo (p.ej. 'Brep', 'Curve') o selección."""
    return call_rhino("list_objects", layer=layer, type=type, selected_only=selected_only, limit=limit)


@mcp.tool()
def get_object_info(ids: list[str]) -> dict:
    """Detalle de objetos por GUID: longitud de curvas, área, volumen, si es sólido y user text."""
    return call_rhino("get_object_info", ids=ids)


@mcp.tool()
def capture_viewport(view: Optional[str] = None, width: int = 1024, height: int = 768,
                     zoom_extents: bool = False, display_mode: Optional[str] = None) -> list:
    """Captura una vista de Rhino como imagen PNG para ver el resultado. view: 'Perspective', 'Top', etc. display_mode: 'Shaded', 'Rendered', 'Wireframe'..."""
    r = call_rhino("capture_viewport", view=view, width=width, height=height,
                   zoom_extents=zoom_extents, display_mode=display_mode)
    img = Image(data=base64.b64decode(r.pop("image_png_base64")), format="png")
    return [img, json.dumps(r)]


# ── Creación ────────────────────────────────────────────────────────────────
# Parámetros comunes: name, layer ('Padre::Hijo', se crea si no existe), color ('#rrggbb').

@mcp.tool()
def create_point(point: Vec3, name: Optional[str] = None, layer: Optional[str] = None,
                 color: Optional[str] = None) -> dict:
    """Crea un punto en [x, y, z]."""
    return call_rhino("create_point", point=point, name=name, layer=layer, color=color)


@mcp.tool()
def create_line(start: Vec3, end: Vec3, name: Optional[str] = None, layer: Optional[str] = None,
                color: Optional[str] = None) -> dict:
    """Crea una línea entre dos puntos."""
    return call_rhino("create_line", start=start, end=end, name=name, layer=layer, color=color)


@mcp.tool()
def create_polyline(points: list[Vec3], closed: bool = False, name: Optional[str] = None,
                    layer: Optional[str] = None, color: Optional[str] = None) -> dict:
    """Crea una polilínea por puntos; closed=True la cierra."""
    return call_rhino("create_polyline", points=points, closed=closed, name=name, layer=layer, color=color)


@mcp.tool()
def create_curve(points: list[Vec3], degree: int = 3, name: Optional[str] = None,
                 layer: Optional[str] = None, color: Optional[str] = None) -> dict:
    """Crea una curva NURBS interpolada que pasa por los puntos."""
    return call_rhino("create_curve", points=points, degree=degree, name=name, layer=layer, color=color)


@mcp.tool()
def create_circle(center: Vec3, radius: float, normal: Vec3 = [0, 0, 1], name: Optional[str] = None,
                  layer: Optional[str] = None, color: Optional[str] = None) -> dict:
    """Crea un círculo con centro, radio y normal del plano."""
    return call_rhino("create_circle", center=center, radius=radius, normal=normal,
                      name=name, layer=layer, color=color)


@mcp.tool()
def create_box(corner: Vec3, size: Vec3, name: Optional[str] = None, layer: Optional[str] = None,
               color: Optional[str] = None) -> dict:
    """Crea una caja alineada a ejes desde la esquina mínima con tamaño [dx, dy, dz]."""
    return call_rhino("create_box", corner=corner, size=size, name=name, layer=layer, color=color)


@mcp.tool()
def create_sphere(center: Vec3, radius: float, name: Optional[str] = None, layer: Optional[str] = None,
                  color: Optional[str] = None) -> dict:
    """Crea una esfera."""
    return call_rhino("create_sphere", center=center, radius=radius, name=name, layer=layer, color=color)


@mcp.tool()
def create_cylinder(base: Vec3, radius: float, height: float, axis: Vec3 = [0, 0, 1],
                    name: Optional[str] = None, layer: Optional[str] = None,
                    color: Optional[str] = None) -> dict:
    """Crea un cilindro cerrado desde el centro de la base, a lo largo de axis."""
    return call_rhino("create_cylinder", base=base, radius=radius, height=height, axis=axis,
                      name=name, layer=layer, color=color)


@mcp.tool()
def create_text(text: str, point: Vec3, height: float = 1.0, font: str = "Arial",
                name: Optional[str] = None, layer: Optional[str] = None,
                color: Optional[str] = None) -> dict:
    """Crea un texto en el plano XY."""
    return call_rhino("create_text", text=text, point=point, height=height, font=font,
                      name=name, layer=layer, color=color)


# ── Edición ─────────────────────────────────────────────────────────────────

@mcp.tool()
def delete_objects(ids: list[str]) -> dict:
    """Borra objetos por GUID."""
    return call_rhino("delete_objects", ids=ids)


@mcp.tool()
def transform_objects(ids: list[str], translate: Optional[Vec3] = None,
                      rotate_angle: Optional[float] = None, rotate_axis: Vec3 = [0, 0, 1],
                      rotate_center: Vec3 = [0, 0, 0], scale_factor: Optional[float] = None,
                      scale_center: Vec3 = [0, 0, 0], copy: bool = False) -> dict:
    """Mueve, rota (grados) y/o escala objetos. Orden aplicado: escala → rotación → traslación. copy=True deja el original."""
    rotate = ({"angle": rotate_angle, "axis": rotate_axis, "center": rotate_center}
              if rotate_angle is not None else None)
    scale = {"factor": scale_factor, "center": scale_center} if scale_factor is not None else None
    return call_rhino("transform_objects", ids=ids, translate=translate, rotate=rotate, scale=scale, copy=copy)


@mcp.tool()
def boolean_operation(operation: Literal["union", "difference", "intersection"], a_ids: list[str],
                      b_ids: Optional[list[str]] = None, delete_inputs: bool = True) -> dict:
    """Operación booleana entre sólidos. difference = A menos B."""
    return call_rhino("boolean_operation", operation=operation, a_ids=a_ids, b_ids=b_ids or [],
                      delete_inputs=delete_inputs)


@mcp.tool()
def set_object_attributes(ids: list[str], name: Optional[str] = None, layer: Optional[str] = None,
                          color: Optional[str] = None,
                          user_text: Optional[dict[str, str]] = None) -> dict:
    """Cambia nombre, capa, color o user text (clave/valor) de objetos."""
    return call_rhino("set_object_attributes", ids=ids, name=name, layer=layer, color=color,
                      user_text=user_text)


@mcp.tool()
def create_layer(name: str, color: Optional[str] = None, make_current: bool = False) -> dict:
    """Crea una capa (ruta 'Padre::Hijo'), opcionalmente con color y como capa actual."""
    return call_rhino("create_layer", name=name, color=color, make_current=make_current)


@mcp.tool()
def select_objects(ids: list[str], clear: bool = True) -> dict:
    """Selecciona objetos en Rhino (clear=True deselecciona todo antes)."""
    return call_rhino("select_objects", ids=ids, clear=clear)


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Deshace N pasos en Rhino. Cada llamada de herramienta es un paso."""
    return call_rhino("undo", steps=steps)


# ── Escape hatches ──────────────────────────────────────────────────────────

@mcp.tool()
def run_command(command: str, echo: bool = False) -> dict:
    """Ejecuta un comando de Rhino como script (usa la versión con guion para evitar diálogos, p.ej. '_-Loft _Pause', '_SelAll _Join'). Devuelve los objetos nuevos creados."""
    return call_rhino("run_command", command=command, echo=echo)


@mcp.tool()
def execute_python(code: str) -> dict:
    """Ejecuta Python 3 dentro de Rhino. Disponibles: Rhino, rg (Rhino.Geometry), rs (rhinoscriptsyntax), sc, doc, System. Asigna a `result` lo que quieras devolver (JSON); print() también se devuelve. Las variables persisten entre llamadas."""
    return call_rhino("execute_python", code=code)


@mcp.tool()
def ping() -> dict:
    """Comprueba que Rhino y el puente están activos."""
    return call_rhino("ping")


if __name__ == "__main__":
    mcp.run()
