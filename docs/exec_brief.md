# Executive brief: can we trust the answers?

_A one-page summary of hallucination risk and answer variability for the 10-K question-answering
assistant, with what we measured and what we recommend._

## The two risks in plain terms

- **Hallucination:** an AI model can state something confidently that isn't true, such as a
  revenue figure or a risk a company never disclosed. In finance, one invented number can undo
  trust in the whole tool.
- **Answers vary between runs:** ask the same question twice and the wording, the detail chosen,
  or occasionally the conclusion can differ. Today's models are designed to work this way, and
  the vendor advises against "turning it off" for the latest Gemini models.

## How the system is built to contain them

1. **It only answers from the filings.** Each answer is written from passages retrieved from the
   companies' own 10-Ks, never from the model's general knowledge.
2. **Every claim carries a citation** to a specific filing, section and SEC link, so a person can
   check it in seconds. Citations pointing at nothing are automatically flagged.
3. **When the filings don't contain the answer, it says so** in a fixed, recognisable sentence,
   rather than guessing. It also refuses to substitute a different company or year.
4. **Every release is tested before customers see it.** A fixed exam of 99 questions with known
   answers, reviewed by a person, is run automatically against each new version. The version
   only goes live if quality hasn't dropped.

## What we measured (99-question exam, deployed system)

| | Result |
|---|---|
| Answers fully or partly correct (correctness score) | **93–94%** |
| Answers containing an unsupported claim (hallucination) | **0 of 99** in each of two runs |
| Unanswerable questions correctly declined (other companies, wrong years, trivia) | **10 of 10** |
| Answerable questions wrongly declined | 4–6% |
| Cost per 1,000 questions | about $4.33 at current model prices |

**When it was wrong, it failed safely.** Every incorrect answer came from the search step not
finding the right page. In those cases the assistant declined ("I could not find this in the
provided filings") or reported only what it had found. It did not make something up.

## What variability looks like in practice

- The same question asked twice gave the **same figure** ($416,161 million in Apple's net sales)
  but cited a different number of supporting passages.
- A borderline question, about a spending measure ExxonMobil no longer reports under that name,
  was declined on one run. On another, it was answered by saying the measure isn't reported and
  giving the related figures the company does report. **Both answers were accurate; they were
  phrased differently.**
- Across two full exam runs of the same system, overall correctness moved by about one question
  in a hundred. That's why we judge quality over many questions, never on a single answer.

## Residual risk: what this does not guarantee

- **A 0% rate on 99 questions is strong evidence, not a guarantee.** Rare errors can still happen,
  especially on questions unlike the exam.
- **The exam was written partly with AI assistance** (then reviewed by a person). It should keep
  growing with your analysts' real questions.
- **Comparing two companies in one question is the weakest area** (the search step finds both
  companies' figures less often). Prefer the dedicated comparison mode for these.
- **Response time depends on the AI provider's shared capacity:** typically 2–5 seconds,
  occasionally much longer at busy times.

## Our recommendations

1. **Use it as an analyst's research assistant, not an unsupervised decision-maker.** For anything
   going to a client, a board or a regulator, a person checks the cited source. The citations make
   this fast.
2. **Keep the release test mandatory,** and add 5–10 real questions from your users every month.
3. **Track the declined-answer rate as well as accuracy.** A rising rate usually means the
   document collection or search needs attention.
4. **If response time matters contractually,** reserve dedicated model capacity (Provisioned
   Throughput). It's a known, fixed cost.
5. **Review cost before 1 January 2027,** when the model's introductory price ends: about $9 per
   1,000 questions at standard prices.
