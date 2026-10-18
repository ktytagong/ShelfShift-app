# ShelfShift reviewer guide

This guide covers signing in, the test accounts, one walkthrough, where Gemini works (decision briefs, Ask and field
reports) and the two clocks you will see.
Everything here matches the full version released on 18 Oct 2026 (Gemini decision briefs and Ask,
and the verify-before-you-move screens).

The app runs on the FreshRetailNet-50K public sales dataset (CC BY 4.0, Dingdong-Inc,
https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K) with simulated stock and labelled assumptions. Every
screen says so: "Public sales dataset · Simulated operational scenario". The dataset's exploratory analysis, demand
forecasting and stockout detection notebook:
https://github.com/khadija2027/Demand-Forecasting-and-Inventory-Safety-in-the-Retail-Sector

## Sign in

1. Open the app. A sign-in card shows.
2. Under "Sign in as", choose **Store manager** or **HQ planner**.
3. Store manager: type a store code in **Store code** (for example `S329`) and the store passcode in **Passcode**.
   HQ planner: type the HQ passcode in **HQ passcode**.
4. Press **Sign in**. The app opens on **Today**, with the navigation on the left (a menu button on a phone).

- The HQ passcode and the store passcode are on slide 12 of the submission deck ("Provide links to your"). They are never written in this guide.
- Every store manager uses the same store passcode; HQ has its own. The store passcode never opens HQ.
- Each browser gets its own private workspace. Nothing you do affects another reviewer, and the shared data is never
  changed.
- To act as another store, open the account menu (the button that starts with "Account:") and press **Sign out**, then
  sign in with the other store code. Your workspace is kept in this browser, so the other store sees your changes.
- Too many wrong passcodes from one address pause sign-in for a while. The message says how to continue.

## Accounts

Find stock decisions by what they show, not by their key: the keys are numbered when the data is built.

| Account | Sign in as | Why you would use it |
|---|---|---|
| `S329` | Store manager | Source store of the verify-before-move decision (product P191, lot S329-P191-2-r). The stock check is addressed to S329 itself. |
| `S538` | Store manager | Source store of the transfer decision used in the walkthrough (product P267, lot S538-P267-2-r). |
| `S892` | Store manager | Destination store of that transfer. |
| `S189` | Store manager | Gemini on a stock decision whose options differ clearly: product P129, lot S189-P129--2-r (Part 3). |
| HQ | HQ planner | Sees every store, can take ownership of any stock decision, and is the only account that changes policies. |

Other examples on the release data, if you want to see more kinds: a markdown at `S678` (product P834, Markdown 20%), a
keep at `S760` (product P589), a cold-chain exclusion at `S415` (product P267) and a close call at `S705` (product P589).

## Walkthrough

**Part 1: verify before you move (store S329).**

1. Sign in as store manager `S329`.
2. Open **Decision board** in the navigation. Click the card for product P191, lot S329-P191-2-r, then press
   **Open stock decision**. The plan is a transfer to S223.
3. In the steps bar, press **2. Context**. The fact check for this lot's recorded stock offers **Count stock**.
   Press it, then press **Send request** in the panel that opens. The row then shows the stock check as requested.
4. Open **Tasks** in the navigation. The stock check is there. Type `0` in **Shelf** and **Backroom** for each lot.
   Under **When did you check?** choose **Checked just now**, then press **Send answer**. The task reads
   "Answered · confirm the count".
5. Tick "I checked the shelf and the backroom for this lot" and press **Confirm count**. Confirming approves nothing; it
   recomputes the stock decision.
6. Open the stock decision again and look at **Options**: the recommendation is now **Keep** under a new
   recommendation version. With no stock on the shelf, there is nothing to move.

**Part 2: a branch transfer from start to close (stores S538 and S892).**

