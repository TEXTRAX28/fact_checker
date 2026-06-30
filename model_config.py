# Model configuration - switch between API and local
import os

# Set this to "deepinfra" or "gemma" to choose which model to use
# Default: "deepinfra" (API)
ACTIVE_MODEL = os.getenv("ACTIVE_MODEL", "deepinfra").lower()

print(f"[CONFIG] Using model: {ACTIVE_MODEL.upper()}")

if ACTIVE_MODEL == "deepinfra":
    MODEL_TYPE = "deepinfra"
    MODEL_NAME = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    print(f"[CONFIG] DeepInfra API - {MODEL_NAME}")

elif ACTIVE_MODEL == "gemma":
    MODEL_TYPE = "local"
    MODEL_NAME = "google/gemma-4-E4B"
    print(f"[CONFIG] Local Gemma 4 E4B - 4-bit quantized (optimized for 16GB RAM)")

else:
    raise ValueError(f"Unknown model: {ACTIVE_MODEL}. Use 'deepinfra' or 'gemma'")
