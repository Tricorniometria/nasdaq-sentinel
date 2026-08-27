import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from bot_v4 import (
    ARCHIVO_SENALES,
    ZONA_NUEVA_YORK,
    analizar_mercado,
    enviar_telegram,
    formatear_noticias_macro,
    obtener_noticias_macro,
    sesion_nueva_york_abierta,
)

ARCHIVO_ESTADO = Path("estado_cloud.json")
ESPERA_MINIMA_ALERTAS = 900
CADUCIDAD_ENTRADA_MINUTOS = 90
MAXIMOS_MACRO_VISTOS = 100

CAMPOS_RESULTADO = [
    "fecha_hora_nueva_york", "estado", "precio", "entrada_baja",
    "entrada_alta", "stop", "objetivo_1", "objetivo_2", "rsi14",
    "atr14", "id_operacion", "direccion", "fecha_entrada",
    "fecha_cierre", "entrada_ejecutada", "precio_salida",
    "resultado_final", "resultado_r", "tp1_alcanzado",
]


def resumen_vacio(fecha=None):
    return {
        "fecha": fecha, "cerradas": 0, "stops": 0, "objetivos_2": 0,
        "breakeven": 0, "cierres_sesion": 0, "canceladas": 0,
        "r_total": 0.0,
    }


def estado_inicial():
    return {
        "sesion_abierta": False,
        "ultimo_estado_notificado": None,
        "ultimo_envio": None,
        "macro_vistos": [],
        "ultimo_error_notificado": None,
        "operacion_abierta": None,
        "resumen_sesion": resumen_vacio(),
    }


