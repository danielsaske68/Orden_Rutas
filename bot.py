import os
import logging
import urllib.parse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)

# Importamos tu núcleo de rutas
from route_core import (
    limpiar_orden, geocodificar_orden, matriz_carretara, PUNTO_SALIDA
)

# Configuración de Logs
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Token del bot (se configurará como Variable de Entorno en Railway)
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DEFAULT_ORIGEN = "Carrer de Bernat Descoll, 63, 46026 València, España"

def ordenar_por_duracion(duraciones, cantidad):
    if cantidad <= 1:
        return list(range(cantidad))
    pendientes = set(range(cantidad))
    orden = []
    nodo_actual = 0
    while pendientes:
        candidatos = [i for i in pendientes if duraciones[nodo_actual][i + 1] is not None]
        if not candidatos:
            break
        siguiente = min(candidatos, key=lambda i: duraciones[nodo_actual][i + 1])
        orden.append(siguiente)
        pendientes.remove(siguiente)
        nodo_actual = siguiente + 1

    # Bucle 2-Opt
    def t(a, b): return duraciones[a][b] or float("inf")
    for _ in range(300):
        mejoro = False
        for i in range(0, len(orden) - 1):
            a = 0 if i == 0 else orden[i - 1] + 1
            b = orden[i] + 1
            for j in range(i + 1, len(orden)):
                c = orden[j] + 1
                d = orden[j + 1] + 1 if j + 1 < len(orden) else None
                actual = t(a, b) + (t(c, d) if d else 0)
                nuevo = t(a, c) + (t(b, d) if d else 0)
                if nuevo + 1 < actual:
                    orden[i:j + 1] = reversed(orden[i:j + 1])
                    mejoro = True
                    break
            if mejoro: break
        if not mejoro: break
    return orden

def generar_gpx(puntos_ordenados, origen_texto):
    """Genera un archivo GPX para navegar en OsmAnd / Organic Maps"""
    gpx = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="RutasBot" xmlns="http://www.topografix.com/GPX/1/1">',
           '<rte>', f'<name>Ruta de Reparto desde {origen_texto}</name>']
    
    for idx, p in enumerate(puntos_ordenados, 1):
        gpx.append(f'  <rtept lat="{p["lat"]}" lon="{p["lon"]}"><name>{idx}. {p["calle"]} ({p["municipio"]})</name></rtept>')
    
    gpx.append('</rte></gpx>')
    return "\n".join(gpx)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    if "origen" not in user_data:
        user_data["origen"] = DEFAULT_ORIGEN

    msg = (
        "🚚 *Bot Optimizador de Rutas de Paquetería*\n\n"
        f"📍 *Punto de Salida Actual:* `{user_data['origen']}`\n\n"
        "👉 *Instrucciones:* Pega directamente aquí el bloque de órdenes/servicios (una por línea) y presiona Enviar.\n\n"
        "Para cambiar la dirección de salida pulsa el botón de abajo o usa /origen."
    )
    keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("📍 Cambiar Punto de Salida", callback_data="cambiar_origen")] 
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)

async def cambiar_origen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["esperando_origen"] = True
    await update.message.reply_text("✏️ *Escribe la nueva dirección de salida:*", parse_mode="Markdown")

async def boton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cambiar_origen":
        context.user_data["esperando_origen"] = True
        await query.message.reply_text("✏️ *Escribe la nueva dirección de salida:*", parse_mode="Markdown")

async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data
    texto = update.message.text.strip()

    # Si estamos esperando un nuevo punto de origen
    if user_data.get("esperando_origen"):
        user_data["origen"] = texto
        user_data["esperando_origen"] = False
        await update.message.reply_text(f"✅ *Punto de salida actualizado a:*\n`{texto}`", parse_mode="Markdown")
        return

    # Procesamiento de lista de avisos
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if not lineas:
        return

    msg_espera = await update.message.reply_text(f"⏳ *Geolocalizando y calculando ruta óptima para {len(lineas)} avisos...*")

    registros = [limpiar_orden(l) for l in lineas]
    localizados = []
    
    for reg in registros:
        geo, _ = geocodificar_orden(reg, pause=False)
        if geo:
            reg.update(geo)
            localizados.append(reg)

    if not localizados:
        await msg_espera.edit_text("❌ No se pudo geolocalizar ninguna dirección.")
        return

    origen = user_data.get("origen", DEFAULT_ORIGEN)
    
    # Obtener coords del origen
    from geopy.geocoders import Nominatim
    geo_origen = Nominatim(user_agent="bot_rutas_valencia").geocode(origen, country_codes="es")
    
    if not geo_origen:
        await msg_espera.edit_text("❌ No se pudo geolocalizar el punto de salida actual.")
        return

    coords = [(float(geo_origen.latitude), float(geo_origen.longitude))] + [(x["lat"], x["lon"]) for x in localizados]
    distancias, duraciones = matriz_carretara(coords)

    orden_idx = ordenar_por_duracion(duraciones, len(localizados))
    ruta_ordenada = [localizados[i] for i in orden_idx]

    # Calcular totales
    total_m = sum(distancias[0 if i==0 else orden_idx[i-1]+1][orden_idx[i]+1] or 0 for i in range(len(orden_idx)))
    total_s = sum(duraciones[0 if i==0 else orden_idx[i-1]+1][orden_idx[i]+1] or 0 for i in range(len(orden_idx)))

    resumen = (
        f"📍 *RUTA ÓPTIMA CALCULADA*\n"
        f"🏁 *Salida:* `{origen}`\n"
        f"📦 *Avisos:* {len(ruta_ordenada)} | 🚗 *Km:* {total_m/1000:.1f} km | ⏱️ *Tiempo:* {int(total_s//60)} min\n\n"
    )

    for idx, p in enumerate(ruta_ordenada, 1):
        resumen += f"*{idx}.* `{p.get('expediente','')}` - {p['calle']}, *{p['municipio']}*\n"

    # Crear archivo GPX para enviar
    gpx_content = generar_gpx(ruta_ordenada, origen)
    with open("ruta.gpx", "w", encoding="utf-8") as f:
        f.write(gpx_content)

    await msg_espera.delete()
    
    # Enviar resumen
    await update.message.reply_text(resumen, parse_mode="Markdown")
    
    # Enviar archivo GPX listo para OsmAnd / Organic Maps
    with open("ruta.gpx", "rb") as doc:
        await update.message.reply_document(
            document=doc,
            filename="ruta_reparto.gpx",
            caption="📱 *Abre este archivo GPX en OsmAnd u Organic Maps para navegar sin límites de Google.*"
        )

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("origen", cambiar_origen_cmd))
    app.add_handler(CallbackQueryHandler(boton_callback))  # <-- Añadir esta línea
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))
    app.run_polling()
