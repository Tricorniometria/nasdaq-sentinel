import csv
import os
import time
from datetime import datetime, time as hora
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


def enviar_telegram(mensaje):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise ValueError("Faltan los datos de Telegram en el archivo .env")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    respuesta = requests.post(
        url,
        data={"chat_id": chat_id, "text": mensaje},
        timeout=20,
    )    if not respuesta.ok:
        print("ERROR EXACTO DE TELEGRAM:", respuesta.status_code, respuesta.text)
    respuesta.raise_for_status()


def sesion_nueva_york_abierta(momento):
    if momento.weekday() >= 5:
        return False

    return hora(9, 30) <= momento.time() < hora(16, 0)


def calcular_rsi(cierres, periodo=14):
    cambios = cierres.diff()
    ganancias = cambios.clip(lower=0).rolling(periodo).mean()
    perdidas = (-cambios.clip(upper=0)).rolling(periodo).mean()

    fuerza = ganancias / perdidas.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + fuerza))

    rsi = rsi.mask((perdidas == 0) & (ganancias > 0), 100)
    rsi = rsi.mask((ganancias == 0) & (perdidas > 0), 0)

    return rsi.mask(
        (ganancias == 0) & (perdidas == 0),
        50,
    )


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

    datos = datos[
        ["Open", "High", "Low", "Close", "Volume"]
    ].copy()

    if datos.index.tz is None:
        datos.index = datos.index.tz_localize("UTC")

    datos.index = datos.index.tz_convert(
        ZONA_NUEVA_YORK
    )

    datos["EMA20"] = (
        datos["Close"]
        .ewm(span=20, adjust=False)
        .mean()
    )

    datos["EMA50"] = (
        datos["Close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    datos["RSI14"] = calcular_rsi(
        datos["Close"]
    )

    cierre_anterior = datos["Close"].shift(1)

    rangos = pd.concat(
        [
            datos["High"] - datos["Low"],
            (
                datos["High"] - cierre_anterior
            ).abs(),
            (
                datos["Low"] - cierre_anterior
            ).abs(),
        ],
        axis=1,
    )

    datos["ATR14"] = (
        rangos.max(axis=1)
        .rolling(14)
        .mean()
    )

    return datos.dropna()


def detectar_fvg(datos):
    tramo = datos.tail(80)

    fvg_alcista = None
    fvg_bajista = None

    for posicion in range(2, len(tramo)):
        primera = tramo.iloc[posicion - 2]
        tercera = tramo.iloc[posicion]
        posteriores = tramo.iloc[posicion + 1:]

        if (
            float(tercera["Low"])
            > float(primera["High"])
        ):
            limite_inferior = float(
                primera["High"]
            )
            limite_superior = float(
                tercera["Low"]
            )

            rellenado = (
                not posteriores.empty
                and float(
                    posteriores["Low"].min()
                ) <= limite_inferior
            )

            if not rellenado:
                fvg_alcista = (
                    limite_inferior,
                    limite_superior,
                )

        if (
            float(tercera["High"])
            < float(primera["Low"])
        ):
            limite_inferior = float(
                tercera["High"]
            )
            limite_superior = float(
                primera["Low"]
            )

            rellenado = (
                not posteriores.empty
                and float(
                    posteriores["High"].max()
                ) >= limite_superior
            )

            if not rellenado:
                fvg_bajista = (
                    limite_inferior,
                    limite_superior,
                )

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

    sesion_actual = datos.loc[
        mascara_actual
    ]

    if sesion_actual.empty:
        raise ValueError(
            "Todavia no hay velas de la sesion "
            "de Nueva York"
        )

    maximo_sesion = float(
        sesion_actual["High"].max()
    )

    minimo_sesion = float(
        sesion_actual["Low"].min()
    )

    equilibrio = (
        maximo_sesion + minimo_sesion
    ) / 2

    mascara_apertura = (
        (fechas == fecha_actual)
        & (horas >= hora(9, 30))
        & (horas < hora(10, 0))
    )

    rango_apertura = datos.loc[
        mascara_apertura
    ]

    maximo_apertura = float(
        rango_apertura["High"].max()
    )

    minimo_apertura = float(
        rango_apertura["Low"].min()
    )

    dias_anteriores = sorted(
        {
            fecha
            for fecha in fechas
            if fecha < fecha_actual
        }
    )

    maximo_anterior = None
    minimo_anterior = None

    if dias_anteriores:
        dia_anterior = dias_anteriores[-1]

        mascara_anterior = (
            (fechas == dia_anterior)
            & (horas >= hora(9, 30))
            & (horas < hora(16, 0))
        )

        sesion_anterior = datos.loc[
            mascara_anterior
        ]

        if not sesion_anterior.empty:
            maximo_anterior = float(
                sesion_anterior["High"].max()
            )

            minimo_anterior = float(
                sesion_anterior["Low"].min()
            )

    return {
        "maximo_sesion": maximo_sesion,
        "minimo_sesion": minimo_sesion,
        "equilibrio": equilibrio,
        "maximo_apertura": maximo_apertura,
        "minimo_apertura": minimo_apertura,
        "maximo_anterior": maximo_anterior,
        "minimo_anterior": minimo_anterior,
    }


def formatear_fvg(nombre, zona):
    if zona is None:
        return (
            f"{nombre}: ninguno activo"
        )

    return (
        f"{nombre}: "
        f"{zona[0]:.2f} - {zona[1]:.2f}"
    )


def crear_plan(
    direccion,
    ema20,
    atr,
    fvg,
):
    if direccion == "LARGO":
        if fvg is not None:
            entrada_baja = fvg[0]
            entrada_alta = fvg[1]
        else:
            entrada_baja = (
                ema20 - atr * 0.25
            )
            entrada_alta = (
                ema20 + atr * 0.25
            )

        entrada_media = (
            entrada_baja + entrada_alta
        ) / 2

        stop = (
            entrada_baja - atr * 0.75
        )

        riesgo = entrada_media - stop

        objetivo_1 = (
            entrada_media + riesgo
        )

        objetivo_2 = (
            entrada_media + riesgo * 2
        )

    else:
        if fvg is not None:
            entrada_baja = fvg[0]
            entrada_alta = fvg[1]
        else:
            entrada_baja = (
                ema20 - atr * 0.25
            )
            entrada_alta = (
                ema20 + atr * 0.25
            )

        entrada_media = (
            entrada_baja + entrada_alta
        ) / 2

        stop = (
            entrada_alta + atr * 0.75
        )

        riesgo = stop - entrada_media

        objetivo_1 = (
            entrada_media - riesgo
        )

        objetivo_2 = (
            entrada_media - riesgo * 2
        )

    return {
        "direccion": direccion,
        "entrada_baja": entrada_baja,
        "entrada_alta": entrada_alta,
        "stop": stop,
        "objetivo_1": objetivo_1,
        "objetivo_2": objetivo_2,
    }


def analizar_mercado():
    datos = descargar_datos()

    momento = datetime.now(
        ZONA_NUEVA_YORK
    )

    niveles = niveles_de_sesion(
        datos,
        momento,
    )

    fvg_alcista, fvg_bajista = (
        detectar_fvg(datos)
    )

    ultima = datos.iloc[-1]

    precio = float(ultima["Close"])
    ema20 = float(ultima["EMA20"])
    ema50 = float(ultima["EMA50"])
    rsi = float(ultima["RSI14"])
    atr = float(ultima["ATR14"])

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

    if (
        fvg_alcista
        and abs(
            precio
            - sum(fvg_alcista) / 2
        ) <= atr * 2
    ):
        puntos_largo += 1

    if precio < ema20 < ema50:
        puntos_corto += 2

    if 32 <= rsi <= 55:
        puntos_corto += 1

    if precio < niveles["equilibrio"]:
        puntos_corto += 1

    if precio < niveles["minimo_apertura"]:
        puntos_corto += 1

    if (
        fvg_bajista
        and abs(
            precio
            - sum(fvg_bajista) / 2
        ) <= atr * 2
    ):
        puntos_corto += 1

    plan = None

    if (
        puntos_largo >= 5
        and puntos_largo > puntos_corto
    ):
        estado = "POSIBLE LARGO"

        plan = crear_plan(
            "LARGO",
            ema20,
            atr,
            fvg_alcista,
        )

    elif (
        puntos_corto >= 5
        and puntos_corto > puntos_largo
    ):
        estado = "POSIBLE CORTO"

        plan = crear_plan(
            "CORTO",
            ema20,
            atr,
            fvg_bajista,
        )

    elif (
        puntos_largo >= 3
        and puntos_largo > puntos_corto
    ):
        estado = "VIGILAR ALCISTA"

    elif (
        puntos_corto >= 3
        and puntos_corto > puntos_largo
    ):
        estado = "VIGILAR BAJISTA"

    else:
        estado = "SIN CONFIGURACION"

    anterior_alto = niveles[
        "maximo_anterior"
    ]

    anterior_bajo = niveles[
        "minimo_anterior"
    ]

    if (
        anterior_alto is not None
        and anterior_bajo is not None
    ):
        texto_anterior = (
            f"Dia anterior: "
            f"{anterior_bajo:.2f} - "
            f"{anterior_alto:.2f}"
        )
    else:
        texto_anterior = (
            "Dia anterior: no disponible"
        )

    if plan:
        texto_plan = (
            f"\nPLAN SIMULADO "
            f"{plan['direccion']}\n"
            f"Entrada: "
            f"{plan['entrada_baja']:.2f} - "
            f"{plan['entrada_alta']:.2f}\n"
            f"Stop: "
            f"{plan['stop']:.2f}\n"
            f"Objetivo 1: "
            f"{plan['objetivo_1']:.2f}\n"
            f"Objetivo 2: "
            f"{plan['objetivo_2']:.2f}\n"
        )
    else:
        texto_plan = (
            "\nSin entrada simulada: "
            "faltan confirmaciones.\n"
        )

    mensaje = (
        "🧭 NASDAQ SENTINEL V3\n\n"
        f"Estado: {estado}\n"
        f"Precio NQ: {precio:.2f}\n"
        f"EMA20 / EMA50: "
        f"{ema20:.2f} / {ema50:.2f}\n"
        f"RSI14: {rsi:.2f} | "
        f"ATR14: {atr:.2f}\n"
        f"Puntuacion largo/corto: "
        f"{puntos_largo}/{puntos_corto}\n\n"
        f"Sesion NY: "
        f"{niveles['minimo_sesion']:.2f} - "
        f"{niveles['maximo_sesion']:.2f}\n"
        f"Equilibrio: "
        f"{niveles['equilibrio']:.2f}\n"
        f"Rango apertura: "
        f"{niveles['minimo_apertura']:.2f} - "
        f"{niveles['maximo_apertura']:.2f}\n"
        f"{texto_anterior}\n\n"
        f"{formatear_fvg(
            'FVG alcista',
            fvg_alcista
        )}\n"
        f"{formatear_fvg(
            'FVG bajista',
            fvg_bajista
        )}\n"
        f"{texto_plan}\n"
        f"Hora Nueva York: "
        f"{momento:%d/%m/%Y %H:%M}\n"
        "⚠️ Simulacion educativa. "
        "Requiere confirmacion y "
        "no ejecuta ordenes."
    )

    return {
        "momento": momento,
        "estado": estado,
        "precio": precio,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "atr": atr,
        "puntos_largo": puntos_largo,
        "puntos_corto": puntos_corto,
        "plan": plan,
        "mensaje": mensaje,
    }


