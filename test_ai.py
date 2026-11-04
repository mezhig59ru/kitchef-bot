import os
import time
import urllib.request
from dotenv import load_dotenv
from google import genai

# Подтягиваем прокси из Windows
system_proxies = urllib.request.getproxies()
if "https" in system_proxies:
    os.environ["HTTPS_PROXY"] = system_proxies["https"]

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print("1. Получаем список доступных моделей от Google...")
candidates = []
try:
    for m in client.models.list():
        # Отбираем модели Flash (они самые быстрые)
        if "flash" in m.name.lower():
            candidates.append(m.name)
    print(f"Найдено моделей Flash: {len(candidates)}")
except Exception as e:
    print(f"Не удалось получить список: {e}")
    candidates = ["gemini-2.5-flash", "gemini-1.5-flash"]

print("\n2. Тестируем, какая модель свободна и ответит быстрее всех:")
for model_name in candidates:
    print(f"-> Проверяем: {model_name}...")
    t0 = time.time()
    try:
        response = client.models.generate_content(
            model=model_name,
            contents="Напиши коротко в одно предложение: Шеф готов к работе?"
        )
        dt = round(time.time() - t0, 2)
        print(f"\n[УСПЕХ!] Модель ответила за {dt} сек.:")
        print(f"Ответ: {response.text.strip()}")
        print(f"\nИспользуем для бота имя: '{model_name}'")
        break
    except Exception as err:
        dt = round(time.time() - t0, 2)
        print(f"   Занята/сбой ({dt} сек): {type(err).__name__}")