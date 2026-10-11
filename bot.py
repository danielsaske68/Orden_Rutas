import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Importem el teu nucli de rutes
from route_core import (
    PUNTO_SALIDA,
    geocodificar_orden,
    limpiar_orden,
    matriz_carretara,
)

# Configuració de Logs
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Token del bot (variable d'entorn a Railway)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DEFAULT_ORIGEN = "Carrer de Bernat Descoll, 63, 46026 València, España"
GESTORPRO_DATA = Path(r"C:\Users\Admin\Desktop\GESTORPRO\data\baremos_v2.json")
PRESUPUESTOS_DIR = Path(r"C:\Users\Admin\Desktop\gps\presupuestos")
PRESUPUESTOS_DIR.mkdir(exist_ok=True)


def ordenar_por_duracion(duraciones, cantidad):
    if cantidad <= 1:
        return list(range(cantidad))

    matrix = []
    for fila in duraciones:
        linea = []
        for val in fila:
            linea.append(int(val * 1000) if val is not None else 99999999)
        matrix.append(linea)

    manager = pywrapcp.RoutingIndexManager(len(matrix), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return matrix[from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(
        distance_callback
    )
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # Força la cerca d'eixida des de l'origen amb l'estratègia SAVINGS
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.SAVINGS
    )

    solution = routing.SolveWithParameters(search_parameters)

    if not solution:
        return list(range(cantidad))

    index = routing.Start(0)
    orden = []
    while not routing.IsEnd(index):
        nodo = manager.IndexToNode(index)
        if nodo != 0:
            orden.append(nodo - 1)
        index = solution.Value(routing.NextVar(index))

    # Si per qualsevol motiu la primera parada està més lluny que l'última, inverteix la llista
    if len(orden) > 1:
        d_primera = duraciones[0][orden[0] + 1]
        d_ultima = duraciones[0][orden[-1] + 1]
        primera_dist = d_primera if d_primera is not None else 0
        ultima_dist = d_ultima if d_ultima is not None else 0

        if primera_dist > ultima_dist:
            orden.reverse()

    return orden


def generar_gpx(puntos_ordenados, origen_texto):
    """Genera un arxiu GPX per a navegar en OsmAnd / Organic Maps"""
    gpx = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="RutasBot"'
        ' xmlns="http://www.topografix.com/GPX/1/1">',
        "<rte>",
        f"<name>Ruta de Reparto desde {origen_texto}</name>",
    ]

    for idx, p in enumerate(puntos_ordenados, 1):
        gpx.append(
            f'  <rtept lat="{p["lat"]}" lon="{p["lon"]}"><name>{idx}.'
            f' {p["calle"]} ({p["municipio"]})</name></rtept>'
        )

    gpx.append("</rte></gpx>")
    return "\n".join(gpx)


def menu_principal():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗺️ Rutas", callback_data="menu_rutas"),
            InlineKeyboardButton("💰 Presupuesto", callback_data="menu_presupuesto"),
        ],
    ])


def menu_secundario():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧭 Acomodar ruta", callback_data="acomodar_ruta"),
            InlineKeyboardButton("📍 Cambiar origen de arranque", callback_data="cambiar_origen"),
        ],
        [
            InlineKeyboardButton("🏠 Menú principal", callback_data="menu_inicio"),
        ],
    ])


