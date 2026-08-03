import logging
import re

logger = logging.getLogger(__name__)

def _claim_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _deduplicate_claims(claims: list[dict]) -> list[dict]:
    """Merge only exact or near-identical lexical restatements; avoid semantic guesses."""
    unique: list[dict] = []
    original_indices: list[list[int]] = []
    for original_index, claim in enumerate(claims):
        text = re.sub(r"\s+", " ", str(claim.get("claim", ""))).strip()
        tokens = _claim_tokens(text)
        numbers = set(re.findall(r"\d+(?:[.,]\d+)*", text))
        duplicate_index = None
        for index, existing in enumerate(unique):
            existing_text = str(existing.get("claim", ""))
            existing_tokens = _claim_tokens(existing_text)
            union = tokens | existing_tokens
            overlap = len(tokens & existing_tokens) / len(union) if union else 0.0
            exact = text.casefold() == existing_text.casefold()
            same_numbers = numbers == set(re.findall(r"\d+(?:[.,]\d+)*", existing_text))
            if exact or (same_numbers and overlap >= 0.88):
                duplicate_index = index
                break
        if duplicate_index is None:
            unique.append(dict(claim))
            original_indices.append([original_index])
            continue

        original_indices[duplicate_index].append(original_index)
        existing = unique[duplicate_index]
        existing["merged_from"] = list(original_indices[duplicate_index])
        existing["original_claims"] = [
            str(claims[index].get("claim", ""))
            for index in original_indices[duplicate_index]
        ]
        logger.info("Merged duplicate extracted claim %d into %d",
                    original_index, original_indices[duplicate_index][0])
    return unique

