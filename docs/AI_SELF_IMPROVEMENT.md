# Can this system improve itself with AI? A research note

*Written with Phase 7 (September 2026). This note covers what the published evidence says, what the
statistics of this particular system allow, and what is worth building next, in order. It is a
simulation project: nothing here is advice to trade real money.*

---

## The short answer

**Yes, but not the way "self-improving AI trader" usually suggests.** The limiting factor is not how
clever the AI is. It is **how little evidence a daily, 4-coin trend system produces**. A system that
changes itself based on its own live results will, for many years, be reacting mostly to luck.

The approaches that can work share three traits:

1. They are **measured forward** against an unchanged twin, never judged by a backtest that the AI
   may have "seen".
2. They improve things that **can be checked quickly**: bugs, data errors, risk and cost. They do not
   rely on "higher returns", which take years to confirm.
3. **A human approves every change.** Nothing rewrites its own rules unattended.

Ranked recommendation (details below):

| # | Idea | Verdict | Why |
|---|---|---|---|
| 1 | **Champion/challenger shadow accounts** | **Built (Phase 7)**; extend | The only honest way to measure any AI idea live |
| 2 | **AI operations reviewer** (weekly plain-English review of the journal) | **Do next** | Cheap, safe; finds bugs/data/cost problems, which are checkable |
| 3 | **AI research assistant under a locked protocol** | **Do, carefully** | Can find real improvements; must count every trial and keep a sealed holdout |
| 4 | **Risk-side models** (volatility / drawdown forecasting) | Later | Risk is measurable in months, returns in decades |
| 5 | **Meta-labeling** (a small ML filter on the rule signals) | Only with far more data | Needs thousands of labelled signals; 4 coins give tens per year |
| 6 | **LLM news / event-risk filter** | Experiment only, as a challenger | Can't be backtested honestly; forward-test only |
| 7 | Performance-chasing allocation (bandits between S1/S2/S3) | **No** | Chases noise at this sample size |
| 8 | Self-modifying code / automatic parameter updates from live P&L | **No** | Fits noise, and breaks the safety guarantees |
| 9 | Backtesting an LLM on pre-cutoff history | **Never** | The model may remember what happened (see §1.2) |

---

## 1. The three constraints that decide everything

### 1.1 Statistical power: this system produces very little evidence

The strategies trade daily candles on 4 coins. In the walk-forward research (run on the synthetic
test prices; rerun it on real data) the combined portfolio made roughly **30 trades a year**. The yardstick below is the Sharpe ratio.

