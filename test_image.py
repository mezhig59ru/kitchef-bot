import os
import io
import urllib.request
import urllib.parse
from PIL import Image

# Подхватываем системный прокси (Hiddify)
system_proxies = urllib.request.getproxies()
if "https" in system_proxies and "HTTPS_PROXY" not in os.environ:
    os.environ["HTTPS_PROXY"] = system_proxies["https"]
if "http" in system_proxies and "HTTP_PROXY" not in os.environ:
    os.environ["HTTP_PROXY"] = system_proxies["http"]

def test_generate_food():
    prompt = (
        "Delicious homemade juicy meat cutlets with creamy mashed potatoes, "
        "garnished with fresh herbs, restaurant plating, commercial food photography, "
        "warm cozy lighting, appetizing, high resolution, 8k"
    )
    
    # Кодируем промпт для URL
    encoded_prompt = urllib.parse.quote(prompt)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"

    print("-> Отправка запроса на генерацию фото блюда в Pollinations AI (Flux)...")

    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        
        with urllib.request.urlopen(req, timeout=30) as response:
            image_bytes = response.read()

        image = Image.open(io.BytesIO(image_bytes))
        output_file = "test_food_result.jpg"
        image.save(output_file)
        print(f"-> Успех! Фото блюда сохранено в файл: {output_file}")
        return True

    except Exception as e:
        print(f"[!] Ошибка при генерации: {type(e).__name__} -> {e}")
        return False

if __name__ == "__main__":
    test_generate_food()