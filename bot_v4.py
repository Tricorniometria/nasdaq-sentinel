import csv
# NASDAQ SENTINEL ANALISIS - V7.1 SOBREEXTENSION 2026-09-03
import math
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as hora, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv


SIMBOLO = "NQ=F"
MODO = "SIMULACION"
INTERVALO_SEGUNDOS = 300
ESPERA_MINIMA_ALERTAS = 900
ZONA_NUEVA_YORK = ZoneInfo("America/New_York")
ARCHIVO_SENALES = Path("senales_simuladas.csv")
MAXIMO_TELEGRAM = 3900
EDAD_DATOS_VERDES_MINUTOS = 12
EDAD_DATOS_AMARILLOS_MINUTOS = 25
MAX_EDAD_FVG_VELAS = 12
MAX_DISTANCIA_FVG_ATR = 1.5
MAX_DISTANCIA_ENTRADA_ATR = 1.5
MAX_EXTENSION_EMA_ATR = 1.75
RSI_MAXIMO_NUEVO_LARGO = 72
RSI_MINIMO_NUEVO_CORTO = 28

FUENTES_MACRO = (
    ("Reserva Federal", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Discursos Fed", "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"),
    ("Empleo EEUU", "https://www.bls.gov/feed/empsit.rss"),
    ("IPC EEUU", "https://www.bls.gov/feed/cpi.rss"),
    ("IPP EEUU", "https://www.bls.gov/feed/ppi.rss"),
    ("BEA", "https://apps.bea.gov/rss/rss.xml"),
)

PALABRAS_ALTO_IMPACTO = (
    "federal funds",
    "fomc",
    "monetary policy",
    "interest rate",
    "powell",
    "consumer price",
    "producer price",
    "employment situation",
    "nonfarm",
    "unemployment",
    "gross domestic product",
    "gdp",
    "personal income and outlays",
    "personal consumption expenditures",
    "pce",
)


def _trocear_mensaje(mensaje, limite=MAXIMO_TELEGRAM):
    mensaje = str(mensaje).strip()
    if not mensaje:
        return []

    partes = []
    restante = mensaje
    while len(restante) > limite:
        corte = restante.rfind("\n", 0, limite + 1)
        if corte < limite // 2:
            corte = restante.rfind(" ", 0, limite + 1)
        if corte < limite // 2:
            corte = limite
        partes.append(restante[:corte].rstrip())
        restante = restante[corte:].lstrip()
    if restante:
        partes.append(restante)
    return partes


def obtener_chat_ids_telegram():
    """Devuelve, sin duplicados, los chats autorizados para alertas y consultas."""
    chat_ids = []
    for nombre_variable in ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_HIJO"):
        for valor in os.getenv(nombre_variable, "").split(","):
            chat_id = valor.strip()
            if chat_id and chat_id not in chat_ids:
                chat_ids.append(chat_id)
    return chat_ids


def enviar_telegram(mensaje):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = obtener_chat_ids_telegram()
    if not token or not chat_ids:
        raise ValueError("Faltan TELEGRAM_BOT_TOKEN o chats autorizados")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    entregas = 0
    errores = []
    for chat_id in chat_ids:
        entrega_chat_completa = True
        for parte in _trocear_mensaje(mensaje):
            ultimo_error = None
            for intento in range(3):
                try:
                    respuesta = requests.post(
                        url,
                        data={"chat_id": chat_id, "text": parte},
                        timeout=20,
                    )
                    if respuesta.ok:
                        ultimo_error = None
                        break

                    try:
                        detalle = respuesta.json().get("description", respuesta.text)
                    except ValueError:
                        detalle = respuesta.text
                    ultimo_error = RuntimeError(
                        f"Telegram {respuesta.status_code}: {detalle}"
                    )

                    if respuesta.status_code == 429 or respuesta.status_code >= 500:
                        time.sleep(2 ** intento)
                        continue
                    break
                except requests.RequestException as error:
                    ultimo_error = error
                    if intento < 2:
                        time.sleep(2 ** intento)
                        continue
                    break
            if ultimo_error:
                entrega_chat_completa = False
                errores.append(f"chat {chat_id}: {ultimo_error}")
                break
        if entrega_chat_completa:
            entregas += 1

    if entregas == 0 and errores:
        raise RuntimeError("; ".join(errores))
    for error in errores:
        print(f"Aviso de entrega de Telegram: {error}")


def sesion_nueva_york_abierta(momento):
    return momento.weekday() < 5 and hora(9, 30) <= momento.time() < hora(16, 0)


def calcular_rsi(cierres, periodo=14):
    cambios = cierres.diff()
    ganancias = cambios.clip(lower=0).rolling(periodo).mean()
    perdidas = (-cambios.clip(upper=0)).rolling(periodo).mean()
    fuerza = ganancias / perdidas.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + fuerza))
    rsi = rsi.mask((perdidas == 0) & (ganancias > 0), 100)
    rsi = rsi.mask((ganancias == 0) & (perdidas > 0), 0)
    return rsi.mask((ganancias == 0) & (perdidas == 0), 50)


