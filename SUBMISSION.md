# Submission Summary

## Identification

```
Full name:              YOUR FULL NAME
Roll number:            i221234
Class / section:        YOUR SECTION
University email:       YOUR EMAIL
GitHub username:        YOUR USERNAME
Agent name:             Expense Ledger Agent
Domain:                 Expense Ledger Agent (receipt text or CSV -> categorized sandbox ledger)
```

## URLs

```
GitHub repository URL:  https://github.com/syedhassan13/AgenticAi_Assigment_1
Final commit hash:      PASTE_COMMIT_HASH_HERE
Working agent interface: https://YOUR-APP.onrender.com/
Health endpoint (GET):  https://YOUR-APP.onrender.com/health
Arena endpoint (POST):  https://YOUR-APP.onrender.com/arena/run
Manifest endpoint (GET): https://YOUR-APP.onrender.com/arena/manifest
API documentation:      https://YOUR-APP.onrender.com/docs
```

## Configuration

```
Hosting provider:       Render (free tier)
Default model/provider: gemini-3.8-flash / Google Gemini
Other available models: gpt-4o-mini (if OPENAI_API_KEY configured)
Example input:          "Process expenses: Coffee $4.50, Lunch $11.25, Uber $12.00"
Expected result:        Categorized ledger with 3 entries (food, food, transport)
Cold-start limitations: Free Render sleeps after 15min; first request ~30-60s; memory lost on restart
Repository access:      Instructor invited / access pending
Public test results:    evaluation/public_test_results.json
```

## Technical Details

- **Agent loop**: observe -> LLM decision -> schema + semantic validation -> bounded repair -> tool execution -> feedback
- **Tools**: parse_input, categorize_expense, detect_duplicates, write_ledger, request_clarification
- **Validation**: Pydantic schema + business-rule semantic checks, max 2 repair retries
- **Context layers**: SYSTEM, USER, STATE (dynamic template), EXTERNAL (untrusted/delimited), TOOL OBS
- **Stopping**: max_steps, timeout, goal_completed, clarification_needed, autonomy_boundary, budget_exceeded
- **Memory**: In-memory bounded (6 turns, 24K chars), single worker, lost on restart
- **Tests**: 48 unit tests + 12 public Arena cases + model comparison harness
