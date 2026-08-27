import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from bot_v3 import (
    ZONA_NUEVA_YORK,
    analizar_mercado,
    enviar_telegram,
    registrar_senal,
    sesion_nueva_york_abierta,
)


ARCHIVO_ESTADO = Path("estado_cloud.json")
ESPERA_MINIMA_ALERTAS = 900


def cargar_estado():
    if not ARCHIVO_ESTADO.exists():
        return {
            "sesion_abierta": False,
            "ultimo_estado_notificado": None,
            "ultimo_envio": None,
        }

    try:
        return json.loads(
            ARCHIVO_ESTADO.read_text(
                encoding="utf-8"
            )
        )
    except (json.JSONDecodeError, OSError):
        return {
            "sesion_abierta": False,
            "ultimo_estado_notificado": None,
            "ultimo_envio": None,
        }


def guardar_estado(estado):
    ARCHIVO_ESTADO.write_text(
        json.dumps(
            estado,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def tiempo_desde_ultimo_envio(
    estado,
    ahora,
):
    texto = estado.get("ultimo_envio")

    if not texto:
        return None

    try:
        ultimo = datetime.fromisoformat(
            texto
        )

        return (
            ahora - ultimo
        ).total_seconds()

    except (ValueError, TypeError):
        return None


def ejecutar_prueba_manual():
    try:
        resultado = analizar_mercado()

        enviar_telegram(
            "🧪 PRUEBA MANUAL DESDE GITHUB\n\n"
            + resultado["mensaje"]
        )

        print(
            "Prueba manual enviada "
            "correctamente a Telegram."
        )

    except Exception as error:
        print(
            "La prueba no pudo completar "
            f"el analisis: {error}"
        )

        enviar_telegram(
            "🧪 NASDAQ SENTINEL EN LA NUBE\n\n"
            "GitHub ha ejecutado correctamente "
            "el programa, pero los datos de mercado "
            "no estaban disponibles.\n\n"
            f"Detalle: {error}"
        )


def ejecutar_programacion():
    ahora = datetime.now(
        ZONA_NUEVA_YORK
    )

    estado = cargar_estado()

    sesion_abierta = (
        sesion_nueva_york_abierta(
            ahora
        )
    )

    if not sesion_abierta:
        if estado.get("sesion_abierta"):
            enviar_telegram(
                "🔵 SESION DE NUEVA YORK "
                "FINALIZADA\n\n"
                "Nasdaq Sentinel queda en espera "
                "hasta la proxima sesion."
            )

            estado["sesion_abierta"] = False
            guardar_estado(estado)

            print(
                "Aviso de cierre enviado."
            )

        else:
            print(
                "Fuera de la sesion. "
                "No es necesario analizar."
            )

        return

    if not estado.get("sesion_abierta"):
        enviar_telegram(
            "🟢 SESION DE NUEVA YORK ABIERTA\n\n"
            "Nasdaq Sentinel inicia la "
            "vigilancia en la nube."
        )

        estado["sesion_abierta"] = True
        guardar_estado(estado)

    resultado = analizar_mercado()
    estado_actual = resultado["estado"]

    ultimo_notificado = estado.get(
        "ultimo_estado_notificado"
    )

    segundos = (
        tiempo_desde_ultimo_envio(
            estado,
            ahora,
        )
    )

    espera_cumplida = (
        segundos is None
        or segundos
        >= ESPERA_MINIMA_ALERTAS
    )

    if (
        estado_actual
        != ultimo_notificado
        and espera_cumplida
    ):
        enviar_telegram(
            resultado["mensaje"]
        )

        registrar_senal(resultado)

        estado[
            "ultimo_estado_notificado"
        ] = estado_actual

        estado["ultimo_envio"] = (
            ahora.isoformat()
        )

        guardar_estado(estado)

        print(
            f"Alerta enviada: {estado_actual}"
        )

    else:
        print(
            "Sin alerta nueva: "
            f"{estado_actual}"
        )


def main():
    load_dotenv()

    if not os.getenv(
        "TELEGRAM_BOT_TOKEN"
    ):
        raise ValueError(
            "Falta TELEGRAM_BOT_TOKEN"
        )

    if not os.getenv(
        "TELEGRAM_CHAT_ID"
    ):
        raise ValueError(
            "Falta TELEGRAM_CHAT_ID"
        )

    if (
        os.getenv("GITHUB_EVENT_NAME")
        == "workflow_dispatch"
    ):
        ejecutar_prueba_manual()
    else:
        ejecutar_programacion()


if __name__ == "__main__":
    main()
