import requests
 
API_KEY = "b95462dbb49e7b7a155d635ecadf3494"
TARGET_URL = "https://www.adidas.co.in/men-black-shoes?sort=price-low-to-high&v_size_en_in=7.5"

payload = {
    "api_key": API_KEY,
    "url": TARGET_URL,
    "render": "true",  # Executes JavaScript and sensor checks
    "output_format": "markdown"
}

response = requests.get("http://api.scraperapi.com/", params=payload)

print(f"Status code: {response.status_code}")
markdown_data = response.text
print(markdown_data[:500])