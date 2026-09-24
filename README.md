<div align="center">

# 🗞️ bome-navaja

**Servidor MCP para el [Boletín Oficial de la Ciudad Autónoma de Melilla (BOME)](https://bomemelilla.es)**

[![Python](https://img.shields.io/badge/python-3.12+-blue)](#-desarrollo)
[![MCP](https://img.shields.io/badge/MCP-19%20herramientas-8A2BE2)](#-herramientas)
[![Licencia](https://img.shields.io/badge/licencia-MIT-green)](#-licencia)

*Pregunta por el BOME en lenguaje natural: lista boletines, lee artículos y PDF completos, busca en vivo con la misma lógica que el sitio, consulta al instante un índice local de sumarios y llega hasta 1985 con el portal antiguo de melilla.es.*

</div>

---

## 📑 Índice

| | |
| --- | --- |
| [🧭 Qué es](#-qué-es) | [📂 Dónde guarda los datos](#-dónde-guarda-los-datos) |
| [🧰 Herramientas](#-herramientas) | [🚀 Instalación](#-instalación) |
| [🔍 Cómo busca el sitio](#-cómo-busca-el-sitio-y-por-qué-importa) | [🔒 Seguridad y cortesía](#-seguridad-y-cortesía-con-el-sitio) |
| [📇 Índice local de sumarios](#-índice-local-de-sumarios) | [🧪 Desarrollo](#-desarrollo) |
| [🏛️ Portal antiguo (melilla.es)](#-portal-antiguo-melillaes) | [🚧 Limitaciones conocidas](#-limitaciones-conocidas) |
| [📖 Leer documentos largos](#-leer-documentos-largos) | [📜 Licencia](#-licencia) |
| [💾 PDF descargados](#-pdf-descargados) | |

---

## 🧭 Qué es

`bome-navaja` es un programa local que tu cliente de IA (Claude Desktop, Claude Code…) lanza en tu máquina y con el que habla por el protocolo MCP. Le da al modelo acceso a [bomemelilla.es](https://bomemelilla.es): el calendario de boletines, el árbol de artículos de cada boletín, el texto completo de artículos y boletines, los PDF, el buscador del sitio y un índice local de sumarios para búsquedas instantáneas.

**Cobertura** (la de bomemelilla.es):

| Periodo | Qué hay |
| --- | --- |
| Desde el 3 de enero de 2014 | Los boletines ordinarios (`BOME-B`) y extraordinarios (`BOME-BX`), en el calendario y con su árbol de artículos (antes de 2018 faltan boletines) |
| Desde finales de 2016 | Sumario de cada artículo, texto HTML completo y PDF |
| 2014–2016 | Los artículos **no tienen sumario ni texto**, y los PDF del boletín y de los artículos **no se pueden descargar** (el sitio responde 404 aunque muestre el botón). En bomemelilla.es solo se puede buscar dentro del contenido con `buscar_bomes` y `ambito="contenido"` |

Para lo anterior a 2018, y para todo lo anterior a 2014, está el **[portal antiguo de melilla.es](#-portal-antiguo-melillaes)**: boletines del 3 de enero de 1985 al 12 de marzo de 2021, con sumarios de artículos desde ~1991 y el PDF de cada página. Sus sumarios de 1991–2017 también se pueden [guardar en el índice local](#indexar-el-portal-antiguo-melillaes).

`bome-navaja` **solo lee datos públicos**: no inicia sesión, no usa credenciales y no envía nada al sitio más allá de las consultas.

---

## 🧰 Herramientas

Todas devuelven un objeto con `ok`. Si algo falla devuelven `ok: false`, un `error` en castellano y un `error_code` estable (`no_encontrado`, `cve_invalido`, `busqueda_invalida`, `lectura_invalida`, `argumento_invalido`, `error_http`, `sitio_bloqueando`, `pausa_preventiva`, `indice_no_disponible`, y para el portal antiguo `url_pdf_invalida` y `boletin_ambiguo`…). Los mensajes de error nombran el sitio que falló (bomemelilla.es o melilla.es); nunca rompen la conversación con una excepción.

| Grupo | Herramienta | Para qué sirve |
| --- | --- | --- |
| **Navegar** | `listar_bomes` | Boletines publicados entre dos fechas (por defecto, los últimos 30 días; como mucho 500, del más reciente al más antiguo), cada uno con su `origen`; antes del 13 de marzo de 2021 añade los que solo tiene el portal antiguo |
| | `ver_bome` | Un boletín con su árbol departamento → consejería → organismo → artículos; con `recuperar_ocultos` busca los artículos que la página omite |
| | `ver_sumario` | La vista web del sumario, con la primera página de cada artículo (algunos sumarios son texto libre y dan 0 entradas: usa `ver_bome`) |
| | `resolver_cve` | URL canónica de cualquier CVE (boletín, artículo, sumario o página) |
| | `listar_consejerias` | Consejerías de un departamento con su id, para filtrar búsquedas |
| | `listar_organismos` | Organismos de una consejería con su id, para filtrar búsquedas |
| **Leer y descargar** | `leer_articulo` | Texto completo de un artículo, paginado |
| | `leer_boletin` | Texto completo de un boletín entero (su PDF) con sus metadatos, paginado |
| | `leer_pdf` | Texto del PDF de cualquier CVE, o de un PDF del portal antiguo con `url`, paginado |
| | `descargar_pdf` | Guarda el PDF de un CVE (o de una `url` del portal antiguo) en la caché local y devuelve su ruta |
| **Buscar en vivo** | `buscar_bomes` | El buscador avanzado del sitio: devuelve **boletines**, 10 por página |
| | `buscar_articulos` | Devuelve **artículos**: busca boletines, abre cada uno y se queda con los artículos cuyo sumario coincide |
| **Índice local** | `buscar_en_indice` | Búsqueda instantánea de artículos en el índice local de sumarios (bomemelilla.es y, una vez indexado, el portal antiguo), cada uno con su `origen` |
| | `estado_indice` | Qué hay indexado de cada origen y cómo va la sincronización |
| | `sincronizar_indice` | Arranca en segundo plano la sincronización del índice: de bomemelilla.es o, con `origen="melilla.es"`, del portal antiguo |
| | `cancelar_sincronizacion` | Pide parar la sincronización en curso, sea del origen que sea |
| **Portal antiguo** | `buscar_bome_antiguo` | Búsqueda literal de artículos en melilla.es (1985–2021), con sumario y PDF de cada página |
| | `ver_bome_antiguo` | Un boletín del portal antiguo (por CVE o `dboid`): PDF entero y artículos con sus páginas |
| **Servidor** | `estado_servidor` | Versión, rutas de datos, SQLite disponible, guardias de los dos sitios y configuración, sin tocar la red |

Los CVE tienen la forma `BOME-L-AAAA-N`: `BOME-B-2026-6416` (boletín), `BOME-BX-2026-41` (extraordinario), `BOME-A-2026-1051` (artículo), `BOME-S-2026-6416` (sumario), `BOME-P-2026-4784` (página), `BOME-PX-2021-362` (página de un extraordinario). Se aceptan en minúsculas y con espacios.

---

## 🔍 Cómo busca el sitio (y por qué importa)

Todo lo que sigue está **medido contra el sitio real**, no deducido de su interfaz. Condiciona cómo hay que redactar las búsquedas.

| Regla | Consecuencia |
| --- | --- |
| Coincidencia **literal por subcadena**, sin distinguir tildes ni mayúsculas (la ñ cuenta como n) | `"nombra"` encuentra «nombramiento»; `"cese"` encuentra «cese», «ceses» y también «procese» |
| **Sin sinónimos** ni variantes | Busca `"cese"`, no `"destitución"`: esta última da 0 resultados porque el BOME no la usa. El orden de las palabras importa |
| El sitio **ignora el texto si no recibe fecha de inicio** (`from`) | El servidor la envía siempre (por defecto, desde el 2014-01-01 hasta hoy) |
| Los términos unidos con **Y** y los de **«no contiene»** se evalúan **dentro de un mismo artículo** | `"personal eventual"` Y `"hacienda"` no encuentra boletines donde esas palabras están en artículos distintos |
| El sitio **ignora el O** entre términos (lo trata como Y) | `buscar_bomes` rechaza el O; úsalo con `buscar_articulos` o `buscar_en_indice`, que lo aplican ellas mismas |
| Los criterios numéricos son **exactos** | Boletín `6416`, artículo `1051`, página `4784` o año `2025`; `641` no encuentra el 6416 |
| El buscador devuelve **boletines, no artículos** | `buscar_articulos` abre cada boletín para quedarse con los artículos que coinciden |
| La página de un boletín **a veces omite artículos** | `buscar_articulos` (y el índice) detectan los huecos de numeración y leen esos artículos de su propia página |

### Ejemplo: «nombramientos y ceses de personal eventual»

«Personal eventual» y «cese» deben estar en el mismo artículo; para incluir también los nombramientos hace falta un O, así que la herramienta es `buscar_articulos` (o `buscar_en_indice` si el índice ya está sincronizado). Los términos forman grupos Y separados por cada `"operador": "o"`:

```json
{
  "texto": "personal eventual",
  "terminos": [
    {"texto": "cese"},
    {"texto": "personal eventual", "operador": "o"},
    {"texto": "nombramiento"}
  ],
  "max_bomes": 50
}
```

Se lee así: (*personal eventual* Y *cese*) O (*personal eventual* Y *nombramiento*). El servidor lanza una búsqueda en el sitio por cada grupo, junta los boletines sin repetir, del más reciente al más antiguo, y devuelve cada artículo con su CVE, sumario, departamento, consejería, organismo, `url` y `pdf_url`.

Solo para los ceses, sin el O, basta con:

```json
{"texto": "personal eventual", "terminos": [{"texto": "cese"}]}
```

Con esta última consulta, el sitio devolvió 12 boletines el 23 de septiembre de 2026.

> [!TIP]
> `buscar_articulos` es lenta a propósito: una petición por boletín, con una pausa de cortesía. Está acotada por `max_bomes` (por defecto 20, máximo 100) y `max_articulos` (por defecto 100, máximo 500). Mira `truncado` y `total_bomes` antes de concluir que no hay más. Con O, `total_bomes` suma los resultados de cada grupo y puede contar un boletín dos veces: `total_bomes_exacto` es `false` en ese caso. Los boletines que el sitio encontró pero donde ningún sumario coincide (por ejemplo, los de 2014–2016, que no tienen sumarios) salen en `bomes_sin_coincidencia`.

Para buscar **dentro del texto de las páginas** (la única vía para 2014–2016), usa `buscar_bomes` con `ambito="contenido"`; devuelve boletines, no artículos.

---

## 📇 Índice local de sumarios

Un fichero SQLite en tu máquina con los sumarios de todos los artículos, indexado con FTS5 y el tokenizador **trigram**. Responde al instante y admite Y, O y «no contiene» dentro del mismo artículo, igual que `buscar_articulos`, pero sin tocar el sitio.

Guarda dos orígenes: **bomemelilla.es** (desde 2018 por defecto) y, si lo [indexas](#indexar-el-portal-antiguo-melillaes), el **portal antiguo de melilla.es** (1991–2017 por defecto). Cada artículo de `buscar_en_indice` trae su `origen` (`"bomemelilla.es"` o `"melilla.es"`); en los del portal antiguo, `url` es la ficha del boletín en melilla.es y `pdf_url` el PDF de la página del artículo (se lee con `leer_pdf` y `url`).

### `coincidencia`: fragmento o palabra

| `coincidencia` | Regla | `"cese"` encuentra | `"ano"` encuentra |
| --- | --- | --- | --- |
| `"fragmento"` (por defecto) | Subcadena, igual que el sitio | cese, ceses, procese | año, humanos |
| `"palabra"` | Cada frase debe empezar una palabra (puede acabar a mitad de palabra) | cese, ceses | año |

En los dos modos se ignoran tildes y mayúsculas, y un `*` final se acepta pero no cambia nada. Los términos de menos de 3 caracteres (p. ej. `"de"`) no caben en un índice de trigramas: se resuelven recorriendo los sumarios, y funcionan igual. Otros parámetros: `desde`/`hasta`, `extraordinario`, `consejeria` (parte del nombre, sin tildes), `orden` (`"fecha"` o `"relevancia"`, esta última aproximada), `limite` (máximo 200) y `desplazamiento`; `siguiente` da el desplazamiento de la página siguiente.

### Sincronizar

- **Solo se sincroniza cuando el modelo llama a `sincronizar_indice`.** El servidor nunca recorre el sitio por su cuenta, ni al arrancar.
- La herramienta vuelve al instante; la sincronización sigue **en segundo plano**, del boletín más reciente al más antiguo.
- Por defecto cubre **desde el 1 de enero de 2018 hasta hoy**. Antes de 2018 bomemelilla.es está incompleto e inestable (faltan boletines, sus páginas rotas responden HTTP 500 y de ahí vienen los bloqueos del cortafuegos) y casi no hay sumarios que buscar; esos boletines se consultan mejor en el portal antiguo de melilla.es. Puedes pedir un `desde` anterior, pero no es recomendable.
- Va **despacio a propósito** (~2–3 s entre peticiones) y cada ejecución indexa como mucho **250 boletines** (los más recientes; parámetro `max_boletines`), unos 15–20 minutos (más si el sitio responde con errores; ver abajo). El rango por defecto (~1.100 boletines desde 2018) necesita **varias ejecuciones**: si el estado final trae `pendientes_tras_limite` mayor que 0, vuelve a sincronizar más tarde. Espaciar las ejecuciones es más amable con el sitio. El índice completo ocupa del orden de **40–50 MB**.
- Es **reanudable**: cada boletín se guarda en cuanto se procesa. Si se corta, la siguiente llamada continúa donde quedó. Por defecto también reindexa los boletines de los últimos 7 días (`reindexar_recientes_dias`) y reintenta los que fallaron (`reintentar_errores`), salvo los `roto`.
- Sigue el progreso con `estado_indice` (`hechos`, `total_planificado`, `eta_segundos`, `rotos`). Mientras tanto `buscar_en_indice` funciona, pero avisa de que los resultados son parciales.
- `cancelar_sincronizacion` para tras el boletín en curso (o al instante si está en una pausa); lo ya indexado se conserva.
- **Cuida el cortafuegos del sitio**, que bloquea la IP tras unas 5 respuestas de error (detalle en [Seguridad y cortesía](#-seguridad-y-cortesía-con-el-sitio)):
  - No pasa de **3 respuestas de error cada 10 minutos**: si llega al límite, hace una pausa preventiva (la indica `mensaje`), así que puede ir más lenta.
  - Tras una página de boletín rota (HTTP 500) hace una pausa de **30–60 s**.
  - Un boletín cuya página respondió 500 dos veces queda como **`roto`**: las sincronizaciones normales lo saltan y `estado_indice` lo cuenta aparte, no como pendiente. `reintentar_rotos: true` los vuelve a pedir, pero cada uno cuesta un 500 que el cortafuegos cuenta: úsalo solo para comprobar si el sitio los arregló.
  - Si el sitio bloquea igualmente (403, 429 o 503, o dos peticiones seguidas sin respuesta), termina en estado **`bloqueado`** y `bome-navaja` no vuelve a pedirle nada durante **75 minutos** (o más, si el sitio lo pide con `Retry-After`); `reintentar_tras_segundos` dice cuánto falta. Relánzala pasado ese tiempo: continúa donde quedó.
- Si tienes **dos clientes** abiertos con `bome-navaja` (por ejemplo, Claude Desktop y Claude Code), solo uno sincroniza: el otro recibe `en_curso_en_otro_proceso` con el `origen` que ocupa el turno. Hay **un solo turno por índice**, sea cual sea el origen: mientras sincroniza bomemelilla.es no se puede sincronizar el portal antiguo, y al revés. El turno se considera abandonado si su dueño deja de dar señales durante 3 minutos.

Cada respuesta de `buscar_en_indice` trae un bloque `cobertura` (rango de fechas indexado, boletines indexados y pendientes, última sincronización, si hay una en curso, y lo indexado de cada origen en `por_origen`). Los pendientes cuentan solo desde 2018; los boletines anteriores que un índice de una versión previa tenga en su calendario sin indexar salen aparte en `pendientes_anteriores_2018` (también en `estado_indice`). Si el índice está vacío, devuelve 0 resultados y un `aviso` que sugiere sincronizar o usar `buscar_articulos` mientras tanto.

### Indexar el portal antiguo (melilla.es)

`sincronizar_indice` con `origen="melilla.es"` guarda en el mismo índice los sumarios de artículos del [portal antiguo](#-portal-antiguo-melillaes), para buscar con Y, O y «no contiene» en 1991–2017 y en varios años a la vez, cosa que la búsqueda del portal (literal, de una sola frase y sin paginar) no permite.

```json
{"origen": "melilla.es"}
```

- **Qué indexa**: los sumarios de la ficha de cada boletín (`ficha_bome.jsp`), con **una petición por boletín** y nunca los PDF. Por defecto, los boletines del **1 de enero de 1991 al 31 de diciembre de 2017**: antes de 1991 las fichas no traen artículos (solo el PDF del boletín entero) y desde 2018 está bomemelilla.es. Admite otro `desde`/`hasta`.
- **El mismo ritmo y el mismo límite** que la de bomemelilla.es: 2 s más hasta 1 s aleatorio entre peticiones y como mucho **250 boletines por ejecución** (`max_boletines`), unos 15–20 minutos. El rango por defecto tiene unos 2.000–2.500 boletines, así que hacen falta **unas 8–10 ejecuciones**, mejor espaciadas; `pendientes_tras_limite` dice cuántos quedan. `BOME_NAVAJA_SYNC_DELAY` y `BOME_NAVAJA_SYNC_MAX_BOLETINES` se aplican igual.
- Es **reanudable** y no repite trabajo: se salta los boletines ya indexados con sumarios (de cualquier origen) y los que el portal ya dio sin artículos. Reintenta los fallidos (`reintentar_errores`) y los `roto` solo con `reintentar_rotos`. `reindexar_recientes_dias` no se aplica: el portal está congelado.
- Usa la **guardia del portal antiguo** (`estado_sitio_melilla.json`), no la de bomemelilla.es: termina `bloqueado` si melilla.es la bloquea, y sus errores no cuentan para el otro sitio.
- **Un solo turno por índice**: mientras corre la sincronización de un origen, la del otro recibe `en_curso_en_otro_proceso`. `cancelar_sincronizacion` para la que esté en curso, sea del origen que sea.
- **Si un boletín está en los dos orígenes, gana el mejor resultado**: con sumarios gana a sin sumarios, y este a un fallo; a igualdad, gana bomemelilla.es. Así los boletines de 2014–2016, que bomemelilla.es tiene sin sumarios, se rellenan con los del portal antiguo, y cada boletín sale una sola vez en `buscar_en_indice`.
- **Resultados**: cada artículo trae `origen: "melilla.es"`, `url` (la ficha del boletín en melilla.es) y `pdf_url` (el PDF de la página del artículo, para `leer_pdf` con `url`). Su `bome_cve` es el identificador del portal (antes de 2014 no es un CVE de bomemelilla.es; si se repite, lleva el `dboid` detrás, como `BOME-BX-1986-1~280058`) y su `cve`, una clave interna `MEL-<dboid>-<número>`. `estado_indice` cuenta cada origen en `por_origen`, guarda la última sincronización de cada uno en `ultimas_sincronizaciones` y muestra el `origen` de la que está en curso.

### Dónde está y cómo rehacerlo

El índice es el fichero `sumarios.sqlite3` dentro de la [carpeta de datos](#-dónde-guarda-los-datos); `estado_servidor` y `estado_indice` muestran su ruta. Para rehacerlo desde cero, cierra el cliente, borra `sumarios.sqlite3` (y `sumarios.sqlite3-wal` / `sumarios.sqlite3-shm` si existen) y vuelve a llamar a `sincronizar_indice`. Un índice de una versión anterior de `bome-navaja` se migra solo al abrirlo, sin volver a descargar nada; los boletines que tenían anotado un HTTP 500 pasan a `roto`.

---

## 🏛️ Portal antiguo (melilla.es)

bomemelilla.es es una migración **incompleta** antes de 2018: de 2014 a 2017 le faltan **141 boletines** (y ahí se concentran sus páginas rotas), y no tiene **nada anterior a 2014**. El [portal antiguo del BOME](https://www.melilla.es/melillaPortal/contenedor.jsp?seccion=bome.jsp) en melilla.es, congelado desde marzo de 2021, conserva el catálogo entero: **3.260 boletines del 3 de enero de 1985 al 12 de marzo de 2021**, con sumarios de artículos desde ~1991 y el PDF de cada página.

| Qué quieres | Herramienta |
| --- | --- |
| Saber qué boletines hay en unas fechas | `listar_bomes`: antes del 13 de marzo de 2021 junta los dos catálogos (si un boletín está en los dos gana bomemelilla.es); los que solo tiene el portal antiguo traen `origen: "melilla.es"`, su `dboid` y `ver_con` |
| Buscar artículos | `buscar_bome_antiguo`: búsqueda **literal** en el texto de los artículos (3–200 caracteres; no busca por número de boletín). El portal devuelve todo en una sola página, así que conviene usar términos concretos. Cada artículo trae boletín, fecha, número, tipo, sumario, consejería/dirección/sección y el PDF de cada página. Filtra por fechas (`desde`/`hasta`) y devuelve como mucho `limite` artículos (por defecto 100, máximo 500) con `total` y `truncado` |
| Ver un boletín | `ver_bome_antiguo` con `cve` o `dboid`: el PDF del boletín entero y sus artículos con sus páginas |
| Buscar con Y, O o «no contiene», o en varios años a la vez | `buscar_en_indice`, después de [indexar el portal antiguo](#indexar-el-portal-antiguo-melillaes) con `sincronizar_indice` y `origen="melilla.es"` (1991–2017 por defecto) |
| Leer o guardar un PDF | `leer_pdf` / `descargar_pdf` con `url` (solo las URL `https://www.melilla.es/mandar.php/...` que dan las dos herramientas anteriores) |

Si `ver_bome` no encuentra un boletín anterior a 2022 en bomemelilla.es, su error sugiere `ver_bome_antiguo`.

**Identificadores.** Desde 2014 la numeración del portal antiguo coincide con los CVE de bomemelilla.es (`BOME-B-2016-5302`, `BOME-BX-2021-16`). Antes de 2014 los identificadores tienen la misma forma pero **no son CVE de bomemelilla.es** (`cve_oficial: false`) y algunos se repiten (24 casos, por ejemplo dos «Extra 1» en 1986): `ver_bome_antiguo` responde entonces `boletin_ambiguo` con los candidatos (`dboid`, fecha, sufijo), y basta con repetir con el `dboid`. Además, **14 boletines tienen una fecha distinta en cada sitio** (por ejemplo `BOME-B-2015-5230`: 17-12-2015 en bomemelilla.es y 01-05-2015 en melilla.es); cita la fecha junto al origen.

**robots.txt y política de uso.** El `robots.txt` de melilla.es no permite a los robots las fichas de boletín (`ficha_bome.jsp`) ni los PDF (`/mandar.php`). Las herramientas solo los piden **bajo demanda**: cuando el modelo llama a una para responderte, una petición cada vez. Desde la versión 0.0.4 hay una excepción deliberada, porque el portal está congelado y puede desaparecer: la [indexación del portal antiguo](#indexar-el-portal-antiguo-melillaes) recorre las fichas en masa, pero solo cuando se pide con `sincronizar_indice` y `origen="melilla.es"` (nunca por su cuenta), despacio (~2–3 s entre peticiones), con un máximo de 250 boletines por ejecución y bajo la guardia del portal. **Los PDF nunca se recorren en masa**: solo se piden bajo demanda.

**Su propia guardia y su caché.** El portal antiguo es otro sitio, así que tiene su propia [guardia](#-seguridad-y-cortesía-con-el-sitio) con las mismas reglas (como mucho 3 respuestas de error cada 10 minutos; 75 minutos sin pedirle nada si bloquea), guardada aparte en `estado_sitio_melilla.json`; sus errores nunca cuentan para bomemelilla.es, y `estado_servidor` la muestra en `guardia_portal_antiguo`. Va a su propio ritmo (~1–1,5 s entre peticiones, de una en una). El catálogo (~1 MB) se descarga con una sola petición la primera vez que hace falta y se guarda en `catalogo_portal_antiguo.json`; como el portal está congelado, se reutiliza siempre (`estado_servidor` lo muestra en `catalogo_portal_antiguo`; borrarlo fuerza una nueva descarga). Si el portal no responde, `listar_bomes` devuelve igualmente lo de bomemelilla.es con un `aviso`.

---

## 📖 Leer documentos largos

`leer_articulo`, `leer_boletin` y `leer_pdf` comparten el mismo cursor, así que un boletín de ~35 páginas nunca llega recortado en silencio:

| Campo | Significado |
| --- | --- |
| `max_caracteres` | Presupuesto por llamada: entre 1000 y 100000 (por defecto 20000); un valor fuera de rango se ajusta al límite más cercano |
| `paginas` | Páginas enteras hasta llenar el presupuesto, **siempre al menos una** |
| `siguiente` | `{desde_pagina, desde_caracter}` para la siguiente llamada, o `null` si no queda nada |
| `cortada` | `true` si una página sola no cabía y se entregó un trozo; `siguiente` apunta dentro de esa página |
| `sin_texto` | `true` en páginas sin texto extraíble (escaneadas); viene con un `aviso` |
| `completo` | `true` solo si todo el documento cupo en una respuesta |

Para seguir leyendo, pasa `desde_pagina` y `desde_caracter` tal cual vienen en `siguiente`.

`leer_articulo` acepta el CVE del artículo (`BOME-A-2026-1051`) o el del boletín más `numero`. Para los artículos de 2014–2016, que no tienen texto, responde con `fuente: "ninguna"` y un aviso.

---

## 💾 PDF descargados

- `descargar_pdf` (y los lectores cuando hace falta) guardan cada PDF en la carpeta de PDF, por defecto `pdfs/` dentro de la [carpeta de datos](#-dónde-guarda-los-datos).
- El nombre del fichero es **siempre el CVE canónico** (`BOME-P-2026-4784.pdf`) o, para un PDF del portal antiguo, la ruta de su URL ya validada (`https://www.melilla.es/mandar.php/n/9/4914/5302_73.pdf` → `melilla-9-4914-5302_73.pdf`): nunca sale de datos del servidor ni de lo que escriba el modelo, y **no hay parámetro para elegir otra ruta**. Solo tú puedes moverla, con `BOME_NAVAJA_PDF_DIR`.
- Del portal antiguo solo se aceptan URL `https://www.melilla.es/mandar.php/n/<número>/<número>/<nombre>.pdf` (sus enlaces `http://` se pasan a https); cualquier otra se rechaza con `url_pdf_invalida` sin tocar la red.
- Una copia válida en caché se reutiliza; `refrescar: true` fuerza la descarga. La escritura es atómica, y una descarga fallida conserva la copia anterior.
- Límite: **100 MB** por PDF (`documento_demasiado_grande`). Como referencia, un boletín ordinario ronda los 4 MB.

---

## 📂 Dónde guarda los datos

| Sistema | Carpeta de datos por defecto |
| --- | --- |
| Windows | `%LOCALAPPDATA%\bome-navaja` (o `~\AppData\Local\bome-navaja` si la variable no existe) |
| macOS | `~/Library/Application Support/bome-navaja` |
| Linux y otros | `$XDG_DATA_HOME/bome-navaja` o, si no está definida, `~/.local/share/bome-navaja` |

Dentro están el índice (`sumarios.sqlite3`), los PDF (`pdfs/`), el estado de la [guardia del sitio](#-seguridad-y-cortesía-con-el-sitio) (`estado_sitio.json`, y `estado_sitio_melilla.json` para el portal antiguo) y la caché del catálogo del [portal antiguo](#-portal-antiguo-melillaes) (`catalogo_portal_antiguo.json`). Nada se crea hasta que hace falta.

| Variable | Efecto |
| --- | --- |
| `BOME_NAVAJA_DATA_DIR` | Cambia la carpeta de datos entera (índice y, salvo otra indicación, PDF) |
| `BOME_NAVAJA_PDF_DIR` | Cambia solo la carpeta de PDF |

Una ruta relativa en estas variables se resuelve contra el directorio de trabajo del proceso, y `estado_servidor` lo indica en el motivo de cada ruta; `~` se expande a tu carpeta de usuario. Un `XDG_DATA_HOME` relativo se ignora, como manda la especificación XDG. Con el paquete `.mcpb`, el ajuste **Carpeta de datos** fija `BOME_NAVAJA_DATA_DIR`; vacío equivale a no definirla.

---

## 🚀 Instalación

Hay tres caminos, de menos a más técnico. Todos necesitan [`uv`](#requisito-uv) salvo el paquete `.mcpb`, que Claude Desktop gestiona solo.

### Opción A — Paquete `.mcpb` para Claude Desktop

1. **Consigue el paquete.** Descarga `bome-navaja-<versión>.mcpb` desde la sección [Releases](https://github.com/jgarcialaneitor/bome-navaja/releases) del repositorio (la primera es [v0.0.1](https://github.com/jgarcialaneitor/bome-navaja/releases/tag/v0.0.1)). Otras opciones:
   - El artefacto `bome-navaja-mcpb` de una ejecución en verde del flujo **CI** (pestaña *Actions* del repositorio), para probar una versión sin publicar.
   - O constrúyelo desde un clon (necesitas `uv` y Node.js con `npx`): `scripts/build_mcpb.sh` en macOS/Linux o `scripts\build_mcpb.ps1` en Windows PowerShell. El resultado queda en `dist/bome-navaja-<versión>.mcpb` (unos 70 KB).
2. Haz **doble clic** en el archivo, o arrástralo a la ventana de Claude Desktop. Aparece el diálogo de instalación con las 19 herramientas.
3. Opcional: en **Carpeta de datos** elige dónde guardar el índice y los PDF. Si la dejas vacía se usa la [carpeta por defecto](#-dónde-guarda-los-datos) de tu sistema.
4. Acepta y reinicia Claude por completo si te lo pide.

La primera vez que arranca, Claude Desktop instala Python y las dependencias por su cuenta (unos segundos).

> [!NOTE]
> El paquete **no está firmado**, así que Claude Desktop puede mostrar una advertencia al instalarlo.

### Requisito: `uv`

Para las opciones B y C, `bome-navaja` no necesita que instales Python: `uv` lo baja solo, junto con las dependencias, la primera vez. Instálalo una vez:

```bash
# macOS y Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Cierra y vuelve a abrir la terminal después de instalarlo.

### Opción B — Claude Code

```bash
claude mcp add bome-navaja -- uvx --from git+https://github.com/jgarcialaneitor/bome-navaja bome-navaja-mcp
```

> [!IMPORTANT]
> El repositorio es **privado** por ahora: `uvx` necesita que tu `git` tenga acceso a él (credenciales de GitHub configuradas). Si no lo tiene, clona el repositorio y usa la variante con `uv run --directory` de la opción C.

### Opción C — Configuración manual de Claude Desktop

1. En Claude Desktop, ve a **Settings → Developer → Edit Config** para abrir `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Añade la entrada dentro de `mcpServers`:

```json
{
  "mcpServers": {
    "bome-navaja": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jgarcialaneitor/bome-navaja",
        "bome-navaja-mcp"
      ]
    }
  }
}
```

   O, desde un clon local (por ejemplo, si no tienes acceso git al repositorio privado):

```json
{
  "mcpServers": {
    "bome-navaja": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/ruta/a/tu/clon/bome-navaja",
        "bome-navaja-mcp"
      ],
      "env": {
        "BOME_NAVAJA_DATA_DIR": "/ruta/opcional/para/los/datos"
      }
    }
  }
}
```

   La clave `env` es opcional.

3. Guarda y **cierra Claude por completo** (no solo la ventana). Al reabrirlo deben aparecer las herramientas de `bome-navaja`.

### Comprobar que quedó bien

Pídele al asistente:

```text
Ejecuta estado_servidor de bome-navaja.
```

Debe responder con la versión, las rutas de datos, PDF e índice (y por qué se eligió cada una), y si SQLite tiene FTS5 y trigram. No toca la red ni crea el índice. Luego prueba una búsqueda real:

```text
Busca en el BOME los artículos sobre ceses de personal eventual y cita sus CVE.
```

---

## 🔒 Seguridad y cortesía con el sitio

- **Pausa de cortesía** de ~0,5 s entre peticiones en las herramientas, con tiempos de espera acotados.
- **Un único cliente serializado** para todas las herramientas: aunque el modelo lance varias a la vez, las peticiones al sitio salen de una en una.
- **La sincronización del índice va más despacio**: su propio cliente espera 2 s más una variación aleatoria de hasta 1 s entre peticiones, indexa como mucho 250 boletines por ejecución y **se detiene sola** (estado `bloqueado`) si el sitio la bloquea. La del portal antiguo va igual. Puedes ajustar las dos con variables de entorno (`estado_servidor` muestra los valores en uso en `cortesia_sincronizacion` y `cortesia_sincronizacion_portal_antiguo`):

  | Variable | Por defecto | Qué hace |
  |---|---|---|
  | `BOME_NAVAJA_SYNC_DELAY` | `2` | Segundos entre peticiones de la sincronización, de los dos orígenes (mínimo 1; un valor menor se sube a 1) |
  | `BOME_NAVAJA_SYNC_MAX_BOLETINES` | `250` | Máximo de boletines por ejecución de `sincronizar_indice`, de los dos orígenes |

- **Guardia del sitio.** El cortafuegos de bomemelilla.es bloquea la IP (en torno a una hora) tras unas 5 respuestas de error, por despacio que vayan las peticiones, y muchas son HTTP 500 de páginas de boletín rotas del propio sitio. Para no llegar a eso:
  - Entre todas las herramientas y la sincronización se admiten como mucho **3 respuestas de error (cualquier 4xx o 5xx) cada 10 minutos**; con el cupo lleno, la sincronización espera y las herramientas responden `pausa_preventiva` con `reintentar_tras_segundos`, sin tocar el sitio.
  - La sincronización hace una pausa de **30–60 s** tras una página rota y no vuelve a pedir un boletín **`roto`** (su página respondió 500 dos veces) salvo con `reintentar_rotos`.
  - Si el sitio bloquea igualmente (403, 429 o 503, o dos peticiones seguidas sin respuesta), `bome-navaja` **deja de tocarlo durante 75 minutos** (o más, si pide `Retry-After`): las herramientas responden `sitio_bloqueando` con `reintentar_tras_segundos` y la sincronización termina `bloqueado`.

  Todos los procesos de `bome-navaja` comparten esta guardia y se conserva entre reinicios: vive en `estado_sitio.json`, en la [carpeta de datos](#-dónde-guarda-los-datos). `estado_servidor` la muestra en `guardia_sitio`. El portal antiguo de melilla.es tiene otra guardia igual pero aparte (`estado_sitio_melilla.json`, `guardia_portal_antiguo`).
- **Portal antiguo**: sus PDF (que su `robots.txt` no permite a los robots) se piden solo bajo demanda, cuando una herramienta los necesita para responderte, nunca en masa. Sus fichas (que tampoco permite) solo se recorren en masa con la [indexación del portal antiguo](#indexar-el-portal-antiguo-melillaes), que arranca solo a mano, va despacio y tiene límite por ejecución; ver [Portal antiguo](#-portal-antiguo-melillaes).
- **No recorre el sitio si no se le pide**: nada al arrancar, y la sincronización solo con `sincronizar_indice`. Nunca corren dos sincronizaciones a la vez sobre el mismo índice, ni de dos procesos ni de dos orígenes.
- Se identifica con un **User-Agent de navegador real** y no usa ni guarda credenciales.
- **El modelo no elige dónde se escribe**: los PDF se nombran por su CVE canónico dentro de la carpeta configurada, y las rutas solo las cambias tú con variables de entorno.
- stdout lleva exclusivamente JSON-RPC; los mensajes para personas van a stderr.

---

## 🧪 Desarrollo

```bash
uv sync
uv run pytest                          # determinista, contra respuestas guardadas del sitio
BOME_NAVAJA_LIVE=1 uv run pytest       # además, los tests marcados live contra el sitio real
```

Los tests `live` consultan bomemelilla.es y se omiten salvo que definas `BOME_NAVAJA_LIVE=1`; la CI nunca los ejecuta.

Para construir el paquete `.mcpb`: `scripts/build_mcpb.sh` (macOS/Linux) o `scripts\build_mcpb.ps1` (Windows). Ambos preparan `build/mcpb` con `scripts/build_mcpb.py`, validan el manifiesto con `npx @anthropic-ai/mcpb validate` y empaquetan en `dist/`. El manifiesto parte de `mcpb/manifest.template.json`.

La CI (`.github/workflows/ci.yml`) tiene cuatro trabajos:

| Trabajo | Qué hace |
| --- | --- |
| `test` | `uv run pytest` en Ubuntu |
| `test-windows` | `uv run pytest` en Windows |
| `bundle` | Construye el `.mcpb` en Ubuntu y lo publica como artefacto `bome-navaja-mcpb` |
| `bundle-windows` | Construye el `.mcpb` con el script de PowerShell en Windows |

---

## 🚧 Limitaciones conocidas

- **El O del sitio no funciona**: su buscador trata el O como Y. `buscar_bomes` lo rechaza; el O solo funciona en `buscar_articulos` y `buscar_en_indice`.
- **Artículos ocultos en los extremos**: los artículos que la página del boletín omite se recuperan cuando dejan un hueco en la numeración, pero no se detectan si faltan al principio o al final del boletín.
- **2014–2016 sin texto ni PDF**: solo se puede buscar en el contenido con `buscar_bomes` y `ambito="contenido"`.
- **El índice solo cubre sumarios**, no el texto completo de los artículos ni de los PDF. Del portal antiguo solo tiene lo que hayas indexado con `sincronizar_indice` y `origen="melilla.es"` (1991–2017 por defecto, en varias ejecuciones); los boletines anteriores a ~1991 no tienen sumarios en el portal.
- **Portal antiguo**: su búsqueda es literal y sin paginar (una búsqueda muy genérica puede superar el límite de 15 MB y pide términos más concretos); los identificadores anteriores a 2014 no son CVE de bomemelilla.es y algunos se repiten; 14 boletines tienen una fecha distinta en cada sitio.
- **Claves mixtas**: algunas respuestas (`ver_bome`, `ver_sumario`, `listar_bomes`) usan claves en inglés (`number`, `date`, `sections`) junto a las castellanas del resto.
- **Política de privacidad**: el sitio no publica un aviso legal, así que el enlace de privacidad del paquete `.mcpb` apunta a su [política de cookies](https://bomemelilla.es/politica-cookies).

---

## 📜 Licencia

MIT. Consulta [`LICENSE`](LICENSE).