def descargar_datos():
    datos = yf.download(
        SIMBOLO,
        period="5d",
        interval="5m",
        progress=False,
        auto_adjust=False,
    )
    if datos.empty:
        raise ValueError("Yahoo Finance no ha devuelto datos")
    if isinstance(datos.columns, pd.MultiIndex):
        datos.columns = datos.columns.get_level_values(0)
    datos = datos[["Open", "High", "Low", "Close", "Volume"]].copy()
    if datos.index.tz is None:
        datos.index = datos.index.tz_localize("UTC")
    datos.index = datos.index.tz_convert(ZONA_NUEVA_YORK)
    datos["EMA20"] = datos["Close"].ewm(span=20, adjust=False).mean()
    datos["EMA50"] = datos["Close"].ewm(span=50, adjust=False).mean()
    datos["RSI14"] = calcular_rsi(datos["Close"])
    cierre_anterior = datos["Close"].shift(1)
    rangos = pd.concat(
        [
            datos["High"] - datos["Low"],
            (datos["High"] - cierre_anterior).abs(),
            (datos["Low"] - cierre_anterior).abs(),
        ],
        axis=1,
    )
    datos["ATR14"] = rangos.max(axis=1).rolling(14).mean()
    datos = datos.dropna()
    if datos.empty:
        raise ValueError("No hay suficientes velas para calcular indicadores")
    return datos


def detectar_fvg(datos, fecha_actual=None):
    tramo = datos
    if fecha_actual is not None:
        mascara = datos.index.date == fecha_actual
        if mascara.any():
            tramo = datos.loc[mascara]
    tramo = tramo.tail(100)
    fvg_alcista = None
    fvg_bajista = None
    for posicion in range(2, len(tramo)):
        primera = tramo.iloc[posicion - 2]
        tercera = tramo.iloc[posicion]
        posteriores = tramo.iloc[posicion + 1 :]
        if float(tercera["Low"]) > float(primera["High"]):
            inferior = float(primera["High"])
            superior = float(tercera["Low"])
            rellenado = (
                not posteriores.empty
                and float(posteriores["Low"].min()) <= inferior
            )
            edad_velas = len(tramo) - 1 - posicion
            if not rellenado and edad_velas <= MAX_EDAD_FVG_VELAS:
                fvg_alcista = (inferior, superior)
        if float(tercera["High"]) < float(primera["Low"]):
            inferior = float(tercera["High"])
            superior = float(primera["Low"])
            rellenado = (
                not posteriores.empty
                and float(posteriores["High"].max()) >= superior
            )
            edad_velas = len(tramo) - 1 - posicion
            if not rellenado and edad_velas <= MAX_EDAD_FVG_VELAS:
                fvg_bajista = (inferior, superior)
    return fvg_alcista, fvg_bajista


