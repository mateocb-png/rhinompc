# Rhino MCP

Conecta Claude con **Rhino 8** para modelar de forma iterativa, de dos maneras:

1. **Desde Claude Code / Claude Desktop** a través de un servidor MCP.
2. **Desde una ventana dentro de Rhino** ("Rhino MCP"), con un chat que usa las mismas herramientas.

```
┌──────────────────┐  stdio   ┌──────────────────────┐   TCP 127.0.0.1:54321   ┌──────────────────────────┐
│ Claude Code /    │ ───────▶ │ server/              │ ──────────────────────▶ │ Rhino 8                  │
│ Claude Desktop   │          │ rhino_mcp_server.py  │   JSON, una por línea   │ rhino/rhino_mcp_bridge.py│
└──────────────────┘          └──────────────────────┘                         │  ├─ servidor TCP         │
                                                                               │  ├─ herramientas (UI)    │
                          Claude API (anthropic SDK) ◀──────────────────────── │  └─ ventana + chat       │
                                                                               └──────────────────────────┘
```

Todo lo que toca el documento se ejecuta en el hilo de UI de Rhino, y cada llamada queda
como un paso de **deshacer** propio (`Ctrl+Z` funciona igual que siempre).

## 1. Dentro de Rhino (obligatorio)

Requisitos: Rhino 8 (Windows o Mac), que trae Python 3.

1. En Rhino, ejecuta el comando `ScriptEditor`.
2. Abre `rhino/rhino_mcp_bridge.py` y pulsa **Run** (F5).
   - La primera vez, Rhino instala el paquete `anthropic` automáticamente (línea `# r: anthropic`).
     Solo se usa para el chat interno.
3. Se abre la ventana **Rhino MCP** y el puente empieza a escuchar en `127.0.0.1:54321`.

Para tenerlo a mano, crea un botón o alias con:

```
_-RunPythonScript "C:\ruta\a\rhino-mcp\rhino\rhino_mcp_bridge.py"
```

Volver a ejecutarlo solo trae la ventana al frente: el servidor no se duplica.
Si cambias el código del script, reinicia Rhino para que se cargue la nueva versión.

### La ventana "Rhino MCP"

| Zona | Qué hace |
|------|----------|
| Puerto / Iniciar / Detener | Controla el puente TCP que usa el servidor MCP externo |
| Log | Cada herramienta ejecutada, con su duración o su error |
| Modelo | Modelo de Claude para el chat (`claude-opus-5` por defecto) |
| API key | Tu `ANTHROPIC_API_KEY`. Si lo dejas vacío, usa la variable de entorno. Se guarda solo en memoria |
| Chat | Escribe y envía con **Ctrl+Enter**. Claude ejecuta herramientas, captura la vista para comprobar el resultado e itera |
| Parar / Nueva conversación | Corta el bucle actual / borra el historial |

El chat tiene activado el *fallback* del lado del servidor (`fallbacks: "default"`): si el
modelo declina una petición, la API la reintenta con un modelo alternativo. Puedes quitarlo
borrando `betas` y `extra_body` en `ChatAgent._run`.

## 2. Servidor MCP (para Claude Code / Claude Desktop)

Requisitos: [uv](https://docs.astral.sh/uv/) (instala Python y `mcp` solo).

**Claude Code**

```bash
claude mcp add rhino -- uv run /ruta/a/rhino-mcp/server/rhino_mcp_server.py
```

**Claude Desktop**: añade esto a `claude_desktop_config.json`
(Configuración → Desarrollador → Editar configuración):

```json
{
  "mcpServers": {
    "rhino": {
      "command": "uv",
      "args": ["run", "/ruta/a/rhino-mcp/server/rhino_mcp_server.py"]
    }
  }
}
```

Sin uv: `pip install "mcp>=1.2,<2"` y usa `python rhino_mcp_server.py` como comando.

Variables opcionales: `RHINO_MCP_HOST`, `RHINO_MCP_PORT` (también en el lado de Rhino), `RHINO_MCP_TIMEOUT`.

Comprueba la conexión pidiéndole a Claude: *"haz ping a Rhino"*.

## Herramientas

| Grupo | Herramientas |
|-------|--------------|
| Consulta | `get_document_info`, `list_objects`, `get_object_info`, `capture_viewport`, `ping` |
| Creación | `create_point`, `create_line`, `create_polyline`, `create_curve`, `create_circle`, `create_box`, `create_sphere`, `create_cylinder`, `create_text` |
| Edición | `delete_objects`, `transform_objects`, `boolean_operation`, `set_object_attributes`, `create_layer`, `select_objects`, `undo` |
| Para todo lo demás | `run_command` (cualquier comando de Rhino, p.ej. `_-Loft`), `execute_python` (RhinoCommon / rhinoscriptsyntax, con variables persistentes entre llamadas) |

Las herramientas de creación aceptan `name`, `layer` (`"Padre::Hijo"`, se crea si no existe)
y `color` (`"#rrggbb"`).

### Ejemplos de peticiones

- "Crea una torre de 20 plantas de 3 m, girando cada planta 4°, en la capa Torre::Losas."
- "Resta un cilindro de radio 2 al centro de la caja seleccionada y enséñame el resultado."
- "Revisa todas las curvas abiertas de la capa Planta y ciérralas."

## Añadir una herramienta

1. En `rhino/rhino_mcp_bridge.py`: escribe `def t_mi_tool(p): ...`, regístrala en `TOOLS`
   y añade su esquema en `CHAT_TOOL_SCHEMAS` (para el chat interno).
2. En `server/rhino_mcp_server.py`: añade una función `@mcp.tool()` que llame a
   `call_rhino("mi_tool", ...)`.

## Seguridad

- El puente escucha solo en `127.0.0.1`: no es accesible desde otras máquinas.
- `execute_python` y `run_command` ejecutan código arbitrario dentro de Rhino. Cualquier programa
  local que se conecte al puerto puede hacerlo, así que detén el puente cuando no lo uses.

## Limitaciones

- Los comandos interactivos (que esperan clics) bloquean hasta que terminas en Rhino; usa la
  versión con guion (`_-Comando`) y pasa las opciones por texto.
- Si Rhino está ocupado más de 120 s, la llamada devuelve un error de tiempo agotado.

## Pruebas

**Sin Rhino** (simula RhinoCommon y prueba puente ↔ servidor MCP ↔ chat):

```bash
uv run --with "mcp>=1.2,<2" python tests/test_offline.py
```

**Con Rhino real** (usa un documento de pruebas; crea y borra objetos en la capa `MCP_Test`):

1. En Rhino: ejecuta `rhino/rhino_mcp_bridge.py`.
2. En una terminal: `python tests/smoke_test_rhino.py`
   (añade `VERBOSE=1` para ver el traceback de los fallos).

Recorre todas las herramientas, guarda una captura en `tests/smoke_capture.png` y muestra
un resumen `PASS` / `FAIL`.
