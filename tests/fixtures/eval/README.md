# Frozen eval corpus

The eval gate must measure a fixed corpus, or its baseline means nothing.

`invoice_processing/data/agent_output/` is regenerated on every pipeline run and
is gitignored, so a fresh checkout has no such directory — CI's eval step would
exit 1 before evaluating anything. These three cases are the frozen copies the
gate runs against.

Regenerate deliberately, never incidentally:

    cp invoice_processing/data/agent_output/<case>/Postprocessing_Data.json \
       tests/fixtures/eval/agent_output/<case>/
    python eval/eval.py --ground-truth invoice_processing/exemplary_data \
      --agent-output tests/fixtures/eval/agent_output --skip-llm --write-baseline

A change here moves the baseline, so it should appear in a diff and be reviewed
like any other behaviour change.
