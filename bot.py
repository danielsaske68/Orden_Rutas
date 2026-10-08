import logging
import os
import urllib.parse

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    if "origen" not in user_data:
        user_data["origen"] = DEFAULT_ORIGEN

    msg = (
        "🚚 *Bot Optimizador de Rutas de Paquetería*\n\n"
        f"📍 *Punto de Salida Actual:* `{user_data['origen']}`\n\n"
        "👉 *Instrucciones:* Pega directamente aquí el bloque de"
        " órdenes/servicios (una por línea) y presiona Enviar.\n\n"
        "Para cambiar la dirección de salida pulsa el botón de abajo o usa"
        " /origen."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📍 Cambiar Punto de Salida", callback_data="cambiar_origen"
        )
    ]])
    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=keyboard
    )


async def cambiar_origen_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    context.user_data["esperando_origen"] = True
    await update.message.reply_text(
        "✏️ *Escribe la nueva dirección de salida:*", parse_mode="Markdown"
    )


async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cambiar_origen":
        context.user_data["esperando_origen"] = True
        await query.message.reply_text(
            "✏️ *Escribe la nueva dirección de salida:*", parse_mode="Markdown"
        )


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    texto = update.message.text.strip()

    # Si estem esperant una nova adreça d'eixida
    if user_data.get("esperando_origen"):
        user_data["origen"] = texto
        user_data["esperando_origen"] = False
        await update.message.reply_text(
            f"✅ *Punto de salida actualizado a:*\n`{texto}`",
            parse_mode="Markdown",
        )
        return

    # Processament de la llista d'avisos
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


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("origen", cambiar_origen_cmd))
    app.add_handler(CallbackQueryHandler(boton_callback))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje)
    )
    app.run_polling()