def niveles_de_sesion(datos, momento):
    fecha_actual = momento.date()
    horas = datos.index.time
    fechas = datos.index.date
    mascara_actual = (
        (fechas == fecha_actual)
        & (horas >= hora(9, 30))
        & (horas < hora(16, 0))
    )
    sesion = datos.loc[mascara_actual]
    if sesion.empty:
        raise ValueError("Todavia no hay velas de la sesion de Nueva York")

    maximo = float(sesion["High"].max())
    minimo = float(sesion["Low"].min())
    amplitud = maximo - minimo
    equilibrio = (maximo + minimo) / 2

    mascara_apertura = (
        (fechas == fecha_actual)
        & (horas >= hora(9, 30))
        & (horas < hora(10, 0))
    )
    rango_apertura = datos.loc[mascara_apertura]
    if rango_apertura.empty:
        maximo_apertura, minimo_apertura = maximo, minimo
    else:
        maximo_apertura = float(rango_apertura["High"].max())
        minimo_apertura = float(rango_apertura["Low"].min())

    maximo_anterior = None
    minimo_anterior = None
    dias_anteriores = sorted({fecha for fecha in fechas if fecha < fecha_actual})
    if dias_anteriores:
        dia_anterior = dias_anteriores[-1]
        mascara_anterior = (
            (fechas == dia_anterior)
            & (horas >= hora(9, 30))
            & (horas < hora(16, 0))
        )
        anterior = datos.loc[mascara_anterior]
        if not anterior.empty:
            maximo_anterior = float(anterior["High"].max())
            minimo_anterior = float(anterior["Low"].min())

    return {
        "maximo_sesion": maximo,
        "minimo_sesion": minimo,
        "equilibrio": equilibrio,
        "zona_descuento": minimo + amplitud * 0.25,
        "zona_premium": minimo + amplitud * 0.75,
        "maximo_apertura": maximo_apertura,
        "minimo_apertura": minimo_apertura,
        "maximo_anterior": maximo_anterior,
        "minimo_anterior": minimo_anterior,
    }


def formatear_fvg(nombre, zona):
    if zona is None:
        return f"{nombre}: ninguno activo"
    return f"{nombre}: {zona[0]:.2f} - {zona[1]:.2f}"


def crear_plan(direccion, ema20, atr, fvg):
    if fvg is not None:
        entrada_baja, entrada_alta = fvg
    else:
        entrada_baja = ema20 - atr * 0.25
        entrada_alta = ema20 + atr * 0.25
    entrada_media = (entrada_baja + entrada_alta) / 2
    if direccion == "LARGO":
        stop = entrada_baja - atr * 0.75
        riesgo = entrada_media - stop
        objetivo_1 = entrada_media + riesgo
        objetivo_2 = entrada_media + riesgo * 2
    else:
        stop = entrada_alta + atr * 0.75
        riesgo = stop - entrada_media
        objetivo_1 = entrada_media - riesgo
        objetivo_2 = entrada_media - riesgo * 2
    return {
        "direccion": direccion,
        "entrada_baja": entrada_baja,
        "entrada_alta": entrada_alta,
        "stop": stop,
        "objetivo_1": objetivo_1,
        "objetivo_2": objetivo_2,
    }


def seleccionar_fvg_cercano(fvg, precio, atr):
    """Solo permite FVG recientes que sigan razonablemente cerca del precio."""
    if fvg is None or not math.isfinite(atr) or atr <= 0:
        return None
    centro = sum(fvg) / 2
    if abs(precio - centro) > atr * MAX_DISTANCIA_FVG_ATR:
        return None
    return fvg


def validar_plan(plan, precio, atr):
    """Impide publicar planes agotados, alejados o matematicamente incoherentes."""
    if plan is None or not math.isfinite(atr) or atr <= 0:
        return False, "No se pudo calcular un riesgo valido."

    valores = (
        precio,
        plan["entrada_baja"],
        plan["entrada_alta"],
        plan["stop"],
        plan["objetivo_1"],
        plan["objetivo_2"],
    )
    if not all(math.isfinite(valor) for valor in valores):
        return False, "El plan contiene valores no validos."

    entrada_baja = plan["entrada_baja"]
    entrada_alta = plan["entrada_alta"]
    if entrada_baja >= entrada_alta:
        return False, "La zona de entrada no es valida."

    if precio > entrada_alta:
        distancia = precio - entrada_alta
    elif precio < entrada_baja:
        distancia = entrada_baja - precio
    else:
        distancia = 0.0
    if distancia > atr * MAX_DISTANCIA_ENTRADA_ATR:
        return False, "La entrada esta demasiado alejada del precio actual."

    if plan["direccion"] == "LARGO":
        estructura = (
            plan["stop"] < entrada_baja < entrada_alta
            < plan["objetivo_1"] < plan["objetivo_2"]
        )
        if not estructura:
            return False, "El orden de entrada, stop y objetivos no es coherente."
        if precio >= plan["objetivo_1"]:
            return False, "El movimiento alcista ya ha superado el primer objetivo."
        if precio < entrada_baja:
            return False, "El precio ya ha perdido la zona de entrada alcista."
    else:
        estructura = (
            plan["objetivo_2"] < plan["objetivo_1"]
            < entrada_baja < entrada_alta < plan["stop"]
        )
        if not estructura:
            return False, "El orden de entrada, stop y objetivos no es coherente."
        if precio <= plan["objetivo_1"]:
            return False, "El movimiento bajista ya ha superado el primer objetivo."
        if precio > entrada_alta:
            return False, "El precio ya ha superado la zona de entrada bajista."

    return True, "Plan validado."


