from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
import torch

model_id = "google/gemma-4-E4B"

try:
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

    messages = [{"role": "user", "content": "Say hello"}]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer.encode(text, return_tensors="pt").to("cuda")
    outputs = model.generate(inputs, max_new_tokens=100)
    response = tokenizer.decode(outputs[0])

    print(f"[OK] {response}")

except Exception as e:
    print(f"[ERROR] {type(e).__name__}: {e}")
