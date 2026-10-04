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
