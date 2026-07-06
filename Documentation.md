# Wikipedia Over-Representation, Work in Progress

## The Problem

When fact-checking claims, the system searches for evidence using Tavily, which returns only up to 6 results per claim. These results are then filtered to remove social/UGC domains (Facebook, YouTube, Reddit, etc.), keeping the top 3 "real" sources.

**The issue:** Wikipedia tends to rank first in Tavily results. When it does, the current pipeline doesn't deduplicate sources, it just takes whatever comes back. This means a single Wikipedia article can appear multiple times in the sources list, filling up all 3 source slots with variants of the same link.

### Real Example: Before and After

**Before (untuned code):** Searching for "wiki" in verdict output returns **12 matches** Wikipedia appears throughout the sources lists across verdicts.

**After (VERIFY_PROMPT tuned):** Searching for "wikipedia" in verdict output returns only **2 matches** Wikipedia instances dropped from 12 to 2 just by changing the prompt instruction.

---

## What Changed

### 1. Expanded Search Coverage

Increased Tavily's result fetch from 6 to 10 per claim. A broader initial pool provides better opportunities to surface authoritative sources before filtering, reducing reliance on any single dominant result (e.g., Wikipedia).

### 2. Streamlined Verdict Scale

Simplified the verdict taxonomy from 6 categories to 3 tiers:
- **6-tier (old):** TRUE, MOSTLY TRUE, PARTLY TRUE, MISLEADING, UNVERIFIABLE, FALSE
- **3-tier (new):** TRUE, UNVERIFIABLE, FALSE

This creates sharper, more decisive verdicts while reducing ambiguity in borderline cases.

### 3. Refined Source Prioritization in VERIFY_PROMPT

Enhanced the model's instructions to intelligently balance source diversity. Rather than mechanically listing whatever sources appear first in search results, the model now:
- Prioritizes authoritative and specialized sources (government, academic, established news)
- Treats Wikipedia as a supplementary source, not a primary reference
- Considers source credibility when selecting which results to cite

This subtle but powerful prompt adjustment leverages the model's reasoning capability to make better source choices, reducing Wikipedia's dominance from 12 instances to just 2, without any code modification.

**Insight:** Prompt engineering can achieve what algorithmic filtering often requires, by aligning the model's behavior with desired outcomes through clearer instructions.
---

## Results

- Wikipedia instances reduced from 12 to 2
- Verdict scale simplified to 3 tiers (more decisive)
- Tavily fetch increased to 10 results (better source diversity to choose from) 
- The result is better and more direct with the 3-tier verdict and after the prompt tuning
- You can see in the picture below (No wikipedia, before tuning the prompt, after tuning the prompt) 


![](wikipedia.png)

---

## Real-World Test: BBC Nadiem Makarim Article

Tested the updated system against a real BBC news article about Indonesian Gojek founder Nadiem Makarim's corruption conviction (2026-06-30). 

### Verdict Distribution (3-Tier Scale)
- **TRUE:** 6 claims
- **UNVERIFIABLE:** 4 claims  
- **FALSE:** 1 claim

### Key Observations

### **Sharper verdicts from 3-tier scale (Major):** 
The compressed scale forces clearer reasoning. For example:
- "Chromebook procurement from 2021-2022" correctly marked **FALSE** (actually 2020-2021), not hedged with PARTLY TRUE
- Claims with insufficient recent evidence correctly marked **UNVERIFIABLE** instead of MOSTLY TRUE, preventing false confidence

**Better source selection:** Wikipedia appears only once (in sources for claim about Nadiem's timeline), and is flagged with a note `[Note: Wikipedia, community-edited]`. Most verdicts cite Reuters, BBC, CNBC, and official sources.

**Confidence calibration improved:** 
- High-confidence verdicts (95-100%) backed by multiple independent sources (Reuters, CNBC, Straits Times)
- 60% confidence claims properly marked UNVERIFIABLE where sources lack direct evidence (e.g., "He had pleaded not guilty" - articles mention defense plea but don't confirm plea content)

### Sample Claims

| Claim | Verdict | Confidence | Notes |
|-------|---------|-----------|-------|
| Nadiem sentenced to 10 years in prison | TRUE | 100% | 3 independent sources (Reuters, CNBC, Straits Times) |
| Chromebook procurement from 2021-2022 | FALSE | 90% | Actual timeline was 2020-2021 per multiple sources |
| He pleaded not guilty | UNVERIFIABLE | 60% | Sources mention defense plea but don't confirm content |
| Gojek has 170M+ users | UNVERIFIABLE | 60% | Most recent evidence is 2019-2020; no current data |
| Remained as minority shareholder during ministry | TRUE | 95% | Reuters directly confirms passive stakeholder status |

This test validates that the 3-tier verdict scale and expanded search coverage produce more defensible and more source grounded 
  