def validar_contexto_entrada(direccion, precio, ema20, rsi, atr):
    """Bloquea entradas contra extremos de RSI o movimientos sobreextendidos."""
    if not all(math.isfinite(valor) for valor in (precio, ema20, rsi, atr)):
        return False, "No se pudo validar el contexto de mercado."
    if atr <= 0:
        return False, "El ATR no permite medir el riesgo."

    if direccion == "LARGO":
        if rsi > RSI_MAXIMO_NUEVO_LARGO:
            return False, "RSI en sobrecompra; no se persigue el movimiento alcista."
        if precio - ema20 > atr * MAX_EXTENSION_EMA_ATR:
            return False, "Precio demasiado extendido por encima de EMA20."
    else:
        if rsi < RSI_MINIMO_NUEVO_CORTO:
            return False, "RSI en sobreventa; no se persigue el movimiento bajista."
        if ema20 - precio > atr * MAX_EXTENSION_EMA_ATR:
            return False, "Precio demasiado extendido por debajo de EMA20."

    return True, "Contexto validado."


def evaluar_calidad_datos(momento, ultima_vela):
    antiguedad = max(0.0, (momento - ultima_vela).total_seconds() / 60)
    if not sesion_nueva_york_abierta(momento):
        return {
            "codigo": "FUERA_SESION",
            "semaforo": "🔵",
            "etiqueta": "MERCADO CERRADO",
            "antiguedad_minutos": round(antiguedad, 1),
            "operativa_permitida": False,
            "detalle": "Fuera del horario regular de Nueva York.",
        }
    if antiguedad <= EDAD_DATOS_VERDES_MINUTOS:
        return {
            "codigo": "VERDE",
            "semaforo": "🟢",
            "etiqueta": "DATOS RECIENTES",
            "antiguedad_minutos": round(antiguedad, 1),
            "operativa_permitida": True,
            "detalle": "Lectura apta para analisis simulado.",
        }
    if antiguedad <= EDAD_DATOS_AMARILLOS_MINUTOS:
        return {
            "codigo": "AMARILLO",
            "semaforo": "🟡",
            "etiqueta": "DATOS CON RETRASO",
            "antiguedad_minutos": round(antiguedad, 1),
            "operativa_permitida": False,
            "detalle": "Solo observacion; se bloquean nuevas configuraciones.",
        }
    return {
        "codigo": "ROJO",
        "semaforo": "🔴",
        "etiqueta": "DATOS NO FIABLES",
        "antiguedad_minutos": round(antiguedad, 1),
        "operativa_permitida": False,
        "detalle": "No operar; se esperan datos mas recientes.",
    }


