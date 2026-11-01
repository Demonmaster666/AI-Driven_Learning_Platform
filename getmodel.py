import google.generativeai as genai

# Replace with your actual API key
API_KEY = "AIzaSyDGmSoJJBzaNwUpY_K_F2Qpz3pLOxVZff4"

# Configure the API
genai.configure(api_key=API_KEY)

# List all available models
models = genai.list_models()

print("Available Gemini Models:\n")
for model in models:
    # Only show text or multimodal models
    if "generateContent" in model.supported_generation_methods:
        print(f"- {model.name}")