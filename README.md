<div align="center">

# bome-navaja 🗞️🔪

**Servidor MCP para el [Boletín Oficial de la Ciudad Autónoma de Melilla (BOME)](https://bomemelilla.es)**

</div>

---

## 🧭 Qué es bome-navaja

`bome-navaja` es un servidor MCP para que un modelo de IA consulte el BOME de Melilla: listar boletines, leer sus artículos, buscar en el boletín, descargar los PDF y mantener un índice local de sumarios para consultas rápidas sin volver a pedirle todo al sitio.

## 🚧 Estado

En construcción. Todavía no hay herramientas publicadas; la documentación completa (instalación, configuración y herramientas) llegará con la primera versión funcional.

## 🧪 Desarrollo

```bash
uv sync
uv run pytest
```

Los tests marcados como `live` consultan el sitio real y se omiten salvo que definas `BOME_NAVAJA_LIVE=1`. La CI nunca los ejecuta.