def cargar_estado():
    if not ARCHIVO_ESTADO.exists():
        return estado_inicial()
    try:
        estado = estado_inicial()
        estado.update(json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8")))
        if not isinstance(estado.get("resumen_sesion"), dict):
            estado["resumen_sesion"] = resumen_vacio()
        return estado
    except (json.JSONDecodeError, OSError, TypeError):
        return estado_inicial()


def guardar_estado(estado):
    ARCHIVO_ESTADO.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
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
        reciente = (
            fecha is None
            or ahora - timedelta(hours=36) <= fecha <= ahora + timedelta(hours=1)
        )
        if noticia["id"] not in vistos and reciente:
            nuevas.append(noticia)
    return noticias, nuevas


def marcar_macro_vistas(estado, noticias):
    acumuladas = list(estado.get("macro_vistos", []))
    acumuladas.extend(noticia["id"] for noticia in noticias)
    estado["macro_vistos"] = list(dict.fromkeys(acumuladas))[-MAXIMOS_MACRO_VISTOS:]


def crear_operacion(resultado, ahora):
    plan = resultado["plan"]
    entrada = (plan["entrada_baja"] + plan["entrada_alta"]) / 2
    return {
        "id": ahora.strftime("%Y%m%d-%H%M%S"),
        "creada": ahora.isoformat(),
        "estado_senal": resultado["estado"],
        "direccion": plan["direccion"],
        "precio_senal": resultado["precio"],
        "entrada_baja": plan["entrada_baja"],
        "entrada_alta": plan["entrada_alta"],
        "entrada": entrada,
        "stop_inicial": plan["stop"],
        "stop_actual": plan["stop"],
        "objetivo_1": plan["objetivo_1"],
        "objetivo_2": plan["objetivo_2"],
        "rsi14": resultado["rsi"],
        "atr14": resultado["atr"],
        "estado_operacion": "ESPERANDO_ENTRADA",
        "fecha_entrada": None,
        "fecha_cierre": None,
        "precio_salida": None,
        "resultado_final": None,
        "resultado_r": None,
        "tp1_alcanzado": False,
        "ultimo_precio": resultado["precio"],
        "ultima_vela_revisada": resultado["ultima_vela"].isoformat(),
    }


def mensaje_configuracion(operacion):
    return (
        "⏳ CONFIGURACION SIMULADA CREADA\n\n"
        f"Direccion: {operacion['direccion']}\n"
        f"Zona: {operacion['entrada_baja']:.2f} - {operacion['entrada_alta']:.2f}\n"
        f"Entrada de control: {operacion['entrada']:.2f}\n"
        f"Stop inicial: {operacion['stop_inicial']:.2f}\n"
        f"Objetivo 1: {operacion['objetivo_1']:.2f}\n"
        f"Objetivo 2: {operacion['objetivo_2']:.2f}\n\n"
        "No se considera iniciada hasta que una vela posterior alcance la "
        "entrada de control. Caduca en 90 minutos."
    )


def cerrar_operacion(operacion, resultado_final, precio_salida, ahora, resultado_r):
    operacion["estado_operacion"] = "CERRADA"
    operacion["fecha_cierre"] = ahora.isoformat()
    operacion["precio_salida"] = precio_salida
    operacion["resultado_final"] = resultado_final
    operacion["resultado_r"] = round(float(resultado_r), 3)


def calcular_r(operacion, precio_salida):
    riesgo = abs(operacion["entrada"] - operacion["stop_inicial"])
    if riesgo == 0:
        return 0.0
    if operacion["direccion"] == "LARGO":
        return (precio_salida - operacion["entrada"]) / riesgo
    return (operacion["entrada"] - precio_salida) / riesgo


def evaluar_operacion(operacion, resultado, ahora):
    eventos = []
    vela = resultado["ultima_vela"].isoformat()
    if vela <= operacion["ultima_vela_revisada"]:
        return eventos, False
    operacion["ultima_vela_revisada"] = vela
    operacion["ultimo_precio"] = resultado["precio"]
    maximo = resultado["maximo_ultima_vela"]
    minimo = resultado["minimo_ultima_vela"]

    if operacion["estado_operacion"] == "ESPERANDO_ENTRADA":
        creada = datetime.fromisoformat(operacion["creada"])
        minutos = (ahora - creada).total_seconds() / 60
        direccion_opuesta = (
            operacion["direccion"] == "LARGO" and resultado["estado"] == "POSIBLE CORTO"
        ) or (
            operacion["direccion"] == "CORTO" and resultado["estado"] == "POSIBLE LARGO"
        )
        if minutos >= CADUCIDAD_ENTRADA_MINUTOS or direccion_opuesta:
            motivo = "CANCELADA_POR_CAMBIO" if direccion_opuesta else "CANCELADA_POR_TIEMPO"
            cerrar_operacion(operacion, motivo, None, ahora, 0.0)
            eventos.append(
                "⚪ CONFIGURACION SIMULADA CANCELADA\n\n"
                "La entrada no se activo o el sesgo tecnico cambio."
            )
            return eventos, True
        if minimo <= operacion["entrada"] <= maximo:
            operacion["estado_operacion"] = "ACTIVA"
            operacion["fecha_entrada"] = ahora.isoformat()
            eventos.append(
                "🟡 ENTRADA SIMULADA ACTIVADA\n\n"
                f"Direccion: {operacion['direccion']}\n"
                f"Entrada: {operacion['entrada']:.2f}\n"
                f"Stop: {operacion['stop_actual']:.2f}\n"
                f"Objetivo 1: {operacion['objetivo_1']:.2f}\n"
                f"Objetivo 2: {operacion['objetivo_2']:.2f}"
            )
        return eventos, False

    if operacion["estado_operacion"] != "ACTIVA":
        return eventos, operacion["estado_operacion"] == "CERRADA"

    es_largo = operacion["direccion"] == "LARGO"
    toca_stop = minimo <= operacion["stop_actual"] if es_largo else maximo >= operacion["stop_actual"]
    toca_objetivo_2 = maximo >= operacion["objetivo_2"] if es_largo else minimo <= operacion["objetivo_2"]
    toca_objetivo_1 = maximo >= operacion["objetivo_1"] if es_largo else minimo <= operacion["objetivo_1"]

    # Si stop y objetivo aparecen en la misma vela, se aplica el resultado conservador.
    if toca_stop:
        if operacion["tp1_alcanzado"]:
            final, resultado_r = "BREAKEVEN_TRAS_OBJETIVO_1", 0.0
        else:
            final, resultado_r = "STOP", -1.0
        cerrar_operacion(operacion, final, operacion["stop_actual"], ahora, resultado_r)
        eventos.append(
            "🔴 OPERACION SIMULADA CERRADA\n\n"
            f"Resultado: {final}\nResultado R: {resultado_r:+.2f}R"
        )
        return eventos, True
    if toca_objetivo_2:
        resultado_r = calcular_r(operacion, operacion["objetivo_2"])
        cerrar_operacion(operacion, "OBJETIVO_2", operacion["objetivo_2"], ahora, resultado_r)
        eventos.append(
            "🟢 OBJETIVO 2 ALCANZADO\n\n"
            f"Operacion simulada finalizada: {resultado_r:+.2f}R"
        )
        return eventos, True
    if toca_objetivo_1 and not operacion["tp1_alcanzado"]:
        operacion["tp1_alcanzado"] = True
        operacion["stop_actual"] = operacion["entrada"]
        eventos.append(
            "✅ OBJETIVO 1 ALCANZADO\n\n"
            "El stop simulado se mueve al punto de entrada. "
            f"Nuevo stop: {operacion['stop_actual']:.2f}"
        )
    return eventos, False


def preparar_csv_resultados():
    if not ARCHIVO_SENALES.exists():
        return
    try:
        with ARCHIVO_SENALES.open("r", newline="", encoding="utf-8-sig") as archivo:
            lector = csv.DictReader(archivo)
            filas = list(lector)
            cabecera = lector.fieldnames or []
        if cabecera == CAMPOS_RESULTADO:
            return
        with ARCHIVO_SENALES.open("w", newline="", encoding="utf-8-sig") as archivo:
            escritor = csv.DictWriter(archivo, fieldnames=CAMPOS_RESULTADO)
            escritor.writeheader()
            for fila in filas:
                escritor.writerow({campo: fila.get(campo, "") for campo in CAMPOS_RESULTADO})
    except (OSError, csv.Error):
        return


def registrar_resultado(operacion):
    preparar_csv_resultados()
    nuevo = not ARCHIVO_SENALES.exists()
    with ARCHIVO_SENALES.open("a", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=CAMPOS_RESULTADO)
        if nuevo:
            escritor.writeheader()
        escritor.writerow({
            "fecha_hora_nueva_york": operacion["creada"],
            "estado": operacion["estado_senal"],
            "precio": f"{operacion['precio_senal']:.2f}",
            "entrada_baja": f"{operacion['entrada_baja']:.2f}",
            "entrada_alta": f"{operacion['entrada_alta']:.2f}",
            "stop": f"{operacion['stop_inicial']:.2f}",
            "objetivo_1": f"{operacion['objetivo_1']:.2f}",
            "objetivo_2": f"{operacion['objetivo_2']:.2f}",
            "rsi14": f"{operacion['rsi14']:.2f}",
            "atr14": f"{operacion['atr14']:.2f}",
            "id_operacion": operacion["id"],
            "direccion": operacion["direccion"],
            "fecha_entrada": operacion.get("fecha_entrada") or "",
            "fecha_cierre": operacion.get("fecha_cierre") or "",
            "entrada_ejecutada": f"{operacion['entrada']:.2f}",
            "precio_salida": "" if operacion.get("precio_salida") is None else f"{operacion['precio_salida']:.2f}",
            "resultado_final": operacion.get("resultado_final") or "",
            "resultado_r": f"{operacion.get('resultado_r', 0):.3f}",
            "tp1_alcanzado": "SI" if operacion.get("tp1_alcanzado") else "NO",
        })


def actualizar_resumen(estado, operacion):
    resumen = estado["resumen_sesion"]
    final = operacion.get("resultado_final", "")
    if final.startswith("CANCELADA"):
        resumen["canceladas"] += 1
    else:
        resumen["cerradas"] += 1
    if final == "STOP":
        resumen["stops"] += 1
    elif final == "OBJETIVO_2":
        resumen["objetivos_2"] += 1
    elif final == "BREAKEVEN_TRAS_OBJETIVO_1":
        resumen["breakeven"] += 1
    elif final == "CIERRE_SESION":
        resumen["cierres_sesion"] += 1
    resumen["r_total"] = round(
        float(resumen.get("r_total", 0)) + float(operacion.get("resultado_r", 0)), 3
    )


def cerrar_operacion_fin_sesion(estado, ahora):
    operacion = estado.get("operacion_abierta")
    if not operacion:
        return
    if operacion["estado_operacion"] == "ESPERANDO_ENTRADA":
        cerrar_operacion(operacion, "CANCELADA_FIN_SESION", None, ahora, 0.0)
    else:
        precio = operacion.get("ultimo_precio", operacion["entrada"])
        cerrar_operacion(operacion, "CIERRE_SESION", precio, ahora, calcular_r(operacion, precio))
    registrar_resultado(operacion)
    actualizar_resumen(estado, operacion)
    estado["operacion_abierta"] = None


def mensaje_resumen(estado):
    r = estado["resumen_sesion"]
    return (
        "📊 RESUMEN SIMULADO DE LA SESION\n\n"
        f"Operaciones cerradas: {r['cerradas']}\n"
        f"Objetivo 2: {r['objetivos_2']}\n"
        f"Stops: {r['stops']}\n"
        f"Breakeven tras objetivo 1: {r['breakeven']}\n"
        f"Cierres al finalizar sesion: {r['cierres_sesion']}\n"
        f"Configuraciones canceladas: {r['canceladas']}\n"
        f"Resultado total: {r['r_total']:+.2f}R\n\n"
        "Estadistica educativa; no representa operaciones reales."
    )


def ejecutar_prueba_manual():
    enviar_telegram(
        "🧪 NASDAQ SENTINEL — SEGUIMIENTO ACTIVO\n\n"
        "Conexion verificada. El sistema de una sola operacion simulada, "
        "control de objetivos y registro de resultados esta preparado."
    )
    try:
        resultado = analizar_mercado()
        enviar_telegram("🧪 PRUEBA DE MERCADO\n\n" + resultado["mensaje"])
        print("Analisis de mercado enviado correctamente.")
    except Exception as error:
        enviar_telegram(
            "ℹ️ PRUEBA DE MERCADO NO DISPONIBLE\n\n"
            f"Telegram funciona. Detalle: {type(error).__name__}: {error}"
        )
    noticias = obtener_noticias_macro(maximas=3)
    enviar_telegram(formatear_noticias_macro(noticias, "🧪 PRUEBA MACRO OFICIAL"))


def notificar_error_una_vez(estado, error):
    firma = f"{type(error).__name__}:{str(error)[:160]}"
    if estado.get("ultimo_error_notificado") == firma:
        return
    enviar_telegram(
        "⚠️ NASDAQ SENTINEL — INCIDENCIA\n\n"
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
            cerrar_operacion_fin_sesion(estado, ahora)
            enviar_telegram(mensaje_resumen(estado))
            enviar_telegram(
                "🔵 SESION DE NUEVA YORK FINALIZADA\n\n"
                "Nasdaq Sentinel queda en espera hasta la proxima sesion."
            )
            estado["sesion_abierta"] = False
            guardar_estado(estado)
            print("Cierre y resumen enviados.")
        else:
            print("Fuera de la sesion. No es necesario analizar.")
        return

    fecha_texto = ahora.date().isoformat()
    if not estado.get("sesion_abierta"):
        estado["resumen_sesion"] = resumen_vacio(fecha_texto)
        estado["ultimo_estado_notificado"] = None
        enviar_telegram(
            "🟢 SESION DE NUEVA YORK ABIERTA\n\n"
            "Vigilancia tecnica, macro y seguimiento simulado activados."
        )
        estado["sesion_abierta"] = True
        guardar_estado(estado)

    try:
        noticias, nuevas = noticias_macro_nuevas(estado, ahora)
        if nuevas:
            enviar_telegram(formatear_noticias_macro(nuevas, "🚨 NOVEDAD MACRO OFICIAL"))
        marcar_macro_vistas(estado, noticias)
        resultado = analizar_mercado()
        operacion = estado.get("operacion_abierta")
        if operacion:
            eventos, cerrada = evaluar_operacion(operacion, resultado, ahora)
            for evento in eventos:
                enviar_telegram(evento)
            if cerrada:
                registrar_resultado(operacion)
                actualizar_resumen(estado, operacion)
                estado["operacion_abierta"] = None

        estado_actual = resultado["estado"]
        ultimo = estado.get("ultimo_estado_notificado")
        segundos = tiempo_desde_ultimo_envio(estado, ahora)
        espera = segundos is None or segundos >= ESPERA_MINIMA_ALERTAS
        if estado_actual != ultimo and espera:
            enviar_telegram(resultado["mensaje"])
            if (
                resultado.get("plan")
                and not resultado.get("datos_atrasados")
                and estado.get("operacion_abierta") is None
            ):
                operacion = crear_operacion(resultado, ahora)
                estado["operacion_abierta"] = operacion
                enviar_telegram(mensaje_configuracion(operacion))
            estado["ultimo_estado_notificado"] = estado_actual
            estado["ultimo_envio"] = ahora.isoformat()
            print(f"Alerta enviada: {estado_actual}")
        else:
            print(f"Sin alerta tecnica nueva: {estado_actual}")
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
