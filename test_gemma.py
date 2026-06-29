"""Test Gemma 2 12B locally"""

print("Testing Gemma 2 12B...")
print("(This will download ~26GB model on first run — coffee time!)\n")

try:
    from transformers import AutoTokenizer, AutoModelForCausalLM
    import torch

    model_id = "google/gemma-2-12b-it"
    print(f"Loading {model_id}...")
    print(f"GPU available: {torch.cuda.is_available()}")

    # Load with quantization for 6GB GPU
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16,
        device_map="auto"
    )

    print("✅ Model loaded successfully!")

    # Test a simple query
    messages = [
        {"role": "user", "content": "Say hello"}
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer.encode(text, return_tensors="pt").to("cuda")

    print("\nRunning test inference...")
    outputs = model.generate(inputs, max_new_tokens=100)
    response = tokenizer.decode(outputs[0])

    print(f"✅ Test response:\n{response}\n")
    print("✅ Gemma 2 12B is ready to use!")

except ImportError as e:
    print(f"❌ Missing library: {e}")
    print("Run: pip install transformers torch")
except Exception as e:
    print(f"❌ Error: {type(e).__name__}: {e}")
    print("\nTroubleshooting:")
    print("- Make sure you have 6GB+ VRAM available")
    print("- Check CUDA installation: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
    print("- First download takes ~5-10 min and 26GB disk space")