def registrar_senal(resultado):
    plan = resultado["plan"]

    if plan is None:
        return

    nuevo_archivo = (
        not ARCHIVO_SENALES.exists()
    )

    with ARCHIVO_SENALES.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as archivo:

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

        escritor = csv.DictWriter(
            archivo,
            fieldnames=campos,
        )

        if nuevo_archivo:
            escritor.writeheader()

        escritor.writerow(
            {
                "fecha_hora_nueva_york":
                    resultado[
                        "momento"
                    ].isoformat(),

                "estado":
                    resultado["estado"],

                "precio":
                    f"{resultado[
                        'precio'
                    ]:.2f}",

                "entrada_baja":
                    f"{plan[
                        'entrada_baja'
                    ]:.2f}",

                "entrada_alta":
                    f"{plan[
                        'entrada_alta'
                    ]:.2f}",

                "stop":
                    f"{plan[
                        'stop'
                    ]:.2f}",

                "objetivo_1":
                    f"{plan[
                        'objetivo_1'
                    ]:.2f}",

                "objetivo_2":
                    f"{plan[
                        'objetivo_2'
                    ]:.2f}",

                "rsi14":
                    f"{resultado[
                        'rsi'
                    ]:.2f}",

                "atr14":
                    f"{resultado[
                        'atr'
                    ]:.2f}",
            }
        )


