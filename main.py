import os
import json
import random
import time
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from supabase import create_client, Client
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
import re

load_dotenv()

#ITOP DATA
USERNAME = os.getenv("ITOP_USER")
PASSWORD = os.getenv("ITOP_PASSWORD")
LOGIN_URL = os.getenv("ITOP_URL")
DATA_URL = os.getenv("ITOP_DATA_URL")

#SQL DATA
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

#MQTT DATA
MQTT_BROKER = os.getenv("MQTT_BROKER")
MQTT_PORT = int(os.getenv("MQTT_PORT"))
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC")

INTERVAL = int(os.getenv("INTERVAL", 300))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
random.seed()

def publicar_mqtt(datos):
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mensaje_json = json.dumps(datos)
        client.publish(MQTT_BASE_TOPIC, mensaje_json)
        print(f"Datos publicados en MQTT: {MQTT_BROKER} -> {MQTT_BASE_TOPIC}")
        client.disconnect()
        return True
    except Exception as e:
        print(f"❌ Error MQTT: {e}")
        return False


def guardar_en_supabase(datos):
    try:
        # Preparamos el objeto para que coincida con las columnas de la tabla SQL
        payload = {
            "total_tickets": datos["total"],
            "count_critical": datos["prioridad"]["Critical"],
            "count_high": datos["prioridad"]["High"],
            "count_medium": datos["prioridad"]["Medium"],
            "count_low": datos["prioridad"]["Low"],
            "min_ttr_minutes": datos["ttr_minimo_minutos"],
            "count_ttr_past": datos["ttr_pasado"],
            "zabbix_tickets": datos["tickets_zabbix"]
        }

        # Ejecutamos el insert
        data, count = supabase.table("metricas_itop").insert(payload).execute()
        print("Datos subidos a Supabase correctamente.")
        return True
    except Exception as e:
        print(f"Error subiendo a Supabase: {e}")
        return False

def iniciar_driver_linux():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

def iniciar_driver():
    options = webdriver.ChromeOptions()
    return webdriver.Chrome(options=options)

def login(driver):
    try:
        driver.get(LOGIN_URL)
        time.sleep(2)

        #Login
        driver.find_element(By.NAME, "auth_user").send_keys(os.getenv("ITOP_USER"))
        driver.find_element(By.NAME, "auth_pwd").send_keys(os.getenv("ITOP_PASSWORD"))
        driver.find_element(By.NAME, "auth_pwd").submit()

        #Esperar a que cargue la tabla
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, "listResults"))
        )
    except Exception as e:
        print(f"Error durante el login: {e}")
        driver.quit()

def esta_logueado(driver):
    try:
        formulario = driver.find_elements(By.NAME, "auth_user")
        if len(formulario) > 0:
            return False
        return True
    except:
        return False

def scrapear_con_selenium(driver):
    try:
        print("Scrapeando datos de iTop...")
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        #Buscar el bloque exacto
        bloque_objetivo = None
        for dashlet in soup.find_all("div", class_="dashlet"):
            header = dashlet.find("h1")
            #Usamos "assigned to my team" porque es el texto que aparece en la cabecera del bloque, pero sin importar mayúsculas/minúsculas. Además, nos aseguramos de no confundirlo con el bloque de "Centros de Control" que también tiene esa frase pero no es el que queremos.
            if header and "assigned to my team" in header.get_text().lower() and "Centros de Control" not in header.get_text():
                bloque_objetivo = dashlet
                break
        
        if not bloque_objetivo:
            return "No se encontró el bloque (Dashlet) de tickets asignados."

        #Mapeo Dinámico de Columnas
        tabla = bloque_objetivo.find("table", class_="listResults")
        headers = [th.get_text(strip=True).lower() for th in tabla.find("thead").find_all("th")]
        
        #Buscamos en qué posición está cada dato
        idx_titulo = next((i for i, h in enumerate(headers) if "Title" in h), 1) 
        idx_prioridad = next((i for i, h in enumerate(headers) if "Priority" in h), 7)
        idx_ttr = next((i for i, h in enumerate(headers) if "TTR Deadline" in h), 8)

        print(f"Detectadas columnas: Título[{idx_titulo}], Prioridad[{idx_prioridad}], TTR[{idx_ttr}]")

        print(f"Total de filas encontradas: {len(tabla.find('tbody').find_all('tr'))}")

        #Procesar filas
        filas = tabla.find("tbody").find_all("tr")
        
        stats = {
            "total": 0,
            "prioridad": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0},
            "ttr_minimo_minutos": None,
            "tickets_zabbix": 0,
            "ttr_pasado": 0
        }
        
        ttr_values = []

        for fila in filas:
            cols = fila.find_all("td")
            

            #Si la fila tiene menos columnas de las necesarias, la saltamos
            if not cols or len(cols) <= max(idx_titulo, idx_prioridad, idx_ttr):
                continue

            try:
                #Extraemos datos usando los índices detectados
                titulo = cols[idx_titulo].get_text(strip=True)
                prioridad = cols[idx_prioridad].get_text(strip=True)
                ttr_texto = cols[idx_ttr].get_text(strip=True)

                stats["total"] += 1

                #Clasificación (Limpiamos espacios por si acaso)
                prioridad = prioridad.strip()
                if prioridad in stats["prioridad"]:
                    stats["prioridad"][prioridad] += 1
                elif prioridad == "":
                    pass 

                #Zabbix
                if "zabbix" in titulo.lower():
                    stats["tickets_zabbix"] += 1

                #TTR
                if ttr_texto:
                    minutos = parsear_tiempo_itop(ttr_texto)
                    if minutos > 0:
                        ttr_values.append(minutos)

                    #Contar tickets con TTR pasado
                    if minutos < 0:
                        stats["ttr_pasado"] += 1
            
            except Exception as e:
                print(f"Fila ignorada por error inesperado: {e}")
                continue

        #TTR mínimo
        if ttr_values:
            stats["ttr_minimo_minutos"] = min(ttr_values)

        return stats

    except Exception as e:
        print(f"Error: {e}")
        driver.quit()
        time.sleep(5)
        driver = iniciar_driver()
        login(driver)

def parsear_tiempo_itop(texto):
    if not texto: return 0
    
    minutos = 0
    
    dias = re.search(r'(\d+)\s*d', texto)
    horas = re.search(r'(\d+)\s*h', texto)
    mins = re.search(r'(\d+)\s*min', texto)
    
    if dias: minutos += int(dias.group(1)) * 1440
    if horas: minutos += int(horas.group(1)) * 60
    if mins: minutos += int(mins.group(1))
    
    if "missed" in texto.lower():
        minutos = minutos * -1
        
    return minutos

def ejecutar_servicio():
    driver = iniciar_driver()

    try:
        login(driver)
        while True:
            if not esta_logueado(driver):
                print(f"Sesión expirada. Reintentando login...")
                login(driver)

            stats = scrapear_con_selenium(driver)
            print(f"Estadísticas actuales: {stats}")

            guardar_en_supabase(stats)
            publicar_mqtt(stats)
            
            #Suma al intervalo un random entre 0 y 60.
            time.sleep(INTERVAL + random.randint(0, 60))
            driver.refresh()
            time.sleep(30)
    except Exception as e:
        print(f"Error inesperado en el servicio: {e}")
        driver.quit()
    except KeyboardInterrupt:
        print("\n Deteniendo servicio...")
    finally:
        driver.quit()
        print("Servicio detenido.")



if __name__ == "__main__":
    ejecutar_servicio()