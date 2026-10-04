"""Whether the injected-vs-used ratio may be ACTED ON. Default: no.

⚠⚠ THE RATIO IS NEGATIVELY COUPLED TO THE THING IT IS READ AS MEASURING.

`telemetry_record.compute_files_used` defines it in its own words: *"A file is 'used' if any
tool call targeted a file that the .md doc describes."* Matching is by word-stem of the doc's
filename against tool-call targets.

So `used` requires a tool call on the subject AFTER the document was injected -- and injection
exists to make that tool call unnecessary. The two are SUBSTITUTIVE: when an injection works,
the model does not need to go and read the code the document describes, so `used` goes DOWN.
**A perfect router scores zero.**

Field evidence (issue #10, a long-running single-project session): across 1,706 injections and
100 accesses, the injected set and the used set DID NOT INTERSECT AT ALL. One file was 99.6% of
injection volume and was never once "accessed". That is the EXPECTED output of the definition
above, not evidence of a mis-routing.

WHAT ACTED ON IT, AND WHAT THAT DID:

  * `advisor._find_high_waste` recommended "Consider removing {file} from keywords.json" once a
    file had been injected 10 times with a waste ratio >= 0.9 -- i.e. it recommended removing
    exactly the files whose injection had WORKED.
  * `learner._learn_prompt_affinity` applied a "mild penalty" to every injected-but-not-used
    file, so the learner was trained to prefer documents that did NOT answer the question.

Both are gated on this flag and both are OFF until the signal is replaced by a holdout
measurement -- withhold injection on a logged fraction of prompts and compare the two arms.
Then one flag turns both back on, because both were waiting on the same fact.

Set ATTNROUTE_TRUST_USED_SIGNAL=1 to re-enable. Nothing in the package sets it.
"""

import os

ENV_VAR = "ATTNROUTE_TRUST_USED_SIGNAL"

#: One sentence, used wherever a surface has to explain why a number is not being acted on.
WHY_NOT_TRUSTED = (
    "the injected-vs-used ratio is substitutive -- `used` needs a tool call that a working "
    "injection removes -- so it is reported as a diagnostic and never acted on (see "
    "attnroute/used_signal.py)"
)


def trust_used_signal() -> bool:
    """May the injected-vs-used ratio drive a reward, a penalty or a recommendation?

    Read from the environment on EVERY call rather than cached at import, so a test can set it
    and so a long-lived hook process picks up a change without a restart.
    """
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


# ═══ OBSERVE-ONLY ══════════════════════════════════════════════════════════════════════════
#
# ⚠ WHY THIS EXISTS, AND IT IS A HONESTY FIX RATHER THAN A FEATURE. "Recording-only" was
#   described to the owner as "changes no behaviour; it just starts collecting numbers", and
#   that was NOT TRUE of the package's default state: the router still injected. On eight
#   long-running sessions that is new context in every prompt, unmeasured.
#
# In OBSERVE-ONLY the router does everything except emit: it scores, selects, builds the
# injection and LOGS what it would have sent, then writes nothing to stdout. Two consequences
# worth stating:
#
#   * It is an honest BASELINE ARM -- a session in this mode is 100% held out, so it measures
#     what a turn costs with no routing at all, which is the denominator the whole exercise
#     needs and which no amount of per-file holdout can produce.
#   * The turn record stays comparable with an injecting session, because the same fields are
#     written from the same computation. `injection_emitted` is what distinguishes them, and an
#     analysis that ignores it would count suppressed context as delivered.
#
# Turning injection ON is a separate, explicit step. Nothing in the package sets this.
OBSERVE_ONLY_ENV = "ATTNROUTE_OBSERVE_ONLY"


def observe_only() -> bool:
    """Compute and log the injection, but emit nothing?

    Read per call, like `trust_used_signal`, so a long-lived hook picks up a change and a test
    can set it without reloading the module.
    """
    return os.environ.get(OBSERVE_ONLY_ENV, "").strip().lower() in ("1", "true", "yes", "on")