def main():
    load_dotenv()

    if (
        not os.getenv(
            "TELEGRAM_BOT_TOKEN"
        )
        or not os.getenv(
            "TELEGRAM_CHAT_ID"
        )
    ):
        raise ValueError(
            "Faltan las credenciales "
            "de Telegram en .env"
        )

    print(
        "Nasdaq Sentinel V3 iniciado."
    )

    print(
        "Sesion de Nueva York: "
        "09:30-16:00."
    )

    print(
        "Para detenerlo, "
        "pulsa Ctrl + C.\n"
    )

    enviar_telegram(
        "🟢 NASDAQ SENTINEL V3 "
        "CONECTADO\n\n"
        "Vigilancia tecnica y registro "
        "simulado activados."
    )

    estado_anterior = None
    sesion_anterior = False
    ultimo_envio = None

    try:
        while True:
            ahora = datetime.now(
                ZONA_NUEVA_YORK
            )

            sesion_abierta = (
                sesion_nueva_york_abierta(
                    ahora
                )
            )

            if sesion_abierta:
                if not sesion_anterior:
                    print(
                        "Sesion de Nueva York "
                        "abierta."
                    )

                    estado_anterior = None

                try:
                    resultado = (
                        analizar_mercado()
                    )

                    estado = resultado[
                        "estado"
                    ]

                    print(
                        f"{ahora:%H:%M:%S} | "
                        f"{estado}"
                    )

                    estado_cambiado = (
                        estado
                        != estado_anterior
                    )

                    espera_cumplida = (
                        ultimo_envio is None
                        or (
                            ahora
                            - ultimo_envio
                        ).total_seconds()
                        >= ESPERA_MINIMA_ALERTAS
                    )

                    if (
                        estado_cambiado
                        and espera_cumplida
                    ):
                        enviar_telegram(
                            resultado["mensaje"]
                        )

                        registrar_senal(
                            resultado
                        )

                        ultimo_envio = ahora

                        print(
                            "Analisis enviado "
                            "a Telegram."
                        )

                    else:
                        print(
                            "Sin alerta nueva: "
                            "no hay cambio "
                            "confirmado."
                        )

                    estado_anterior = estado

                except Exception as error:
                    print(
                        "Error temporal: "
                        f"{error}"
                    )

            else:
                if sesion_anterior:
                    enviar_telegram(
                        "🔵 SESION DE NUEVA YORK "
                        "FINALIZADA\n\n"
                        "Sentinel V3 queda "
                        "en espera."
                    )

                    print(
                        "Sesion finalizada. "
                        "Bot en espera."
                    )

                    estado_anterior = None

                print(
                    f"{ahora:%d/%m/%Y %H:%M:%S} | "
                    "Fuera de la sesion."
                )

            sesion_anterior = (
                sesion_abierta
            )

            if sesion_abierta:
                espera = (
                    INTERVALO_SEGUNDOS
                )
            else:
                espera = 60

            time.sleep(espera)

    except KeyboardInterrupt:
        print(
            "\nNasdaq Sentinel V3 "
            "detenido correctamente."
        )


if __name__ == "__main__":
    main()
    
