# Running CADSmith on AWS Bedrock

Everything here is code that already works. What is missing is your account's
details, and they are deliberately missing: nothing in this repository holds a
credential, a region, an account id or a price, and none of it should.

Fill in the blanks on your own machine.

---

## What you need

- SSO access to the AWS account (you have this — ETIL developer group)
- **Model access granted** for the Claude models you intend to call. Group
  membership is not the same thing: Bedrock model access is enabled per
  account, per region, per model, in the Bedrock console.
- The AWS CLI, for `aws sso login`

Nothing else. Bedrock authenticates with your AWS credentials — there is no
Anthropic API key involved, and no key field to paste anything into.

---

## 1. Sign in

```
aws configure sso          # once, if this profile does not exist yet
aws sso login --profile <YOUR_PROFILE>
```

Then tell the app which profile and region to use. Either export them:

```
setx AWS_PROFILE  <YOUR_PROFILE>          # Windows, persists
setx AWS_REGION   <YOUR_REGION>
```

or put them in `.env` at the repository root (already gitignored):

```
AWS_PROFILE=<YOUR_PROFILE>
AWS_REGION=<YOUR_REGION>
```

The app resolves credentials through botocore, in the same order the AWS CLI
does: environment variables, then the named profile, then the SSO session,
then an instance role. If `aws sts get-caller-identity` works, so will the app.

---

## 2. Find out which model ids your account can actually call

**Do this before running the app.** Bedrock model ids are region-specific, and
the id that works in one region is often not the id that works in another.

```
aws bedrock list-foundation-models --region <YOUR_REGION> ^
    --query "modelSummaries[?contains(modelId,'claude')].modelId"

aws bedrock list-inference-profiles --region <YOUR_REGION>
```

Two shapes come back:

| Shape | Looks like | When |
|---|---|---|
| Direct model id | `anthropic.claude-...` | The model is served in that region |
| Cross-region inference profile | `us.anthropic.claude-...`, `eu.anthropic.…` | The model is only reachable through a profile |

The app defaults to `anthropic.claude-sonnet-5` to generate and
`anthropic.claude-opus-5` to judge. **If your account lists a different id, or
lists it only as an inference profile, type the id you actually have into the
app's Generation model and Judge model fields.** They are free-text.

---

## 3. Check it before spending anything

```
.venv\Scripts\python -m app.tools.bedrock_check --no-call
```

That checks region, credentials, model configuration and the spend guard, and
sends nothing. When it looks right, drop `--no-call` and it sends one probe —
eight output tokens — to prove the model actually answers:

```
.venv\Scripts\python -m app.tools.bedrock_check
```

A validation error naming the model almost always means the id is wrong for
your region or the account has not been granted access to it. The check says
so and tells you which command lists the alternatives.

---

## 4. Run the app

```
app\run_app.ps1
```

Pick **AWS Bedrock (Anthropic)** in the provider list. There is no key to
paste; if the header says the environment is ready, the credentials resolved.

---

## Not spending more than you meant to

This is the part worth reading twice.

**Every run has a hard token ceiling.** The pipeline is a loop — plan, code,
execute, judge, refine, repeat — and the vision Judge sends a rendered image
on every pass, so input tokens grow fastest. A part that will not converge is
the expensive case, and it is the one that is capped. The budget is checked
*before* each model call; when the allowance is gone the run stops, keeps the
attempts it already produced, and says why.

Default: **250,000 tokens per run**. A normal part measures around 40,000, so
this stops a runaway without interfering with real work.

```
CADSMITH_TOKEN_BUDGET=100000        # tighter, while you are finding your feet
```

**Three settings decide most of the cost**, and all three are on screen:

| Setting | Effect |
|---|---|
| Refinement iterations | Each one is a full code + judge round. Start at 1. |
| Vision Judge | Off drops an image from every judging call. Cheapest single saving. |
| Standard dimensions | Adds a reference block to the Planner prompt. Small. |

**Standard parts cost nothing.** A washer, a bearing, an ISO fastener, a gear
— those come from the catalogue with zero model calls. Leave the catalogue on.

**Cost in money is not shown unless you supply the rates.** Claude on Bedrock
is billed by AWS at AWS's own per-region prices, and this app will not guess
them. If you want an estimate, read your rates off the Bedrock pricing page
and set:

```
CADSMITH_INPUT_PER_MTOK=<YOUR_INPUT_RATE>
CADSMITH_OUTPUT_PER_MTOK=<YOUR_OUTPUT_RATE>
```

Then every run reports an estimate alongside its token count. Without them you
still get exact token counts, which are what the API actually reports.

**Belt and braces, on the AWS side:** set a Budget with an alert in AWS
Billing for the account. Nothing in an application can protect you as reliably
as the provider's own cap.

---

## If something does not work

| Symptom | Cause |
|---|---|
| "No AWS credentials found" | SSO session expired — `aws sso login` again |
| Validation error naming the model | Wrong id for the region, or access not granted |
| "No region" | `AWS_REGION` unset and the profile does not configure one |
| Throttling | Bedrock per-model quota; lower the refinement iterations |
| Works in the CLI, not the app | Different profile — the app reads `AWS_PROFILE` |

`.venv\Scripts\python -m app.tools.doctor` reports the whole environment.