def analizar_mercado():
    datos = descargar_datos()
    momento = datetime.now(ZONA_NUEVA_YORK)
    niveles = niveles_de_sesion(datos, momento)
    fvg_alcista, fvg_bajista = detectar_fvg(datos, momento.date())
    ultima = datos.iloc[-1]
    precio = float(ultima["Close"])
    ema20 = float(ultima["EMA20"])
    ema50 = float(ultima["EMA50"])
    rsi = float(ultima["RSI14"])
    atr = float(ultima["ATR14"])
    ultima_vela = datos.index[-1].to_pydatetime()
    calidad_datos = evaluar_calidad_datos(momento, ultima_vela)
    antiguedad_minutos = calidad_datos["antiguedad_minutos"]
    datos_atrasados = not calidad_datos["operativa_permitida"]

    puntos_largo = 0
    puntos_corto = 0
    if precio > ema20 > ema50:
        puntos_largo += 2
    if 45 <= rsi <= 68:
        puntos_largo += 1
    if precio > niveles["equilibrio"]:
        puntos_largo += 1
    if precio > niveles["maximo_apertura"]:
        puntos_largo += 1
    if fvg_alcista and abs(precio - sum(fvg_alcista) / 2) <= atr * 2:
        puntos_largo += 1

    if precio < ema20 < ema50:
        puntos_corto += 2
    if 32 <= rsi <= 55:
        puntos_corto += 1
    if precio < niveles["equilibrio"]:
        puntos_corto += 1
    if precio < niveles["minimo_apertura"]:
        puntos_corto += 1
    if fvg_bajista and abs(precio - sum(fvg_bajista) / 2) <= atr * 2:
        puntos_corto += 1

    plan = None
    motivo_plan = "Faltan confirmaciones tecnicas."
    if calidad_datos["codigo"] == "AMARILLO":
        estado = "DATOS EN OBSERVACION"
    elif calidad_datos["codigo"] == "ROJO":
        estado = "DATOS NO FIABLES"
    elif datos_atrasados:
        estado = "MERCADO CERRADO"
    elif puntos_largo >= 5 and puntos_largo > puntos_corto:
        valido, motivo_plan = validar_contexto_entrada(
            "LARGO", precio, ema20, rsi, atr
        )
        if valido:
            fvg_plan = seleccionar_fvg_cercano(fvg_alcista, precio, atr)
            candidato = crear_plan("LARGO", ema20, atr, fvg_plan)
            valido, motivo_plan = validar_plan(candidato, precio, atr)
        if valido:
            estado = "POSIBLE LARGO"
            plan = candidato
        else:
            estado = "SESGO ALCISTA - ESPERAR"
    elif puntos_corto >= 5 and puntos_corto > puntos_largo:
        valido, motivo_plan = validar_contexto_entrada(
            "CORTO", precio, ema20, rsi, atr
        )
        if valido:
            fvg_plan = seleccionar_fvg_cercano(fvg_bajista, precio, atr)
            candidato = crear_plan("CORTO", ema20, atr, fvg_plan)
            valido, motivo_plan = validar_plan(candidato, precio, atr)
        if valido:
            estado = "POSIBLE CORTO"
            plan = candidato
        else:
            estado = "SESGO BAJISTA - ESPERAR"
    elif puntos_largo >= 3 and puntos_largo > puntos_corto:
        estado = "VIGILAR ALCISTA"
    elif puntos_corto >= 3 and puntos_corto > puntos_largo:
        estado = "VIGILAR BAJISTA"
    else:
        estado = "SIN CONFIGURACION"

    if niveles["maximo_anterior"] is not None:
        texto_anterior = (
            f"Dia anterior: {niveles['minimo_anterior']:.2f} - "
            f"{niveles['maximo_anterior']:.2f}"
        )
    else:
        texto_anterior = "Dia anterior: no disponible"

    if plan:
        texto_plan = (
            f"\nPLAN SIMULADO {plan['direccion']}\n"
            f"Entrada: {plan['entrada_baja']:.2f} - {plan['entrada_alta']:.2f}\n"
            f"Stop: {plan['stop']:.2f}\n"
            f"Objetivo 1: {plan['objetivo_1']:.2f}\n"
            f"Objetivo 2: {plan['objetivo_2']:.2f}\n"
        )
    else:
        texto_plan = f"\nSin entrada simulada: {motivo_plan}\n"

    aviso_datos = (
        f"\n{calidad_datos['semaforo']} Calidad: {calidad_datos['etiqueta']} "
        f"({antiguedad_minutos:.1f} min)\n"
    )
    if datos_atrasados:
        aviso_datos += "⚠️ Nuevas configuraciones bloqueadas por seguridad.\n"

    mensaje = (
        "🧭 NASDAQ SENTINEL V4\n\n"
        f"Estado: {estado}\n"
        f"Precio NQ: {precio:.2f}\n"
        f"EMA20 / EMA50: {ema20:.2f} / {ema50:.2f}\n"
        f"RSI14: {rsi:.2f} | ATR14: {atr:.2f}\n"
        f"Puntuacion largo/corto: {puntos_largo}/{puntos_corto}\n\n"
        f"Sesion NY: {niveles['minimo_sesion']:.2f} - {niveles['maximo_sesion']:.2f}\n"
        f"Balance 25%-75%: {niveles['zona_descuento']:.2f} - "
        f"{niveles['zona_premium']:.2f}\n"
        f"Equilibrio: {niveles['equilibrio']:.2f}\n"
        f"Rango apertura: {niveles['minimo_apertura']:.2f} - "
        f"{niveles['maximo_apertura']:.2f}\n"
        f"{texto_anterior}\n\n"
        f"{formatear_fvg('FVG alcista', fvg_alcista)}\n"
        f"{formatear_fvg('FVG bajista', fvg_bajista)}\n"
        f"{texto_plan}{aviso_datos}\n"
        f"Ultima vela NY: {ultima_vela:%d/%m/%Y %H:%M}\n"
        f"Hora Nueva York: {momento:%d/%m/%Y %H:%M}\n"
        "⚠️ Simulacion educativa. No ejecuta ordenes."
    )
    return {
        "momento": momento,
        "ultima_vela": ultima_vela,
        "maximo_ultima_vela": float(ultima["High"]),
        "minimo_ultima_vela": float(ultima["Low"]),
        "estado": estado,
        "precio": precio,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "atr": atr,
        "puntos_largo": puntos_largo,
        "puntos_corto": puntos_corto,
        "niveles": niveles,
        "fvg_alcista": fvg_alcista,
        "fvg_bajista": fvg_bajista,
        "plan": plan,
        "motivo_plan": motivo_plan,
        "calidad_datos": calidad_datos,
        "antiguedad_minutos": antiguedad_minutos,
        "datos_atrasados": datos_atrasados,
        "mensaje": mensaje,
    }