For a single strategy with a true Sharpe of 0.5, the standard error of its measured Sharpe is about
√((1 + SR²/2)/T) ([Lo 2002](https://rpc.cfainstitute.org/research/financial-analysts-journal/2002/the-statistics-of-sharpe-ratios)):

| Years of data | 1 | 3 | 5 | 10 |
|---|---|---|---|---|
| Uncertainty (±1 standard error) | ±1.06 | ±0.61 | ±0.47 | ±0.34 |

To detect that a *change* improved things, compare two accounts. Here that means the AI account
against its shadow twin, whose returns are highly correlated because most trades are shared. The
table below gives the years of daily data needed before a Sharpe improvement stands about 2 standard
errors clear of noise. It uses the asymptotic Jobson-Korkie/Memmel variance of the difference,
Var ≈ [2 − 2ρ + ½(SR_a² + SR_b² − 2·SR_a·SR_b·ρ²)] / T, with a baseline Sharpe of 0.5, T in
years and ρ the correlation of daily returns; years needed = 4·Var·T / gain².

| True Sharpe gain | correlation 0.50 | 0.80 | 0.95 |
|---|---|---|---|
| +0.10 | 492 years | 205 | 54 |
| +0.25 | 84 | 36 | 11 |
| +0.50 | 24 | 11 | 4.4 |
| +1.00 | 8.2 | 4.7 | 2.7 |

**What this means in practice:**
- Any loop that learns from the system's own live profit and loss will, for years, mostly learn noise.
- Only a very large improvement, or one that barely changes the trades (correlation 0.95), shows up
  within a few years.
- `trader llm report` does this test for you. It says "no evidence either way" until the evidence
  exists, and estimates how long that will take.
- Risk quantities (volatility, drawdown depth, loss per stopped trade) are estimated far more
  precisely than average returns. So improvements aimed at **risk** can be confirmed much sooner than
  improvements aimed at **return**.

### 1.2 Look-ahead through memory: LLMs remember history

A language model trained on data up to some date may *know what happened next* for any earlier day.
This is documented, not hypothetical:
- Lopez-Lira, Tang & Zhu show LLMs recall exact pre-cutoff values of economic and financial data.
  Telling the model to "respect the date", or masking names, does not stop it
  ([The Memorization Problem, 2025](https://arxiv.org/abs/2504.14765)).
- Gao, Jiang & Yan measure a "lookahead propensity". LLM forecasts are most accurate exactly where
  the model probably saw the outcome, and the effect collapses after the training cutoff
  ([Detecting Lookahead Bias in LLM Forecasts](https://arxiv.org/abs/2512.23847)).
- Glasserman & Lin find the same kind of problem in GPT sentiment backtests
  ([2023](https://arxiv.org/abs/2309.17322)).

**Consequence:** an LLM can only be evaluated on days **after its training cutoff**, i.e. forward.
That is why Phase 7's analyst runs only in live paper trading, never in backtests. It is also why
old catch-up days (`llm.max_age_days`) are decided by the rules alone.

### 1.3 Multiple testing: every idea tried is a lottery ticket

If you try 50 variants and keep the best, the winner's backtest Sharpe is inflated even if all 50 are
pure noise. The **Deflated Sharpe Ratio** corrects a result for the number of trials and for fat
tails. The **Probability of Backtest Overfitting** shows that the chance of picking an overfit
strategy grows quickly with the number of trials
([Bailey & López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551);
[Bailey et al.](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf)).

An AI that tirelessly proposes and tests ideas is a **multiple-testing machine**. Unless every trial
is counted and a holdout stays sealed, it will "discover" noise faster than any human could. The
2026 methodology paper on agentic asset-pricing research reaches the same conclusion. It argues that
the *discovery system itself* must be backtested, re-running the whole loop at past dates, not just
the factors it found ([Pan, Ding & Giesecke](https://arxiv.org/abs/2609.00731)).

---

## 2. What the evidence says about LLM traders

- **StockBench** (multi-month, contamination-free): most LLM agents, including frontier models,
  **did not beat buy-and-hold** on return or risk-adjusted return. Some had shallower drawdowns
  ([2025](https://arxiv.org/abs/2510.02209)).
- **LiveTradeBench**: general reasoning scores did **not** predict trading results
  ([2025](https://arxiv.org/html/2511.03628)).
- **Agent Market Arena** reports agents beating buy-and-hold on BTC/ETH/stocks. That was over **two
  months** of live trading, which §1.1 shows is far too short to tell skill from luck. It also found
  that the agent *framework* mattered more than the model
  ([2025](https://arxiv.org/abs/2510.11695)).
- **LLM-driven factor mining** (e.g. AlphaAgent) reports decay-resistant factors on equity
  cross-sections of hundreds of stocks. That breadth is what 4 coins lack. Such work also relies
  heavily on regularisation against overfitting and crowding
  ([KDD 2025](https://arxiv.org/abs/2502.16789)).
- **Meta-labeling** (a secondary model deciding whether and how big to take each primary signal) has
  published support ([Singh & Joubert](https://hudsonthames.org/wp-content/uploads/2022/04/Does-Meta-Labeling-Add-to-Signal-Efficacy.pdf)),
  but it needs many labelled examples.

**The honest expectation for Phase 7's analyst:** a small or zero effect on returns, possibly some
reduction in bad entries, at a known cost per review. The shadow account exists to find out. It
should not be assumed.

---

## 3. The options in detail

### 3.1 Champion / challenger shadows: built, and the foundation for everything else
**What:** every change runs as a *challenger* account beside an unchanged *champion*. Both start the
same day, with the same money and the same data. The difference between them is the change's
effect.
**Status:** built in Phase 7 as the AI account and its rules-only shadow, with the paired test in
`trader llm report`.
**Next step:** generalise it to N challengers (a different prompt, effort level, model, or rule
tweak). Adopt a change only when a **pre-registered** rule fires, written down *before* looking, for
example "after ≥ 2 years, t ≥ 2 on paired daily returns, net of AI cost, and no worse max drawdown".
**Cost:** each AI challenger costs its API reviews; rule-only challengers cost nothing.

### 3.2 AI operations reviewer: recommended next
**What:** once a week, an LLM reads the journal: risk events, skipped or failed days, data-validation
warnings, alert failures, AI fallbacks and costs, plus the week's trades with their decision trails.
It writes a short plain-English review for you and flags anything that looks wrong (a stop that did
not ratchet, a coin repeatedly excluded, a cost spike, an unusual gap fill).
**Why it works:** it targets **errors, not alpha**. Errors are verifiable, since you can check each
flag, and fixing a bug is a real improvement you can confirm right away.
**Guardrails:**
- Read-only.
- It sees only the journal (data up to the review date).
- Its output is a report, never a change.
**Cost:** roughly one call a week.

### 3.3 AI research assistant under a locked protocol: the real "self-improvement", carefully
**What:** an agent proposes hypotheses (a new exit rule, a filter, a parameter family) as code in a
*research branch* and runs the existing walk-forward research on them.
**Protocol, so that it does not become a noise-mining machine:**
1. **Count every trial.** Report the Deflated Sharpe Ratio using the total number of variants the
   agent ever tried, not just the winner.
2. **Seal a holdout.** Keep the most recent 12–24 months untouched until a final, single check.
3. **Minimums.** At least 100 out-of-sample trades, and robustness across all coins and regimes; the
   sensitivity heatmap must show a plateau, not a spike.
4. **A human reviews** the diff and the report and makes the decision.
5. **Forward shadow.** An adopted idea then runs as a challenger (§3.1) for at least 6–12 months
   before it replaces anything.

**Expected yield:** occasional small, robust improvements, mostly in risk or cost handling; most
proposals will (correctly) fail the protocol.
**Build cost:** medium. The research engine, walk-forward, sensitivity and Monte Carlo already exist
from Phase 3. What is missing is the trial counter, the DSR and the sealed holdout.

### 3.4 Risk-side models: later
Volatility forecasting (e.g. GARCH-type or simple realised-volatility models) or drawdown-risk
scoring would scale exposure down in turbulent periods. S3 already does the robust, simple version:
volatility targeting. A learned version is checkable within months, because risk estimates converge
fast (§1.1), so it could be a reasonable second challenger. It must beat the simple
volatility-target rule, not just "no rule".

### 3.5 Meta-labeling: only with far more data
A small classifier (for example gradient-boosted trees on the signal's indicators, volatility,
regime and trend age) predicts whether each rule signal will reach its target before its stop, and
sizes accordingly.
**Problem here:** tens of signals a year across 4 coins, while a classifier needs thousands of
examples, plus purged and embargoed cross-validation.
**How it could become viable:** expand the universe to 20–50 liquid coins, or train across asset
classes, then forward-test it as a challenger. Until then it would overfit.

### 3.6 LLM news / event-risk filter: experiment, forward-only
An LLM with web search would scan for exchange hacks, delistings, regulatory actions or
stablecoin de-pegs before an entry, and veto or shrink the trade.
**Plausible value:** avoiding rare disasters.
**Why forward-only:** it cannot be backtested honestly (§1.2), and historical news archives are
licensed and leak hindsight.
**Cost:** higher per review. The effect is rare, so measurement takes even longer than §1.1 suggests.
Treat it as a curiosity challenger, not a core feature.

### 3.7 What not to do
- **Performance-chasing allocation** (bandits moving money to whichever of S1/S2/S3 did best
  recently). At this sample size it mostly buys after luck and sells after bad luck.
- **Automatic parameter re-optimisation from live results.** The Phase 3 research (on synthetic
  prices) already showed the fixed defaults beating walk-forward re-optimisation. Doing it live and unattended is the same mistake
  with less oversight.
- **Letting an AI edit the trading code or risk limits unattended.** That breaks the guarantees this
  project is built on: one code path, look-ahead proofs, and a risk engine with final authority.
- **Judging any AI idea on a backtest over years the model may remember.**

---

## 4. How to judge the Phase 7 analyst you now have

1. Turn it on (`llm.enabled: true`, key in `.env`, then `.venv/bin/trader llm test`).
2. Leave it alone. Do not change the prompt, model or effort mid-experiment: that starts a new
   experiment.
3. Read `.venv/bin/trader llm report` or the dashboard's **AI analyst** card monthly. Expect "no
   evidence either way" for a long time; that is the honest answer, not a malfunction.
4. Decide in advance what would make you keep or drop it. A suggestion:
   - **Drop it** if, after one year, it is behind the shadow net of cost and has a deeper drawdown.
   - **Keep it on probation** otherwise.
   - **Believe in it** only after the paired test is significant.

---

## Sources

- Lo, A. (2002). *The Statistics of Sharpe Ratios.* Financial Analysts Journal. <https://rpc.cfainstitute.org/research/financial-analysts-journal/2002/the-statistics-of-sharpe-ratios>
- Bailey, D. & López de Prado, M. (2014). *The Deflated Sharpe Ratio.* <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551>
- Bailey, D. et al. *Statistical Overfitting and Backtest Performance.* <https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf>
- Lopez-Lira, A., Tang, Y. & Zhu, M. (2025). *The Memorization Problem: Can We Trust LLMs' Economic Forecasts?* <https://arxiv.org/abs/2504.14765>
- Gao, Z., Jiang, W. & Yan, Y. (2025). *Detecting Lookahead Bias in LLM Forecasts.* <https://arxiv.org/abs/2512.23847>
- Glasserman, P. & Lin, C. (2023). *Assessing Look-Ahead Bias in Stock Return Predictions Generated by GPT Sentiment Analysis.* <https://arxiv.org/abs/2309.17322>
- *StockBench: Can LLM Agents Trade Stocks Profitably in Real-World Markets?* (2025). <https://arxiv.org/abs/2510.02209>
- *LiveTradeBench: Seeking Real-World Alpha with Large Language Models* (2025). <https://arxiv.org/html/2511.03628>
- *When Agents Trade: Live Multi-Market Trading Benchmark for LLM Agents* (2025). <https://arxiv.org/abs/2510.11695>
- *AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay* (KDD 2025). <https://arxiv.org/abs/2502.16789>
- Pan, Y., Ding, X. & Giesecke, K. (2026). *Agentic Empirical Asset Pricing: Methodological Foundations.* <https://arxiv.org/abs/2609.00731>
- Singh, A. & Joubert, J. *Does Meta-Labeling Add to Signal Efficacy?* <https://hudsonthames.org/wp-content/uploads/2022/04/Does-Meta-Labeling-Add-to-Signal-Efficacy.pdf>
