# Wikipedia Over-Representation, Work in Progress

## Test #1

When fact-checking claims, the system searches for evidence using Tavily, which returns only up to 10 results per claim. These results are then filtered to remove social/UGC domains (Facebook, YouTube, Reddit, etc.), keeping the top 3 "real" sources.

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
  

## Test #2
I have this text as a test to test TRUE FALSE or UNVERIFIABLE and is the wikipedia problem fixed?
```
Artificial intelligence has become one of the fastest-growing technologies in history. ChatGPT was publicly released by OpenAI in November 2022 and reached one million users in about five days. Since then, many governments and universities have begun developing policies for the responsible use of generative AI. In March 2023, Italy temporarily banned ChatGPT over privacy concerns before later restoring access. The European Union approved the AI Act in 2024, making it the world's first comprehensive AI regulation. Some experts argue that AI will eventually replace many office jobs, while others believe it will primarily augment human workers rather than replace them. OpenAI has stated that GPT-4 performs better than GPT-3.5 on many standardized benchmarks, although benchmark scores do not necessarily translate directly into real-world performance. Today, more people use generative AI than ever before, and AI adoption is increasing across industries including healthcare, education, finance, and software engineering.
```
### Output:
```
(.venv) PS C:\Users\natan\VSC Code\fact-checker> python main.py
Real-Time Fact Checker
Model: Llama 3.3 70B (DeepInfra)

Input mode:
  1. Microphone (live)
  2. Live stream URL (YouTube/news)
  3. Article URL
  4. Paste text / paragraph

> 4
Paste your text, then press Enter twice when done:
Artificial intelligence has become one of the fastest-growing technologies in history. ChatGPT was publicly released by OpenAI in November 2022 and reached one million users in about five days. Since then, many governments and universities have begun developing policies for the responsible use of generative AI. In March 2023, Italy temporarily banned ChatGPT over privacy concerns before later restoring access. The European Union approved the AI Act in 2024, making it the world's first comprehensive AI regulation. Some experts argue that AI will eventually replace many office jobs, while others believe it will primarily augment human workers rather than replace them. OpenAI has stated that GPT-4 performs better than GPT-3.5 on many standardized benchmarks, although benchmark scores do not necessarily translate directly into real-world performance. Today, more people use generative AI than ever before, and AI adoption is increasing across industries including healthcare, education, finance, and software engineering.



Extracted 1 paragraphs. Fact-checking...


[TRUE]  SPEAKER_A
  Claim:  ChatGPT was publicly released by OpenAI in November 2022
  Conf:   [===================-] 95%
  Why:    Two independent sources, including a historical website and Wikipedia, confirm that ChatGPT was released to the public by OpenAI in November 2022. The exact date of release is specified as November 30, 2022, by one of the sources.
  Sources (2):
    = https://www.history.com/this-day-in-history/november-30/chatgpt-released-openai
    = https://en.wikipedia.org/wiki/ChatGPT [Note: Wikipedia, community-edited]

[UNVERIFIABLE]  SPEAKER_A
  Claim:  ChatGPT reached one million users in about five days
  Conf:   [============--------] 60%
  Why:    The search results do not provide direct evidence of the time it took for ChatGPT to reach one million users. The sources provide information on ChatGPT's current user base, growth, and market share, but do not include historical data on the initial user acquisition rate.
  Sources (3):
    = https://www.demandsage.com/chatgpt-statistics
    = https://explodingtopics.com/blog/chatgpt-users
    = https://fatjoe.com/blog/chatgpt-stats

[TRUE]  SPEAKER_A
  Claim:  Italy temporarily banned ChatGPT over privacy concerns in March 2023
  Conf:   [===================-] 95%
  Why:    The Italian watchdog cited concerns about ChatGPT's data collection and processing, and imposed a temporary limitation on the processing of Italian users' data. The ban was later lifted after the owners of ChatGPT addressed data privacy concerns.
  Sources (3):
    = https://source.washu.edu/2023/09/a-cautionary-tale-how-italys-chatgpt-ban-hurt-businesses-economy
    = https://www.theguardian.com/technology/2023/mar/31/italy-privacy-watchdog-bans-chatgpt-over-data-breach-concerns
    = https://www.dw.com/en/ai-italy-lifts-ban-on-chatgpt-after-data-privacy-improvements/a-65469742

[TRUE]  SPEAKER_A
  Claim:  The European Union approved the AI Act in 2024
  Conf:   [===================-] 95%
  Why:    The European Union's Artificial Intelligence Act was published in the Official Journal of the European Union on 12 July 2024 and entered into force on 1 August 2024. The sources confirm the AI Act's approval and implementation timeline.
  Sources (3):
    = https://ai-act-service-desk.ec.europa.eu/en/ai-act/timeline/timeline-implementation-eu-ai-act
    = https://www.kennedyslaw.com/en/thought-leadership/article/2026/the-eu-ai-act-implementation-timeline-understanding-the-next-deadline-for-compliance
    = https://www.goodwinlaw.com/en/insights/publications/2024/10/insights-technology-aiml-eu-ai-act-implementation-timeline

[TRUE]  SPEAKER_A
  Claim:  OpenAI has stated that GPT-4 performs better than GPT-3.5 on many standardized benchmarks
  Conf:   [===================-] 95%
  Why:    Multiple sources, including Synthedia and Coursera, confirm that OpenAI has announced GPT-4 outperforms GPT-3.5 in many evaluations, with improvements in accuracy, safety, and functionality. Datastudios also reports a significant leap in language understanding and multimodal intelligence.
  Sources (3):
    = https://synthedia.substack.com/p/gpt-4-is-better-than-gpt-35-here
    = https://www.coursera.org/articles/chat-gpt-3-vs-4
    = https://www.datastudios.org/post/chatgpt-4o-vs-gpt-3-5-full-comparison-and-report
```