def _texto_elemento(elemento, nombres):
    for hijo in elemento.iter():
        nombre = hijo.tag.split("}")[-1].lower()
        if nombre in nombres and hijo.text:
            return hijo.text.strip()
    return ""


def _fecha_macro(texto):
    if not texto:
        return None
    try:
        fecha = parsedate_to_datetime(texto)
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        return fecha.astimezone(ZONA_NUEVA_YORK)
    except (TypeError, ValueError, OverflowError):
        try:
            fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            return fecha.astimezone(ZONA_NUEVA_YORK)
        except ValueError:
            return None


def _impacto_alto(titulo):
    titulo = titulo.lower()
    return any(palabra in titulo for palabra in PALABRAS_ALTO_IMPACTO)


def obtener_noticias_macro(maximas=6):
    noticias = []
    cabeceras = {"User-Agent": "NasdaqSentinel/4.0 educational-monitor"}
    for fuente, url in FUENTES_MACRO:
        try:
            respuesta = requests.get(url, headers=cabeceras, timeout=15)
            respuesta.raise_for_status()
            raiz = ET.fromstring(respuesta.content)
            elementos = [
                nodo for nodo in raiz.iter()
                if nodo.tag.split("}")[-1].lower() in {"item", "entry"}
            ]
            for elemento in elementos[:4]:
                titulo = _texto_elemento(elemento, {"title"})
                if not titulo or not _impacto_alto(titulo):
                    continue
                enlace = _texto_elemento(elemento, {"link"})
                if not enlace:
                    for hijo in elemento.iter():
                        if hijo.tag.split("}")[-1].lower() == "link":
                            enlace = hijo.attrib.get("href", "")
                            if enlace:
                                break
                fecha_texto = _texto_elemento(
                    elemento, {"pubdate", "published", "updated", "date"}
                )
                fecha = _fecha_macro(fecha_texto)
                identificador = _texto_elemento(elemento, {"guid", "id"})
                identificador = identificador or enlace or f"{fuente}:{titulo}"
                noticias.append(
                    {
                        "id": identificador,
                        "fuente": fuente,
                        "titulo": " ".join(titulo.split()),
                        "enlace": enlace,
                        "fecha": fecha,
                    }
                )
        except (requests.RequestException, ET.ParseError):
            continue

    noticias_unicas = {}
    for noticia in noticias:
        noticias_unicas[noticia["id"]] = noticia
    ordenadas = sorted(
        noticias_unicas.values(),
        key=lambda item: item["fecha"] or datetime.min.replace(tzinfo=ZONA_NUEVA_YORK),
        reverse=True,
    )
    return ordenadas[:maximas]