def menu_presupuesto_secundario():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔎 Buscar baremo", callback_data="buscar_baremo"),
            InlineKeyboardButton("➕ Añadir trabajo", callback_data="agregar_presupuesto"),
        ],
        [
            InlineKeyboardButton("📄 Generar PDF", callback_data="generar_presupuesto"),
            InlineKeyboardButton("🧹 Borrar lista", callback_data="limpiar_presupuesto"),
        ],
        [
            InlineKeyboardButton("🏠 Menú principal", callback_data="menu_inicio"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    if "origen" not in user_data:
        user_data["origen"] = DEFAULT_ORIGEN
    user_data["modo"] = "inicio"

    msg = (
        "🚚 *Bot Optimizador de Rutas y Presupuestos*\n\n"
        f"📍 *Punto de Salida Actual:* `{user_data['origen']}`\n\n"
        "👉 Elige una opción del menú para continuar."
    )
    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=menu_principal()
    )


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_data = context.user_data

    if query.data == "menu_inicio":
        if "origen" not in user_data:
            user_data["origen"] = DEFAULT_ORIGEN
        user_data["modo"] = "inicio"
        await query.edit_message_text(
            "🚚 *Bot Optimizador de Rutas y Presupuestos*\n\n"
            f"📍 *Punto de Salida Actual:* `{user_data['origen']}`\n\n"
            "👉 Elige una opción del menú para continuar.",
            parse_mode="Markdown",
            reply_markup=menu_principal(),
        )
        return

    if query.data == "menu_rutas":
        user_data["modo"] = "rutas"
        await query.edit_message_text(
            "🗺️ *Modo rutas activado*\n\n"
            "Pega aquí las direcciones o servicios, una por línea, y te calcularé la ruta más eficiente.\n\n"
            "Puedes cambiar el origen de arranque o volver a acomodar la ruta desde los botones de abajo.",
            parse_mode="Markdown",
            reply_markup=menu_secundario(),
        )
        return

    if query.data == "menu_presupuesto":
        user_data["modo"] = "presupuesto"
        user_data.setdefault("presupuesto_items", [])
        await query.edit_message_text(
            "💰 *Modo presupuesto activado*\n\n"
            "Aquí puedes buscar baremos, añadir trabajos y sumar un presupuesto final antes de exportarlo a PDF.\n\n"
            "Usa los botones para buscar un baremo o añadir un trabajo al total.",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if query.data == "acomodar_ruta":
        user_data["modo"] = "rutas"
        await query.edit_message_text(
            "🧭 *Acomodar ruta*\n\n"
            "Pega las direcciones una por línea y te la reordeno en la ruta más eficiente.",
            parse_mode="Markdown",
            reply_markup=menu_secundario(),
        )
        return

    if query.data == "buscar_baremo":
        user_data["esperando_baremo"] = True
        user_data["esperando_presupuesto_item"] = False
        await query.edit_message_text(
            "🔎 *Escribe el concepto para buscar en los baremos:*",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if query.data == "agregar_presupuesto":
        user_data["esperando_presupuesto_item"] = True
        user_data["esperando_baremo"] = False
        await query.edit_message_text(
            "➕ *Escribe el trabajo a incluir en el presupuesto:*",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if query.data == "generar_presupuesto":
        items = user_data.get("presupuesto_items", [])
        if not items:
            await query.edit_message_text(
                "📭 *Todavía no has añadido ningún trabajo al presupuesto.*",
                parse_mode="Markdown",
                reply_markup=menu_presupuesto_secundario(),
            )
            return

        presupuesto_total = {
            "titulo": "Presupuesto total",
            "categoria": "Varios trabajos",
            "descripcion": "; ".join(item["descripcion"] for item in items),
            "precio_min": round(sum(item["precio_min"] for item in items), 2),
            "precio_recomendado": round(sum(item["precio_recomendado"] for item in items), 2),
            "precio_max": round(sum(item["precio_max"] for item in items), 2),
            "observaciones": f"Presupuesto acumulado con {len(items)} trabajos incluidos.",
            "trabajos": [item["titulo"] for item in items],
        }

        resumen = (
            "💰 *Presupuesto acumulado*\n\n"
            f"📦 *Trabajos:* {len(items)}\n"
            f"💵 *Total estimado:* {presupuesto_total['precio_recomendado']} €\n"
            f"📉 *Mínimo:* {presupuesto_total['precio_min']} €\n"
            f"📈 *Máximo:* {presupuesto_total['precio_max']} €"
        )
        await query.edit_message_text(resumen, parse_mode="Markdown")

        pdf_path = PRESUPUESTOS_DIR / f"presupuesto_total_{query.from_user.id}_{int(query.id)}.pdf"
        if generar_pdf_presupuesto(presupuesto_total, pdf_path):
            with open(pdf_path, "rb") as doc:
                await query.message.reply_document(
                    document=doc,
                    filename=pdf_path.name,
                    caption="📄 *PDF del presupuesto acumulado*",
                    parse_mode="Markdown",
                )
        return

    if query.data == "limpiar_presupuesto":
        user_data["presupuesto_items"] = []
        await query.edit_message_text(
            "🧹 *Lista de presupuesto vaciada.*",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if query.data == "cambiar_origen":
        user_data["esperando_origen"] = True
        await query.edit_message_text(
            "✏️ *Escribe la nueva dirección de salida:*",
            parse_mode="Markdown",
            reply_markup=menu_secundario(),
        )


async def cambiar_origen_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["esperando_origen"] = True
    await update.message.reply_text(
        "✏️ *Escribe la nueva dirección de salida:*", parse_mode="Markdown"
    )


def normalizar_texto(valor):
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    return re.sub(r"\s+", " ", texto)


def cargar_baremos():
    try:
        if not GESTORPRO_DATA.exists():
            return []
        with open(GESTORPRO_DATA, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logging.warning("No se pudieron cargar los baremos: %s", exc)
        return []


def buscar_baremos_similares(texto, limite=5):
    baremos = cargar_baremos()
    if not baremos:
        return []

    target = normalizar_texto(texto)
    resultados = []

    for baremo in baremos:
        haystack = " ".join([
            str(baremo.get("nombre", "")),
            str(baremo.get("categoria", "")),
            " ".join(str(x) for x in baremo.get("alias", []) or []),
            " ".join(str(x) for x in baremo.get("buscar", []) or []),
        ])
        haystack_norm = normalizar_texto(haystack)

        score = 0
        for palabra in re.findall(r"[a-zA-Z0-9]+", target):
            if len(palabra) < 3:
                continue
            if palabra in haystack_norm:
                score += 2

        if target and target in haystack_norm:
            score += 10

        if score > 0:
            resultados.append({
                "baremo": baremo,
                "score": score,
                "nombre": str(baremo.get("nombre", "Sin nombre")).strip(),
                "categoria": str(baremo.get("categoria", "General")).strip(),
            })

    resultados.sort(key=lambda item: item["score"], reverse=True)
    return resultados[:limite]


def buscar_baremo(texto):
    candidatos = buscar_baremos_similares(texto, limite=1)
    if not candidatos:
        return None
    return candidatos[0]["baremo"]


def generar_pdf_presupuesto(data, filepath):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        doc = SimpleDocTemplate(
            str(filepath),
            pagesize=A4,
            leftMargin=36,
            rightMargin=36,
            topMargin=34,
            bottomMargin=34,
        )
        styles = getSampleStyleSheet()
        story = []

        titulo = str(data.get("titulo", "Presupuesto")).strip() or "Presupuesto"
        categoria = str(data.get("categoria", "General")).strip() or "General"
        descripcion = str(data.get("descripcion", "")).strip() or "Sin descripción"
        observaciones = str(data.get("observaciones", "")).strip() or "Sin observaciones"
        trabajos = data.get("trabajos") or [titulo]

        story.append(Paragraph("<b>GESTOR PRO</b>", styles["Heading1"]))
        story.append(Paragraph("Presupuesto técnico", styles["Heading2"]))
        story.append(Spacer(1, 8))
        story.append(Paragraph(f"Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}", styles["BodyText"]))
        story.append(Spacer(1, 18))

        header_data = [
            [Paragraph("<b>Concepto</b>", styles["BodyText"]), Paragraph(titulo, styles["BodyText"])],
            [Paragraph("<b>Categoría</b>", styles["BodyText"]), Paragraph(categoria, styles["BodyText"])],
            [Paragraph("<b>Descripción</b>", styles["BodyText"]), Paragraph(descripcion[:220], styles["BodyText"])],
        ]
        header_table = Table(header_data, colWidths=[140, 360])
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f7fb")),
            ("GRID", (0, 0), (-1, -1), 1, colors.HexColor("#d0d9e5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 18))

        lineas = [["Concepto", "Mínimo", "Recomendado", "Máximo"]]
        for idx, trabajo in enumerate(trabajos[:6], 1):
            nombre = str(trabajo).strip() or f"Trabajo {idx}"
            if len(nombre) > 48:
                nombre = nombre[:45] + "..."
            lineas.append([
                nombre,
                f"{float(data.get('precio_min', 0) or 0) / max(len(trabajos or [1]), 1):.2f} €",
                f"{float(data.get('precio_recomendado', 0) or 0) / max(len(trabajos or [1]), 1):.2f} €",
                f"{float(data.get('precio_max', 0) or 0) / max(len(trabajos or [1]), 1):.2f} €",
            ])

        lineas.append([
            "TOTAL",
            f"{float(data.get('precio_min', 0) or 0):.2f} €",
            f"{float(data.get('precio_recomendado', 0) or 0):.2f} €",
            f"{float(data.get('precio_max', 0) or 0):.2f} €",
        ])

        tabla = Table(lineas, colWidths=[200, 90, 110, 100])
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4d7a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 1, colors.HexColor("#b0bec9")),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f8fafd")]),
            ("BACKGROUND", (-4, -1), (-1, -1), colors.HexColor("#dfeaf7")),
            ("FONTNAME", (-4, -1), (-1, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (-4, -1), (-1, -1), colors.HexColor("#16314d")),
        ]))
        story.append(tabla)
        story.append(Spacer(1, 18))

        story.append(Paragraph("<b>Observaciones</b>", styles["Heading2"]))
        story.append(Paragraph(observaciones, styles["BodyText"]))
        story.append(Spacer(1, 18))

        if trabajos and isinstance(trabajos, list):
            story.append(Paragraph("<b>Trabajos incluidos</b>", styles["Heading2"]))
            for idx, trabajo in enumerate(trabajos[:8], 1):
                story.append(Paragraph(f"{idx}. {str(trabajo).strip()}", styles["BodyText"]))

        story.append(Spacer(1, 20))
        story.append(Paragraph("Documento generado por GESTOR PRO Telegram", styles["Italic"]))

        doc.build(story)
        return True
    except Exception as exc:
        logging.warning("No se pudo generar el PDF profesional: %s", exc)
        return False


async def estimar_presupuesto_basico(texto):
    baremo = buscar_baremo(texto)
    if baremo:
        base = float(baremo.get("precio_recomendado", 0) or 0)
        minimo = float(baremo.get("precio_min", base) or base)
        maximo = float(baremo.get("precio_max", base) or base)
        prioridad = 1.0
        t = normalizar_texto(texto)
        if any(k in t for k in ["urgente", "hoy mismo", "fin de semana", "noche"]):
            prioridad *= 1.2
        if any(k in t for k in ["acceso dificil", "acceso difícil", "muebles", "pared", "falso techo"]):
            prioridad *= 1.15
        if any(k in t for k in ["material", "pieza", "válvula", "brida", "sifon", "racor"]):
            prioridad *= 1.1

        data = {
            "titulo": str(baremo.get("nombre", "Presupuesto estimado")).strip() or "Presupuesto estimado",
            "categoria": str(baremo.get("categoria", "General")).strip() or "General",
            "descripcion": texto,
            "precio_min": round(max(0, minimo * prioridad), 2),
            "precio_recomendado": round(max(0, base * prioridad), 2),
            "precio_max": round(max(0, maximo * prioridad), 2),
            "observaciones": "Estimación basada en el baremo del gestor y ajustada por urgencia, accesos y materiales extra.",
            "trabajos": [
                str(baremo.get("nombre", "Trabajo técnico")),
                "Comprobación final del servicio",
                "Prueba de funcionamiento",
            ],
        }
        return data

    t = normalizar_texto(texto)
    base = 90.0

    if any(k in t for k in ["wc", "vater", "inodoro", "desatasco", "atasco"]):
        base = 120.0
    if any(k in t for k in ["llave", "grifo", "latiguillo", "fuga"]):
        base = 140.0
    if any(k in t for k in ["tuberia", "cobre", "pex", "multicapa", "tramo"]):
        base = 180.0
    if any(k in t for k in ["urgente", "hoy mismo", "fin de semana", "noche"]):
        base *= 1.2
    if any(k in t for k in ["acceso dificil", "acceso difícil", "muebles", "pared", "falso techo"]):
        base *= 1.15
    if any(k in t for k in ["material", "pieza", "válvula", "brida", "sifon", "racor"]):
        base *= 1.1

    return {
        "titulo": "Presupuesto estimado",
        "categoria": "General",
        "descripcion": texto,
        "precio_min": round(base * 0.9, 2),
        "precio_recomendado": round(base, 2),
        "precio_max": round(base * 1.25, 2),
        "observaciones": "Estimación rápida del bot. Si quieres, se puede ajustar con el baremo del gestor o IA más avanzada.",
        "trabajos": [
            "Diagnóstico del problema",
            "Intervención técnica",
            "Prueba final",
        ],
    }


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    texto = update.message.text.strip()

    if user_data.get("esperando_origen"):
        user_data["origen"] = texto
        user_data["esperando_origen"] = False
        await update.message.reply_text(
            f"✅ *Punto de salida actualizado a:*\n`{texto}`",
            parse_mode="Markdown",
            reply_markup=menu_principal(),
        )
        return

    if user_data.get("esperando_baremo"):
        if texto.lower() in {"ia", "usar ia", "ia opcional", "usar la ia"}:
            user_data["esperando_baremo"] = False
            user_data["baremo_candidatos"] = []
            await update.message.reply_text(
                "🧠 *IA como apoyo.*\n\n"
                "La base del presupuesto sigue siendo el baremo local. La IA puede ayudarte a decidir o ajustar si hay varios casos parecidos.",
                parse_mode="Markdown",
                reply_markup=menu_presupuesto_secundario(),
            )
            return

        if texto.isdigit() and user_data.get("baremo_candidatos"):
            idx = int(texto) - 1
            if 0 <= idx < len(user_data["baremo_candidatos"]):
                baremo = user_data["baremo_candidatos"][idx]
                user_data["esperando_baremo"] = False
                user_data["baremo_candidatos"] = []
                precio = float(baremo.get("precio_recomendado", 0) or 0)
                await update.message.reply_text(
                    "✅ *Baremo seleccionado*\n\n"
                    f"📌 *Nombre:* {baremo.get('nombre', 'Sin nombre')}\n"
                    f"📂 *Categoría:* {baremo.get('categoria', 'General')}\n"
                    f"💵 *Precio recomendado:* {precio:.2f} €\n"
                    f"📝 *Alias:* {', '.join(str(x) for x in (baremo.get('alias', []) or [])[:5]) or 'Sin alias'}",
                    parse_mode="Markdown",
                    reply_markup=menu_presupuesto_secundario(),
                )
                return

        candidatos = buscar_baremos_similares(texto, limite=5)
        if not candidatos:
            user_data["esperando_baremo"] = False
            user_data["baremo_candidatos"] = []
            await update.message.reply_text(
                "🔎 *No encontré coincidencias en los baremos.*\n\n"
                "Prueba con otra descripción o usa añadir trabajo para estimarlo directamente.",
                parse_mode="Markdown",
                reply_markup=menu_presupuesto_secundario(),
            )
            return

        if len(candidatos) == 1:
            baremo = candidatos[0]["baremo"]
            user_data["esperando_baremo"] = False
            user_data["baremo_candidatos"] = []
            precio = float(baremo.get("precio_recomendado", 0) or 0)
            await update.message.reply_text(
                "🔎 *Baremo encontrado*\n\n"
                f"📌 *Nombre:* {baremo.get('nombre', 'Sin nombre')}\n"
                f"📂 *Categoría:* {baremo.get('categoria', 'General')}\n"
                f"💵 *Precio recomendado:* {precio:.2f} €\n"
                f"📝 *Descripción:* {baremo.get('descripcion', 'Sin descripción')}",
                parse_mode="Markdown",
                reply_markup=menu_presupuesto_secundario(),
            )
            return

        user_data["baremo_candidatos"] = [item["baremo"] for item in candidatos]
        lista = "\n".join(
            f"{idx}. {item['nombre']} ({item['categoria']}) - score {item['score']}"
            for idx, item in enumerate(candidatos, 1)
        )
        await update.message.reply_text(
            "🔎 *He encontrado varias coincidencias en el archivo local.*\n\n"
            f"{lista}\n\n"
            "Escribe el número que quieras usar, o escribe *IA* si prefieres que la IA te ayude a decidir.",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if user_data.get("esperando_presupuesto_item"):
        user_data["esperando_presupuesto_item"] = False
        presupuesto = await estimar_presupuesto_basico(texto)
        user_data.setdefault("presupuesto_items", []).append({
            **presupuesto,
            "descripcion": texto,
            "titulo": presupuesto.get("titulo") or "Trabajo estimado",
        })

        total = sum(item["precio_recomendado"] for item in user_data["presupuesto_items"])
        await update.message.reply_text(
            "✅ *Trabajo añadido al presupuesto*\n\n"
            f"📌 *Descripción:* {texto}\n"
            f"💵 *Importe estimado:* {presupuesto['precio_recomendado']} €\n"
            f"🧾 *Total acumulado:* {total:.2f} €",
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )
        return

    if user_data.get("modo") == "presupuesto":
        presupuesto = await estimar_presupuesto_basico(texto)
        user_data.setdefault("presupuesto_items", []).append({
            **presupuesto,
            "descripcion": texto,
            "titulo": presupuesto.get("titulo") or "Trabajo estimado",
        })

        total = sum(item["precio_recomendado"] for item in user_data["presupuesto_items"])
        resumen = (
            "💰 *Presupuesto acumulado*\n\n"
            f"📌 *Título:* {presupuesto['titulo']}\n"
            f"📂 *Categoría:* {presupuesto['categoria']}\n"
            f"📝 *Descripción:* {presupuesto['descripcion']}\n"
            f"💵 *Mínimo:* {presupuesto['precio_min']} €\n"
            f"✅ *Recomendado:* {presupuesto['precio_recomendado']} €\n"
            f"💸 *Máximo:* {presupuesto['precio_max']} €\n\n"
            f"🧾 *Total acumulado:* {total:.2f} €\n"
            f"📌 *Observaciones:* {presupuesto['observaciones']}"
        )
        await update.message.reply_text(
            resumen,
            parse_mode="Markdown",
            reply_markup=menu_presupuesto_secundario(),
        )

        pdf_path = PRESUPUESTOS_DIR / f"presupuesto_{update.message.from_user.id}_{int(update.message.date.timestamp())}.pdf"
        if generar_pdf_presupuesto(presupuesto, pdf_path):
            with open(pdf_path, "rb") as doc:
                await update.message.reply_document(
                    document=doc,
                    filename=pdf_path.name,
                    caption="📄 *Presupuesto generado en PDF*",
                    parse_mode="Markdown",
                )
        return

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if not lineas:
        return

    msg_espera = await update.message.reply_text(
        f"⏳ *Geolocalizando y calculando ruta óptima para {len(lineas)}"
        " avisos...*"
    )

    registros = [limpiar_orden(l) for l in lineas]
    localizados = []

    for reg in registros:
        geo, _ = geocodificar_orden(reg, pause=False)
        if geo:
            reg.update(geo)
            localizados.append(reg)

    if not localizados:
        await msg_espera.edit_text(
            "❌ No se pudo geolocalizar ninguna dirección."
        )
        return

    origen = user_data.get("origen", DEFAULT_ORIGEN)

    # Obtenir coordenades de l'origen
    from geopy.geocoders import Nominatim

    geo_origen = Nominatim(user_agent="bot_rutas_valencia").geocode(
        origen, country_codes="es"
    )

    if not geo_origen:
        await msg_espera.edit_text(
            "❌ No se pudo geolocalizar el punto de salida actual."
        )
        return

    coords = [(float(geo_origen.latitude), float(geo_origen.longitude))] + [
        (x["lat"], x["lon"]) for x in localizados
    ]
    distancias, duraciones = matriz_carretara(coords)

    orden_idx = ordenar_por_duracion(duraciones, len(localizados))
    ruta_ordenada = [localizados[i] for i in orden_idx]

    # Calcular totals
    total_m = sum(
        distancias[0 if i == 0 else orden_idx[i - 1] + 1][orden_idx[i] + 1]
        or 0
        for i in range(len(orden_idx))
    )
    total_s = sum(
        duraciones[0 if i == 0 else orden_idx[i - 1] + 1][orden_idx[i] + 1]
        or 0
        for i in range(len(orden_idx))
    )

    resumen = (
        "📍 *RUTA ÓPTIMA CALCULADA*\n"
        f"🏁 *Salida:* `{origen}`\n"
        f"📦 *Avisos:* {len(ruta_ordenada)} | 🚗 *Km:* {total_m/1000:.1f} km | ⏱️"
        f" *Tiempo:* {int(total_s//60)} min\n\n"
    )

    for idx, p in enumerate(ruta_ordenada, 1):
        resumen += (
            f"*{idx}.* `{p.get('expediente','')}` - {p['calle']}, *{p['municipio']}*\n"
        )

    # Crear arxiu GPX
    gpx_content = generar_gpx(ruta_ordenada, origen)
    with open("ruta.gpx", "w", encoding="utf-8") as f:
        f.write(gpx_content)

    await msg_espera.delete()

    # Enviar resum
    await update.message.reply_text(resumen, parse_mode="Markdown")

    # Enviar arxiu GPX
    with open("ruta.gpx", "rb") as doc:
        await update.message.reply_document(
            document=doc,
            filename="ruta_reparto.gpx",
            caption=(
                "📱 *Abre este archivo GPX en OsmAnd u Organic Maps para navegar"
                " sin límites de Google.*"
            ),
        )


def main() -> None:
    """Inicializa y arranca el bot de Telegram."""
    if not TOKEN:
        raise RuntimeError(
            "Falta la variable de entorno TELEGRAM_TOKEN. "
            "Configúrela antes de iniciar el bot."
        )

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("origen", cambiar_origen_cmd))
    application.add_handler(
        CallbackQueryHandler(
            handle_menu_callback,
            pattern="menu_|acomodar_ruta|cambiar_origen|buscar_baremo|agregar_presupuesto|generar_presupuesto|limpiar_presupuesto",
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje)
    )

    logging.info("Bot de rutas iniciado en Telegram.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
