"""Test Gemma 4 E4B locally"""

print("Testing Gemma 4 E4B...")

try:
    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    import torch

    model_id = "google/gemma-4-E4B"
    print(f"Loading {model_id}...")
    print(f"GPU available: {torch.cuda.is_available()}")

    # 4-bit quantization for efficient loading
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16
    )

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
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
    print("[OK] Gemma 4 E4B is ready to use!")

except ImportError as e:
    print(f"[ERROR] Missing library: {e}")
    print("Run: pip install transformers torch bitsandbytes")
except Exception as e:
    print(f"[ERROR] {type(e).__name__}: {e}")
    print("\nTroubleshooting:")
    print("- Make sure you have 6GB+ VRAM available")
    print("- Check CUDA installation: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
    print("- First download takes ~2-3 min and 4-5GB disk space")