def formatear_noticias_macro(noticias, titulo="📰 MACRO EEUU"):
    if not noticias:
        return f"{titulo}\n\nSin novedades oficiales de alto impacto."
    lineas = [titulo, ""]
    for noticia in noticias:
        fecha = noticia["fecha"]
        hora_texto = fecha.strftime("%d/%m %H:%M NY") if fecha else "Fecha no indicada"
        lineas.append(f"• {noticia['fuente']} — {hora_texto}")
        lineas.append(noticia["titulo"])
        if noticia["enlace"]:
            lineas.append(noticia["enlace"])
        lineas.append("")
    lineas.append("Fuente oficial. Confirmar el horario antes de operar.")
    return "\n".join(lineas).strip()


def registrar_senal(resultado):
    plan = resultado["plan"]
    if plan is None or resultado.get("datos_atrasados"):
        return
    nuevo_archivo = not ARCHIVO_SENALES.exists()
    campos = [
        "fecha_hora_nueva_york",
        "estado",
        "precio",
        "entrada_baja",
        "entrada_alta",
        "stop",
        "objetivo_1",
        "objetivo_2",
        "rsi14",
        "atr14",
    ]
    with ARCHIVO_SENALES.open("a", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=campos)
        if nuevo_archivo:
            escritor.writeheader()
        escritor.writerow(
            {
                "fecha_hora_nueva_york": resultado["momento"].isoformat(),
                "estado": resultado["estado"],
                "precio": f"{resultado['precio']:.2f}",
                "entrada_baja": f"{plan['entrada_baja']:.2f}",
                "entrada_alta": f"{plan['entrada_alta']:.2f}",
                "stop": f"{plan['stop']:.2f}",
                "objetivo_1": f"{plan['objetivo_1']:.2f}",
                "objetivo_2": f"{plan['objetivo_2']:.2f}",
                "rsi14": f"{resultado['rsi']:.2f}",
                "atr14": f"{resultado['atr']:.2f}",
            }
        )


def main():
    load_dotenv()
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        raise ValueError("Faltan las credenciales de Telegram en .env")
    print("Nasdaq Sentinel V4 iniciado. Pulsa Ctrl + C para detenerlo.")
    enviar_telegram("🟢 NASDAQ SENTINEL V4 CONECTADO\n\nModo simulado activado.")
    estado_anterior = None
    sesion_anterior = False
    ultimo_envio = None
    try:
        while True:
            ahora = datetime.now(ZONA_NUEVA_YORK)
            abierta = sesion_nueva_york_abierta(ahora)
            if abierta:
                try:
                    resultado = analizar_mercado()
                    estado = resultado["estado"]
                    espera = (
                        ultimo_envio is None
                        or (ahora - ultimo_envio).total_seconds() >= ESPERA_MINIMA_ALERTAS
                    )
                    if estado != estado_anterior and espera:
                        enviar_telegram(resultado["mensaje"])
                        registrar_senal(resultado)
                        ultimo_envio = ahora
                    estado_anterior = estado
                    print(f"{ahora:%H:%M:%S} | {estado}")
                except Exception as error:
                    print(f"Error temporal: {error}")
            elif sesion_anterior:
                enviar_telegram("🔵 SESION DE NUEVA YORK FINALIZADA\n\nV4 queda en espera.")
                estado_anterior = None
            sesion_anterior = abierta
            time.sleep(INTERVALO_SEGUNDOS if abierta else 60)
    except KeyboardInterrupt:
        print("Nasdaq Sentinel V4 detenido correctamente.")


if __name__ == "__main__":
    main()