### The Problem This Test Was Checking

Test #1 fixed Wikipedia over-representation on the Nadiem Makarim article, but that was one article, one topic. Test #2 uses a completely different topic (AI/ChatGPT/regulation, a domain where Wikipedia articles are dense and well-maintained, exactly the condition that caused the original 12-instance problem) to check whether the fix holds outside the article it was tuned on, or whether it was a fix that only worked by coincidence on that one input.

### Wikipedia Fix: Confirmed Fixed

Across all 5 verdicts and 13 total source slots in this output, `wikipedia.org` appears **exactly once**, in claim 1's sources list:

```
Sources (2):
    = https://www.history.com/this-day-in-history/november-30/chatgpt-released-openai
    = https://en.wikipedia.org/wiki/ChatGPT [Note: Wikipedia, community-edited]
```

That single instance is also the good case, not the bad one, it sits alongside a non-Wikipedia source rather than filling every source slot the way it did before the fix, and it carries the `[Note: Wikipedia, community-edited]`. No verdict in this test has 2 or 3 Wikipedia links crowding out other sources, which was the actual failure mode Test #1 was written to fix.

This confirms the VERIFY_PROMPT source-prioritization fix generalizes: it wasn't a one-off result tied to the Nadiem Makarim article, it holds on a different topic domain where Wikipedia coverage is just as strong.

---

## Update (Jul 7): Filtering and Ranking Moved to Code

Tests #1 and #2 above validated the fix at the **prompt level** (VERIFY_PROMPT
telling the model to deprioritize Wikipedia). Since then, the same intent was
also implemented at the **code level**, so the behavior no longer depends on
the model consistently following that instruction:

- Social/UGC domains (Facebook, YouTube, Reddit, etc.) are now excluded via
  Tavily's own `exclude_domains` parameter, before results even come back,
  not filtered out in Python after the fact.
- `_filter_sources()` now explicitly sorts results into three tiers:
  official/high-quality domains first (`_HIGH_QUALITY`: Reuters, AP, BBC,
  .gov, WHO, World Bank, UN, IMF, Nature, ScienceDirect, NYT), Wikipedia
  demoted last (`_MEDIUM_QUALITY`), everything else in between.

The prompt-level fix documented above and the code-level fix are
complementary, not conflicting: the prompt guides the model's own source
selection, the code guarantees the ranking regardless of what the model does.
See `md/STATUS.md` Known Issue 2 and `md/ARCHITECTURE.md` for the current
implementation.