1. Sign out, then sign in as store manager `S538`.
2. Open **Decision board**, click the card for product P267, lot S538-P267-2-r, and press **Open stock decision**.
3. Press **Take ownership**, then **Send for approval**. The page now says it is waiting for S892.
4. Sign out and sign in as `S892`. Open the same stock decision from the Decision board and press
   **Accept transfer**. If the plan still rests on facts nobody has checked, a tick box shows first ("I've seen this
   unverified fact and approve with it unresolved"); tick it, and **Accept transfer** becomes available.
5. Sign out and sign in as `S538`. Open the stock decision, tick the same box if it shows, and press **Approve plan**,
   then **Prepare transfer work order**.
6. Press **Record dispatch**. Fill **Units dispatched** with the committed quantity shown on the work order, choose a
   **Condition**, and press **Record dispatch** again to save.
7. Sign out and sign in as `S892`. Open the stock decision, press **Record receipt**, fill
   **Units received in good condition** with the units sent and **Units damaged** with `0`, choose a **Condition**,
   and press **Record receipt** again.
8. Sign out and sign in as `S538`. Open the stock decision. If it offers **Record price change**, tick
   "I changed the shelf price for this lot" and save. Then press **Close stock decision**, write a
   **Closing note**, and press **Close stock decision** again. The decision moves to Archive with your closing line.

At every step each account sees only the action that is its own. HQ can take ownership but cannot approve on a
store's behalf.

**Part 3: Gemini on a stock decision (any account).**

Gemini (gemini-3.5-flash on Google Cloud) explains and questions the engine's plan. It never computes a number and
never approves anything: every figure comes from the engine, and people make every decision.

1. Sign in as store manager `S189`. Open the stock decision for product P129 (lot S189-P129--2-r) and press
   **4. Compare** in the steps bar. Its options differ clearly, so you can see how the brief and the answers follow
   the engine's figures.
2. Under **Decision brief**, Gemini's summary of the plan shows with a label:
   - **Precomputed**: Gemini wrote it when this release was built.
   - **Live**: Gemini wrote it when you opened the decision.
   - **Fallback · Gemini unavailable · figures from the engine**: Gemini did not answer, so the engine's own summary
     shows instead. Options and approvals still work.
   Each figure in the brief links to the engine result it came from; click one to see that source. **Generation
   details** shows the model, the tokens used and the time taken.
3. Under **Ask about this stock decision**, press a suggestion (**Compare these options** or **What needs checking?**)
   or type your own question in **Your question**, then press **Ask question**. Gemini looks up this decision's stock,
   forecast, options and checks, and answers with links to what it used. Your questions are visible only to you.
4. A daily AI limit protects the shared budget. If it is reached, the box says so and resets at 00:00 Bangkok time;
   everything else keeps working.

**Part 4: Gemini reads field reports.**

1. Sign in as store manager `S223`, open **Business events** and press **Report an observation**.
2. In **What happened**, type "One chiller at S223 is broken; we can only take half the chilled stock until tomorrow."
   (or attach a photo or PDF) and press **Submit report**.
3. Gemini reads the report and fills in **Suggested fields**, each marked **Suggested by Gemini**. Accept each one
   with **Use suggested value**, or type your own. Where you typed something else, both show.
4. **Preview impact** shows which open stock decisions would change if the report is confirmed
   ("Potential impact · unconfirmed"). Gemini never confirms a report; a person does.

More samples (a local event, a carrier quote as a screenshot and as a PDF, a blurry photo, and a message that tries to
give instructions) are in [docs/guide](guide/README.md), with the account to use, what to enter and what Gemini
should read.

## Two clocks

The app shows two different times, on purpose.

- **"Recorded"** is when a person acted: the real time of your click, in Bangkok time, for example
  "Recorded 18 Oct 2026, 09:12 Bangkok time".
- **"Scenario date"** is the operational date of the public sales scenario: "Scenario date: Sat 17 Oct 2026". It is
  the day the stock decisions are about. It never moves with the calendar, and the data is never live.

The sales history is real public data shifted into the judging week; the stock, the network and the operational
scenario are simulated and labelled as such.
