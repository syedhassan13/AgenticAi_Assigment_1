# Submission Summary - Expense Ledger Agent

> Convert this document to PDF for the Google Classroom submission.
> Replace ALL placeholder values with your actual information.

---

## Identification

| Field | Value |
|-------|-------|
| **Full name** | YOUR FULL NAME |
| **Roll number** | i221234 |
| **Class / section** | YOUR SECTION |
| **University email** | YOUR EMAIL |
| **GitHub username** | YOUR USERNAME |
| **Agent name** | Expense Ledger Agent |
| **Domain** | Expense Ledger Agent (receipt text or CSV -> categorized sandbox ledger) |

---

## URLs

| Endpoint | URL |
|----------|-----|
| **GitHub repository** | https://github.com/syedhassan13/AgenticAi_Assigment_1 |
| **Final commit hash** | dfb0b3833fe45373b4a8cfa8578038823f97e8ec |
| **Working agent interface** | https://agenticai-assigment-1.onrender.com/ |
| **Health endpoint (GET)** | https://agenticai-assigment-1.onrender.com/health |
| **Arena endpoint (POST)** | https://agenticai-assigment-1.onrender.com/arena/run |
| **Manifest endpoint (GET)** | https://agenticai-assigment-1.onrender.com/arena/manifest |
| **API documentation** | https://agenticai-assigment-1.onrender.com/docs |

---

## Configuration

| Setting | Value |
|---------|-------|
| **Hosting provider** | Render (free tier) |
| **Default model / provider** | gemini-3.8-flash / Google Gemini |
| **Other available models** | gpt-4o-mini (if OPENAI_API_KEY configured) |
| **Example input** | "Process expenses: Coffee $4.50, Lunch $11.25, Uber $12.00" |
| **Expected result** | Categorized ledger with 3 entries totaling $27.75 |
| **Cold-start limitations** | Free Render sleeps after 15min; first request ~30-60s; in-memory history lost on restart |
| **Repository access** | Instructor invited / access pending |
| **Public test results** | evaluation/public_test_results.json |

---

## Verification Checklist

- [ ] Both attachments use my roll number; ZIP contains correctly named project folder
- [ ] ZIP, GitHub source, and deployed app correspond to the reported final commit
- [ ] Working interface opens in a private browser window
- [ ] GET /health and GET /arena/manifest work publicly
- [ ] POST /arena/run tested via /docs or HTTP client (not browser address bar)
- [ ] Agent can handle clarification across two messages in the same chat
- [ ] Public evaluation results and model/cost evidence are included
- [ ] Instructor invited to the private repository
- [ ] No keys, credentials, or sensitive data in either attachment
- [ ] Both PDF and SUBMISSION.md contain working URLs
- [ ] Google Classroom shows "Turned in"
