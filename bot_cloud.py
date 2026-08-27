import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from bot_v4 import (
    ZONA_NUEVA_YORK,
    analizar_mercado,
    enviar_telegram,
    formatear_noticias_macro,
    obtener_noticias_macro,
    registrar_senal,
    sesion_nueva_york_abierta,
)


ARCHIVO_ESTADO = Path("estado_cloud.json")
ESPERA_MINIMA_ALERTAS = 900
MAXIMOS_MACRO_VISTOS = 100


def estado_inicial():
    return {
        "sesion_abierta": False,
        "ultimo_estado_notificado": None,
        "ultimo_envio": None,
        "macro_vistos": [],
        "ultimo_error_notificado": None,
    }


def cargar_estado():
    if not ARCHIVO_ESTADO.exists():
        return estado_inicial()
    try:
        estado = estado_inicial()
        estado.update(json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8")))
        return estado
    except (json.JSONDecodeError, OSError, TypeError):
        return estado_inicial()


def guardar_estado(estado):
    ARCHIVO_ESTADO.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def tiempo_desde_ultimo_envio(estado, ahora):
    texto = estado.get("ultimo_envio")
    if not texto:
        return None
    try:
        return (ahora - datetime.fromisoformat(texto)).total_seconds()
    except (ValueError, TypeError):
        return None


def noticias_macro_nuevas(estado, ahora):
    noticias = obtener_noticias_macro()
    vistos = set(estado.get("macro_vistos", []))
    nuevas = []
    for noticia in noticias:
        fecha = noticia.get("fecha")
        es_reciente = fecha is None or ahora - timedelta(hours=36) <= fecha <= ahora + timedelta(hours=1)
        if noticia["id"] not in vistos and es_reciente:
            nuevas.append(noticia)
    return noticias, nuevas


def marcar_macro_vistas(estado, noticias):
    acumuladas = list(estado.get("macro_vistos", []))
    acumuladas.extend(noticia["id"] for noticia in noticias)
    estado["macro_vistos"] = list(dict.fromkeys(acumuladas))[-MAXIMOS_MACRO_VISTOS:]


def ejecutar_prueba_manual():
    enviar_telegram(
        "🧪 NASDAQ SENTINEL V4 EN LA NUBE\n\n"
        "Conexion con Telegram verificada correctamente."
    )
    try:
        resultado = analizar_mercado()
        enviar_telegram("🧪 PRUEBA DE MERCADO\n\n" + resultado["mensaje"])
        print("Analisis de mercado enviado correctamente.")
    except Exception as error:
        enviar_telegram(
            "ℹ️ PRUEBA DE MERCADO NO DISPONIBLE\n\n"
            "La conexion con Telegram funciona, pero no se pudo obtener "
            f"la lectura de mercado en esta prueba. Detalle: {type(error).__name__}: {error}"
        )
        print(f"Mercado no disponible durante la prueba: {error}")

    try:
        noticias = obtener_noticias_macro(maximas=3)
        enviar_telegram(formatear_noticias_macro(noticias, "🧪 PRUEBA MACRO OFICIAL"))
        print("Bloque macro enviado correctamente.")
    except Exception as error:
        print(f"Bloque macro no disponible durante la prueba: {error}")


def notificar_error_una_vez(estado, error):
    firma = f"{type(error).__name__}:{str(error)[:160]}"
    if estado.get("ultimo_error_notificado") == firma:
        return
    enviar_telegram(
        "⚠️ NASDAQ SENTINEL V4 — INCIDENCIA\n\n"
        "El analisis no se ha completado en esta ejecucion. "
        "El bot volvera a intentarlo automaticamente.\n\n"
        f"Detalle: {firma}"
    )
    estado["ultimo_error_notificado"] = firma
    guardar_estado(estado)


def ejecutar_programacion():
    ahora = datetime.now(ZONA_NUEVA_YORK)
    estado = cargar_estado()
    abierta = sesion_nueva_york_abierta(ahora)

    if not abierta:
        if estado.get("sesion_abierta"):
            enviar_telegram(
                "🔵 SESION DE NUEVA YORK FINALIZADA\n\n"
                "Nasdaq Sentinel V4 queda en espera hasta la proxima sesion."
            )
            estado["sesion_abierta"] = False
            guardar_estado(estado)
            print("Aviso de cierre enviado.")
        else:
            print("Fuera de la sesion. No es necesario analizar.")
        return

    if not estado.get("sesion_abierta"):
        enviar_telegram(
            "🟢 SESION DE NUEVA YORK ABIERTA\n\n"
            "Nasdaq Sentinel V4 inicia la vigilancia tecnica y macro en la nube."
        )
        estado["sesion_abierta"] = True
        guardar_estado(estado)

    try:
        noticias, nuevas = noticias_macro_nuevas(estado, ahora)
        if nuevas:
            enviar_telegram(formatear_noticias_macro(nuevas, "🚨 NOVEDAD MACRO OFICIAL"))
        marcar_macro_vistas(estado, noticias)

        resultado = analizar_mercado()
        estado_actual = resultado["estado"]
        ultimo = estado.get("ultimo_estado_notificado")
        segundos = tiempo_desde_ultimo_envio(estado, ahora)
        espera = segundos is None or segundos >= ESPERA_MINIMA_ALERTAS

        if estado_actual != ultimo and espera:
            enviar_telegram(resultado["mensaje"])
            registrar_senal(resultado)
            estado["ultimo_estado_notificado"] = estado_actual
            estado["ultimo_envio"] = ahora.isoformat()
            print(f"Alerta enviada: {estado_actual}")
        else:
            print(f"Sin alerta nueva: {estado_actual}")

        estado["ultimo_error_notificado"] = None
        guardar_estado(estado)
    except Exception as error:
        print(f"Error durante la ejecucion: {type(error).__name__}: {error}")
        notificar_error_una_vez(estado, error)
        raise


def main():
    load_dotenv()
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise ValueError("Falta TELEGRAM_BOT_TOKEN")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        raise ValueError("Falta TELEGRAM_CHAT_ID")
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        ejecutar_prueba_manual()
    else:
        ejecutar_programacion()


if __name__ == "__main__":
    main()
