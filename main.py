import os
import json
import random
import time
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from supabase import create_client, Client
from bs4 import BeautifulSoup
import paho.mqtt.client as mqtt
import re

load_dotenv()

#ITOP DATA
USERNAME = os.getenv("ITOP_USER")
PASSWORD = os.getenv("ITOP_PASSWORD")
LOGIN_URL = os.getenv("ITOP_URL")
DATA_URL = os.getenv("ITOP_DATA_URL")

#FILTERS
GRUPOS = os.getenv("ITOP_GRUPOS", "Centros de Control,Trading").split(",")

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

        #Esperar a que carguen las tablas del dashboard (iTop 3: DataTables con data-status="loaded")
        esperar_tablas(driver)
        return True
    except Exception as e:
        print(f"Error durante el login: {e}")
        return False

def esperar_tablas(driver, timeout=30):
    #En iTop 3 las listas son DataTables que se rellenan por JavaScript. Cuando terminan de pintarse,
    #iTop marca la tabla con data-status="loaded".
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "table.ibo-datatable[data-status='loaded']"))
    )

def esta_logueado(driver):
    try:
        formulario = driver.find_elements(By.NAME, "auth_user")
        if len(formulario) > 0:
            return False
        return True
    except:
        return False

def buscar_columna(headers, nombre, defecto):
    nombre = nombre.lower()
    return next((i for i, h in enumerate(headers) if nombre in h), defecto)

def texto_celda(fila, cols, etiqueta, idx):
    #iTop 3 etiqueta cada celda con data-attribute-label (p.ej. "Title", "Priority", "TTR Deadline").
    #Si no existe, se usa la posición detectada a partir de la cabecera.
    celda = fila.find("td", attrs={"data-attribute-label": etiqueta})
    if celda is None and idx < len(cols):
        celda = cols[idx]
    return celda.get_text(strip=True) if celda is not None else ""

def scrapear_con_selenium(driver):
    try:
        print("Scrapeando datos de iTop...")
        try:
            esperar_tablas(driver)
        except Exception:
            print("Aviso: las tablas no llegaron a marcarse como cargadas, se intenta igualmente.")

        soup = BeautifulSoup(driver.page_source, 'html.parser')

        #Buscar el bloque exacto (iTop 3: panel de DataTable con el título en .ibo-panel--title)
        bloque_objetivo = None
        for panel in soup.find_all("div", class_="ibo-datatable-panel"):
            header = panel.find(class_="ibo-panel--title")
            if not header:
                continue
            titulo_bloque = header.get_text(" ", strip=True).lower()
            #Buscamos "assigned to my team" sin importar mayúsculas/minúsculas, excluyendo los bloques filtrados por los grupos de ITOP_GRUPOS (como "Centros de Control" o "Trading").
            if "assigned to my team" in titulo_bloque and not any(group.strip().lower() in titulo_bloque for group in GRUPOS if group.strip()):
                bloque_objetivo = panel
                break

        if not bloque_objetivo:
            print("No se encontró el bloque de tickets asignados (assigned to my team).")
            return None

        #Con scroll activado DataTables separa la tabla en dos: una con la cabecera visible y otra con el cuerpo.
        tablas = bloque_objetivo.find_all("table", class_="ibo-datatable")
        headers = []
        for t in tablas:
            ths = [th.get_text(strip=True).lower() for th in t.find_all("th")]
            if any(ths):
                headers = ths
                break

        #Mapeo dinámico de columnas (por si alguna celda no trae data-attribute-label)
        idx_titulo = buscar_columna(headers, "Title", 1)
        idx_prioridad = buscar_columna(headers, "Priority", 7)
        idx_ttr = buscar_columna(headers, "TTR Deadline", 8)
        print(f"Detectadas columnas: Título[{idx_titulo}], Prioridad[{idx_prioridad}], TTR[{idx_ttr}]")

        filas = []
        for t in tablas:
            tbody = t.find("tbody")
            if tbody:
                filas.extend(tbody.find_all("tr"))
        #Fila "No data available" (td único con clase dataTables_empty) cuando no hay tickets
        filas = [f for f in filas if f.find("td") and not f.find("td", class_="dataTables_empty")]

        print(f"Total de filas encontradas: {len(filas)}")

        #Comprobación contra el contador "Total: N objects." del panel (por si la tabla estuviera paginada)
        contador = bloque_objetivo.find("span", class_="ibo-datatable--result-count")
        if contador and contador.get_text(strip=True).isdigit() and int(contador.get_text(strip=True)) != len(filas):
            print(f"Aviso: el panel indica {contador.get_text(strip=True)} tickets pero se han leído {len(filas)} filas.")

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

            try:
                titulo = texto_celda(fila, cols, "Title", idx_titulo)
                prioridad = texto_celda(fila, cols, "Priority", idx_prioridad)
                ttr_texto = texto_celda(fila, cols, "TTR Deadline", idx_ttr)

                stats["total"] += 1

                #Clasificación
                prioridad = prioridad.strip().capitalize()
                if prioridad in stats["prioridad"]:
                    stats["prioridad"][prioridad] += 1

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
        print(f"Error scrapeando: {e}")
        return None

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
    driver = iniciar_driver_linux()

    try:
        login(driver)
        while True:
            if not esta_logueado(driver):
                print(f"Sesión expirada. Reintentando login...")
                login(driver)

            stats = scrapear_con_selenium(driver)
            print(f"Estadísticas actuales: {stats}")

            if stats is not None:
                guardar_en_supabase(stats)
                publicar_mqtt(stats)
            else:
                #Si falla el scraping, reiniciamos el navegador y volvemos a loguear
                print("No se han podido obtener datos. Reiniciando el navegador...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(5)
                driver = iniciar_driver_linux()
                login(driver)
            
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