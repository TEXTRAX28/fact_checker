"""Test Gemma 4 12B locally"""

print("Testing Gemma 4 12B...")
print("(This will download ~27GB model on first run — coffee time!)\n")

try:
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import torch

    model_id = "google/gemma-4-12b-it"
    print(f"Loading {model_id}...")
    print(f"GPU available: {torch.cuda.is_available()}")

    # Load with quantization for 6GB GPU
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        device_map="auto"
    )

    print("[OK] Model loaded successfully!")

    # Test a simple query
    messages = [
        {"role": "user", "content": "Say hello"}
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer.encode(text, return_tensors="pt").to("cuda")

    print("\nRunning test inference...")
    outputs = model.generate(inputs, max_new_tokens=100)
    response = tokenizer.decode(outputs[0])

    print(f"[OK] Test response:\n{response}\n")
    print("[OK] Gemma 4 12B is ready to use!")

except ImportError as e:
    print(f"[ERROR] Missing library: {e}")
    print("Run: pip install transformers torch")
except Exception as e:
    print(f"[ERROR] {type(e).__name__}: {e}")
    print("\nTroubleshooting:")
    print("- Make sure you have 6GB+ VRAM available")
    print("- Check CUDA installation: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
    print("- First download takes ~5-10 min and 26GB disk space")
